"""Fuente c) de la fase 3: reglas sobre inventory.json.

Cada regla es una función pequeña `regla_x(recursos, ctx) -> [findings]` registrada en REGLAS.
Para agregar una regla: escribe la función y súmala a la lista del final.
Los attrs se leen con .get() defensivo: si un colector no trae el dato, la regla no dispara.

Uso: from reglas import aplicar; findings = aplicar(inventory)
"""

from datetime import datetime, timezone

from hallazgos import hallazgo, parsear_fecha, severidad_por_costo

FUENTE = "regla"

# Precios de referencia us-east-1 (USD por GB-mes). Son estimados, no una factura.
PRECIO_EBS_GB = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
PRECIO_SNAPSHOT_GB = 0.05
PRECIO_EIP_MES = 3.60  # $0.005/hora
AHORRO_GP2_A_GP3_GB = PRECIO_EBS_GB["gp2"] - PRECIO_EBS_GB["gp3"]

DIAS_SNAPSHOT_VIEJO = 180
DIAS_EC2_DETENIDA = 30
PUERTOS_WEB = {"80", "443"}
# Puertos que, abiertos al mundo, suben la severidad a alta (administración y bases de datos).
PUERTOS_CRITICOS = {"22", "3389", "3306", "5432", "1433", "1521", "27017", "6379", "9200", "5601", "11211"}
TAGS_OWNER = {"owner", "team", "responsable", "dueño", "dueno", "squad", "contact"}
TAGS_ENV = {"environment", "env", "stage", "ambiente", "entorno"}


def _es(r, servicios, tipo):
    return r.get("service") in servicios and r.get("type") == tipo


def _attrs(r):
    return r.get("attrs") or {}


# ---------- reglas ----------

def regla_ebs_sin_adjuntar(recursos, ctx):
    out = []
    for r in recursos:
        if _es(r, {"ebs", "ec2"}, "volume") and r.get("state") == "available":
            a = _attrs(r)
            gb = a.get("size_gb") or 0
            usd = gb * PRECIO_EBS_GB.get(a.get("volume_type"), PRECIO_EBS_GB["gp2"])
            out.append(hallazgo("desperdicio", severidad_por_costo(usd, "media"), r["account_id"], r["region"], r["id"],
                                "Volumen EBS sin adjuntar",
                                f"{gb} GiB {a.get('volume_type')}, estado available. Snapshot y borrar si no se usa.",
                                usd, FUENTE))
    return out


def regla_eip_sin_asociar(recursos, ctx):
    out = []
    for r in recursos:
        if not _es(r, {"eip", "ec2"}, "elastic_ip"):
            continue
        a = _attrs(r)
        asociada = a.get("associated")
        if asociada is None:
            asociada = bool(a.get("association_id") or a.get("instance_id") or a.get("network_interface_id"))
        if not asociada and r.get("state") != "associated":
            out.append(hallazgo("desperdicio", "media", r["account_id"], r["region"], r["id"],
                                "Elastic IP sin asociar", f"IP {a.get('public_ip')} reservada sin uso; se cobra igual.",
                                PRECIO_EIP_MES, FUENTE))
    return out


def regla_snapshots_viejos(recursos, ctx):
    # Un snapshot que respalda una AMI registrada no se puede borrar sin deregistrar la AMI: no es "viejo".
    de_amis = {(r["account_id"], r["region"], s) for r in recursos if _es(r, {"ami", "ec2"}, "image")
               for s in _attrs(r).get("snapshot_ids") or []}
    out = []
    for r in recursos:
        if not _es(r, {"ebs", "ec2"}, "snapshot") or (r["account_id"], r["region"], r["id"]) in de_amis:
            continue
        a = _attrs(r)
        fecha = parsear_fecha(a.get("start_time") or a.get("create_time"))
        if not fecha:
            continue
        dias = (ctx["ahora"] - fecha).days
        if dias > DIAS_SNAPSHOT_VIEJO:
            gb = a.get("size_gb") or 0
            usd = gb * PRECIO_SNAPSHOT_GB  # tope: los snapshots son incrementales
            origen = ", ".join(r.get("relations", {}).get("attached_to", [])) or "sin volumen origen"
            out.append(hallazgo("desperdicio", severidad_por_costo(usd, "baja"), r["account_id"], r["region"], r["id"],
                                f"Snapshot EBS de más de {DIAS_SNAPSHOT_VIEJO} días",
                                f"{dias} días, {gb} GiB ({origen}). Costo máximo estimado; revisar si sigue siendo necesario.",
                                usd, FUENTE))
    return out


def _puertos_abiertos(a):
    """Lista de 'proto/puertos' abiertos a 0.0.0.0/0 o ::/0."""
    if a.get("open_to_world_ports"):
        return list(a["open_to_world_ports"])
    return sorted({f"{x.get('protocol')}/{x.get('ports')}" for x in a.get("ingress") or []
                   if x.get("source") in ("0.0.0.0/0", "::/0")})


def _es_critico(puerto):
    proto, _, rango = puerto.partition("/")
    if proto in ("icmp", "icmpv6", "1", "58"):
        return False
    if proto == "all" or rango == "all":
        return True
    if "-" in rango:
        desde, _, hasta = rango.partition("-")
        try:
            return any(int(desde) <= int(p) <= int(hasta) for p in PUERTOS_CRITICOS)
        except ValueError:
            return True
    return rango in PUERTOS_CRITICOS


def regla_sg_abierto(recursos, ctx):
    out = []
    for r in recursos:
        if not _es(r, {"vpc", "ec2"}, "security_group") or not _attrs(r).get("open_to_world"):
            continue
        puertos = [p for p in _puertos_abiertos(_attrs(r)) if p.partition("/")[2] not in PUERTOS_WEB]
        if not puertos:
            continue
        sev = "alta" if any(_es_critico(p) for p in puertos) else "media"
        out.append(hallazgo("riesgo", sev, r["account_id"], r["region"], r["id"],
                            "Security group abierto a internet en puertos no web",
                            f"{r.get('name') or r['id']}: 0.0.0.0/0 en {', '.join(puertos)}.", None, FUENTE))
    return out


def regla_rds_publico(recursos, ctx):
    out = []
    for r in recursos:
        if r.get("service") == "rds" and _attrs(r).get("publicly_accessible") is True:
            out.append(hallazgo("riesgo", "alta", r["account_id"], r["region"], r["id"],
                                "Base de datos RDS con acceso público",
                                f"{r.get('type')} {_attrs(r).get('engine')} con PubliclyAccessible=true.", None, FUENTE))
    return out


def regla_rds_sin_cifrar(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        # Las instancias miembro de un cluster heredan el cifrado del cluster: se reporta solo el cluster.
        if r.get("service") == "rds" and a.get("storage_encrypted") is False and not a.get("cluster_id"):
            out.append(hallazgo("riesgo", "media", r["account_id"], r["region"], r["id"],
                                "Base de datos RDS sin cifrado en reposo",
                                f"{r.get('type')} {a.get('engine')} con StorageEncrypted=false.", None, FUENTE))
    return out


def _fecha_detencion(a):
    fecha = parsear_fecha(a.get("stopped_at") or a.get("stop_time"))
    if fecha:
        return fecha
    # StateTransitionReason: "User initiated (2025-01-31 10:00:00 GMT)"
    razon = a.get("state_transition_reason") or ""
    if "(" in razon and "GMT" in razon:
        return parsear_fecha(razon.split("(")[-1].replace("GMT", "").strip(" )"))
    return None


def regla_ec2_detenida(recursos, ctx):
    out = []
    for r in recursos:
        if not (_es(r, {"ec2"}, "instance") and r.get("state") == "stopped"):
            continue
        fecha = _fecha_detencion(_attrs(r))
        if fecha and (ctx["ahora"] - fecha).days > DIAS_EC2_DETENIDA:
            out.append(hallazgo("desperdicio", "media", r["account_id"], r["region"], r["id"],
                                f"Instancia EC2 detenida hace más de {DIAS_EC2_DETENIDA} días",
                                f"{r.get('name') or r['id']} detenida desde {fecha.date()}; sus volúmenes siguen cobrando.",
                                None, FUENTE))
    return out


def regla_gp2(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        if _es(r, {"ebs", "ec2"}, "volume") and a.get("volume_type") == "gp2":
            gb = a.get("size_gb") or 0
            usd = gb * AHORRO_GP2_A_GP3_GB
            out.append(hallazgo("costo", "baja", r["account_id"], r["region"], r["id"], "Volumen gp2 migrable a gp3",
                                f"{gb} GiB gp2 → gp3 ahorra ~20% sin downtime (ahorro estimado).", usd, FUENTE))
    return out


def regla_opensearch_abierto(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        abierta = a.get("access_policy_principal_star") or a.get("policy_open") or a.get("open_access_policy")
        if r.get("service") == "opensearch" and abierta is True:
            # Fuera de VPC el endpoint es público: el riesgo es mucho mayor.
            sev = "media" if a.get("in_vpc") else "alta"
            out.append(hallazgo("riesgo", sev, r["account_id"], r["region"], r["id"],
                                "OpenSearch con access policy abierta (Principal *)",
                                f"Dominio {'en VPC' if a.get('in_vpc') else 'con endpoint público'}; "
                                f"fine-grained access={a.get('fine_grained_access')}.", None, FUENTE))
    return out


def regla_s3_sin_bloqueo_publico(recursos, ctx):
    out = []
    for r in recursos:
        a = _attrs(r)
        # None = no se pudo leer (ver attrs.errors); solo reportamos el False explícito.
        if _es(r, {"s3"}, "bucket") and a.get("public_access_fully_blocked") is False:
            detalle = "sin public access block" if not a.get("public_access_block") else \
                "public access block parcial: " + ", ".join(k for k, v in a["public_access_block"].items() if not v)
            out.append(hallazgo("riesgo", "media", r["account_id"], r["region"], r["id"],
                                "Bucket S3 sin bloqueo de acceso público completo",
                                f"{detalle}. Verificar si el bloqueo a nivel de cuenta lo cubre.", None, FUENTE))
    return out


def _sin_tag(tags, claves):
    return not any(k.lower() in claves for k in (tags or {}))


def servicios_sin_lectura_de_tags():
    """Colectores con TAGS = False: no leen tags, así que sus recursos siempre parecerían sin tag."""
    try:
        import importlib
        import pkgutil

        import colectores
    except ImportError:  # reglas usado sin 2-inventariar en el path: no se excluye nada
        return set()
    sin = set()
    for info in pkgutil.iter_modules(colectores.__path__):
        try:
            mod = importlib.import_module(f"colectores.{info.name}")
        except Exception:  # noqa: BLE001 — un colector roto no impide validar
            continue
        if not getattr(mod, "TAGS", True):
            sin.add(getattr(mod, "NAME", info.name))
    return sin


def _llave(r):
    return (r["account_id"], r.get("region"), r.get("service"), r.get("type"), r["id"])


def regla_sin_tags(recursos, ctx):
    """Agregada por cuenta: cuántos recursos no tienen tag de owner y/o environment. Solo cuenta
    recursos de colectores que leen tags; los demás se informan aparte en el detalle."""
    excluidos = ctx.get("servicios_sin_tags")
    if excluidos is None:
        excluidos = servicios_sin_lectura_de_tags()
    por_cuenta, sin_lectura = {}, {}
    for r in recursos:
        if r.get("service") in excluidos:
            sin_lectura[r["account_id"]] = sin_lectura.get(r["account_id"], 0) + 1
            continue
        por_cuenta.setdefault(r["account_id"], []).append(r)
    out = []
    for account_id, lista in sorted(por_cuenta.items()):
        sin_owner = [r for r in lista if _sin_tag(r.get("tags"), TAGS_OWNER)]
        sin_env = [r for r in lista if _sin_tag(r.get("tags"), TAGS_ENV)]
        faltan = {_llave(r) for r in sin_owner} | {_llave(r) for r in sin_env}
        if not faltan:
            continue
        por_servicio = {}
        for r in lista:
            if _llave(r) in faltan:
                por_servicio[r["service"]] = por_servicio.get(r["service"], 0) + 1
        resumen = ", ".join(f"{s}={n}" for s, n in sorted(por_servicio.items(), key=lambda x: -x[1]))
        sev = "media" if len(faltan) > len(lista) / 2 else "baja"
        fuera = sin_lectura.get(account_id, 0)
        nota = f" {fuera} recursos de servicios cuyos tags no se leen quedan fuera del conteo." if fuera else ""
        out.append(hallazgo("inventario", sev, account_id, None, None,
                            f"{len(faltan)} de {len(lista)} recursos sin tag de owner/environment",
                            f"Sin owner: {len(sin_owner)} · sin environment: {len(sin_env)}. Por servicio: {resumen}.{nota}",
                            None, FUENTE))
    return out


def regla_root(recursos, ctx):
    """Usuario root (de colectores/iam.py). region=None: es global, y así se deduplica con aws-doctor."""
    out = []
    for r in recursos:
        if not _es(r, {"iam"}, "account_summary"):
            continue
        a = _attrs(r)
        if a.get("access_keys_present"):
            out.append(hallazgo("riesgo", "alta", r["account_id"], None, "root-access-keys",
                                "Usuario root con access keys",
                                "La cuenta tiene access keys del root: credenciales permanentes sin límite de permisos. "
                                "Revisa su último uso y bórralas.", None, FUENTE, rule_id="regla.root_access_keys"))
        if a.get("password_present") and not a.get("mfa_enabled"):
            out.append(hallazgo("riesgo", "alta", r["account_id"], None, "root", "Usuario root sin MFA",
                                "El root tiene password y no tiene MFA.", None, FUENTE, rule_id="regla.root_sin_mfa"))
    return out


REGLAS = [
    regla_ebs_sin_adjuntar,
    regla_eip_sin_asociar,
    regla_snapshots_viejos,
    regla_sg_abierto,
    regla_rds_publico,
    regla_rds_sin_cifrar,
    regla_ec2_detenida,
    regla_gp2,
    regla_opensearch_abierto,
    regla_s3_sin_bloqueo_publico,
    regla_sin_tags,
    regla_root,
]


def aplicar(inventory):
    """Corre todas las reglas. La fecha de referencia es la del inventario (reproducible)."""
    ahora = parsear_fecha(inventory.get("generated_at")) or datetime.now(timezone.utc)
    ctx = {"ahora": ahora}
    recursos = inventory.get("resources", [])
    findings = []
    for regla in REGLAS:
        for f in regla(recursos, ctx):
            f.setdefault("rule_id", "regla." + regla.__name__.removeprefix("regla_"))
            findings.append(f)
    return findings
