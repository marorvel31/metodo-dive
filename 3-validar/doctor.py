"""Fuente a) de la fase 3: aws-doctor (https://github.com/elC0mpa/aws-doctor).

Corre `aws-doctor waste --output json --region <r> --profile dive-<cuenta>` por cada
cuenta × región con costo en discovery.json, guarda la salida cruda y la convierte en findings.
Si la salida no es JSON (versiones viejas, o un error), se parsea la tabla de texto.

Uso: lo llama validar.py; no se ejecuta solo.
"""

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import error_corto, redactar
from hallazgos import hallazgo, severidad_por_costo

BINARIO = "aws-doctor"
FUENTE = "aws-doctor"
MAX_EJEMPLOS = 15  # IDs que se listan en el detalle de un finding agrupado

# clave del JSON de aws-doctor → cómo convertir cada elemento.
#   id: campos candidatos para resource_id · costo: campos candidatos para $/mes
#   agrupar: True = un finding por cuenta/región con la lista de IDs (evita cientos de filas)
#   global_: True = el recurso no es regional (S3), se agrupa por cuenta con region None
CATEGORIAS = {
    "unused_ebs_volumes": dict(titulo="Volumen EBS sin adjuntar", cat="desperdicio", sev="media",
                               id=["volume_id"], costo=["estimated_monthly_cost"]),
    "stopped_instance_volumes": dict(titulo="Volumen EBS de instancia detenida", cat="desperdicio", sev="baja",
                                     id=["volume_id"], costo=["estimated_monthly_cost"]),
    "unused_elastic_ips": dict(titulo="Elastic IP sin asociar", cat="desperdicio", sev="media",
                               id=["allocation_id", "public_ip"], costo=["estimated_monthly_cost"]),
    "stopped_instances": dict(titulo="Instancia EC2 detenida hace más de 30 días", cat="desperdicio", sev="media",
                              id=["instance_id"], costo=[]),
    "reserved_instances": dict(titulo="Reserved Instance por vencer o vencida", cat="costo", sev="media",
                               id=["reserved_instance_id"], costo=[]),
    "unused_load_balancers": dict(titulo="Load balancer sin targets", cat="desperdicio", sev="media",
                                  id=["name", "arn"], costo=["estimated_monthly_cost"]),
    "unused_amis": dict(titulo="AMI sin uso (verificar ASG/launch templates)", cat="desperdicio", sev="baja",
                        id=["image_id"], costo=["max_potential_saving_monthly"]),
    "orphaned_snapshots": dict(titulo="Snapshot EBS huérfano (volumen borrado)", cat="desperdicio", sev="baja",
                               id=["snapshot_id"], costo=["max_potential_savings"]),
    "stale_snapshots": dict(titulo="Snapshot EBS viejo", cat="desperdicio", sev="baja",
                            id=["snapshot_id"], costo=["max_potential_savings"]),
    "unused_key_pairs": dict(titulo="Key pairs EC2 sin uso", cat="riesgo", sev="baja",
                             id=["key_pair_id", "key_name"], costo=[], agrupar=True),
    "s3_buckets_without_lifecycle": dict(titulo="Buckets S3 sin lifecycle policy", cat="costo", sev="baja",
                                         id=["bucket_name"], costo=[], agrupar=True, global_=True),
    "s3_buckets_with_incomplete_multipart_uploads": dict(titulo="Buckets S3 con multipart uploads incompletos",
                                                         cat="desperdicio", sev="baja", id=["bucket_name"],
                                                         costo=[], agrupar=True, global_=True),
    "cloudwatch_log_groups_without_retention_policy": dict(titulo="Log groups sin retención", cat="costo",
                                                           sev="baja", id=["log_group_name"],
                                                           costo=["estimated_monthly_cost"], agrupar=True),
    "stopped_rds_instances": dict(titulo="Instancia RDS detenida", cat="desperdicio", sev="media",
                                  id=["db_instance_id"], costo=["estimated_monthly_cost"]),
    "old_rds_snapshots": dict(titulo="Snapshot manual RDS viejo", cat="desperdicio", sev="baja",
                              id=["db_snapshot_id"], costo=["estimated_monthly_cost"]),
    "idle_rds_instances": dict(titulo="Instancia RDS sin conexiones", cat="desperdicio", sev="media",
                               id=["db_instance_id"], costo=["estimated_monthly_cost"]),
    # Categorías de aws-doctor 2.x (claves de model/output_json.go, v2.21.1).
    "idle_load_balancers": dict(titulo="Load balancer sin tráfico", cat="desperdicio", sev="media",
                                id=["name", "arn"], costo=["estimated_monthly_cost"]),
    "idle_nat_gateways": dict(titulo="NAT Gateway sin tráfico", cat="desperdicio", sev="media",
                              id=["nat_gateway_id"], costo=["estimated_monthly_cost"]),
    "idle_ec2_instances": dict(titulo="Instancia EC2 ociosa (CPU y red bajas)", cat="desperdicio", sev="media",
                               id=["instance_id"], costo=["estimated_monthly_cost"]),
    "idle_sagemaker_endpoints": dict(titulo="Endpoint SageMaker sin invocaciones", cat="desperdicio", sev="media",
                                     id=["endpoint_name", "endpoint_arn"], costo=["estimated_monthly_cost"]),
    "over_provisioned_lambdas": dict(titulo="Lambdas con memoria sobredimensionada", cat="costo", sev="baja",
                                     id=["function_name"], costo=[], agrupar=True),
    "ecr_untagged_images": dict(titulo="Repositorios ECR con imágenes sin tag", cat="desperdicio", sev="baja",
                                id=["repository_name"], costo=["estimated_monthly_cost"], agrupar=True),
    "ecr_no_lifecycle_policies": dict(titulo="Repositorios ECR sin lifecycle policy", cat="costo", sev="baja",
                                      id=["repository_name"], costo=[], agrupar=True),
    "ecr_empty_repositories": dict(titulo="Repositorios ECR vacíos", cat="inventario", sev="baja",
                                   id=["repository_name"], costo=[], agrupar=True),
    "unused_secrets": dict(titulo="Secretos sin acceso reciente", cat="desperdicio", sev="baja",
                           id=["name"], costo=["estimated_monthly_cost"], agrupar=True),
    # IAM es global: aws-doctor lo repite en cada región consultada; global_ lo deja uno por cuenta.
    "unused_iam_users": dict(titulo="Usuarios IAM sin uso", cat="riesgo", sev="media",
                             id=["user_name"], costo=[], agrupar=True, global_=True),
    "root_user_waste": dict(titulo="Usuario root sin MFA", cat="riesgo", sev="alta", id=[], costo=[],
                            global_=True, id_fijo="root", incluir=lambda el: el.get("has_mfa") is False),
}

# Claves del JSON de `waste` que no son categorías de desperdicio.
_CLAVES_NO_CATEGORIA = {"account_id", "generated_at", "has_waste", "total_estimated_monthly_cost", "errors",
                        "summary"}

# Campos del JSON que no aportan al detalle (ya son id o costo).
_OMITIR_EN_DETALLE = {"estimated_monthly_cost", "max_potential_saving_monthly", "max_potential_savings",
                      "snapshot_ids", "description", "safety_warning"}

# Salida de texto: (palabra en el título de la tabla, palabra en el STATUS) → clave de CATEGORIAS.
# Se evalúan en orden; "" en el status significa "cualquiera".
_TEXTO_A_CLAVE = [
    ("EBS Volume", "Available", "unused_ebs_volumes"),
    ("EBS Volume", "Stopped", "stopped_instance_volumes"),
    ("EBS Volume", "", "unused_ebs_volumes"),
    ("Elastic IP", "", "unused_elastic_ips"),
    ("EC2", "Stopped", "stopped_instances"),
    ("EC2", "", "reserved_instances"),
    ("Load Balancer", "", "unused_load_balancers"),
    ("AMI", "", "unused_amis"),
    ("Snapshot", "Orphan", "orphaned_snapshots"),
    ("EBS Snapshot", "", "stale_snapshots"),
    ("Key Pair", "", "unused_key_pairs"),
    ("S3", "Multipart", "s3_buckets_with_incomplete_multipart_uploads"),
    ("S3", "", "s3_buckets_without_lifecycle"),
    ("CloudWatch", "", "cloudwatch_log_groups_without_retention_policy"),
    ("RDS", "Idle", "idle_rds_instances"),
    ("RDS", "Snapshot", "old_rds_snapshots"),
    ("RDS", "Stopped", "stopped_rds_instances"),
    ("RDS", "", "stopped_rds_instances"),
]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ID_AWS = re.compile(r"^(vol|i|eipalloc|snap|ami|key|sg|eni)-[0-9a-f]+$|^arn:")
_DOLARES = re.compile(r"\$\s*([0-9][0-9,]*\.?[0-9]*)")


def disponible():
    return shutil.which(BINARIO) is not None


def version():
    """Primera línea de `aws-doctor version` (ej. "aws-doctor version 2.21.1"); None si no está o falla."""
    if not disponible():
        return None
    try:
        r = subprocess.run([BINARIO, "version"], capture_output=True, text=True, timeout=10)
        return (r.stdout.strip().splitlines() or [None])[0]
    except (OSError, subprocess.SubprocessError):
        return None


def comando(account_id, region, access_mode):
    cmd = [BINARIO, "waste", "--output", "json", "--region", region]
    if access_mode == "sso":
        cmd += ["--profile", f"dive-{account_id}"]
    return cmd


# ---------- parseo ----------

def _numero(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _dolares(texto):
    m = _DOLARES.search(texto or "")
    return float(m.group(1).replace(",", "")) if m else None


def items_desde_json(datos):
    """JSON de `waste --output json` → [{clave, id, usd, info}]."""
    items = []
    for clave, conf in CATEGORIAS.items():
        lista = datos.get(clave) or []
        if not isinstance(lista, list):
            continue  # forma inesperada: avisos_desde_json lo reporta
        for el in lista:
            if not isinstance(el, dict):
                continue
            if "incluir" in conf and not conf["incluir"](el):
                continue
            rid = conf.get("id_fijo") or next((el.get(c) for c in conf["id"] if el.get(c)), None)
            usd = next((_numero(el.get(c)) for c in conf["costo"] if _numero(el.get(c)) is not None), None)
            info = ", ".join(f"{k}={v}" for k, v in el.items()
                             if k not in _OMITIR_EN_DETALLE and k not in conf["id"] and v not in (None, "", [], False))
            items.append({"clave": clave, "id": rid, "usd": usd, "info": info})
    return items


def avisos_desde_json(datos):
    """Lo que aws-doctor reportó y no se convierte en findings: checks que fallaron (`errors`) y
    categorías que este parser no conoce (una versión nueva de aws-doctor). Devuelve [(colector, texto)]."""
    errores = datos.get("errors") or {}
    if not isinstance(errores, dict):  # otra forma en una versión futura: se avisa sin romper la fase
        errores = {"errors": errores}
    avisos = [(f"{FUENTE}/{redactar(check, 60)}", redactar(msg)) for check, msg in sorted(errores.items())]
    for clave in CATEGORIAS:
        if datos.get(clave) and not isinstance(datos[clave], list):
            avisos.append((FUENTE, f"forma inesperada en {clave}: se esperaba una lista; revisa la versión de aws-doctor"))
    for el in datos.get("root_user_waste") or []:
        if isinstance(el, dict) and not isinstance(el.get("has_mfa"), bool):
            avisos.append((FUENTE, "root_user_waste sin has_mfa booleano: no se pudo evaluar el MFA del root"))
    for clave, valor in sorted(datos.items()):
        if clave in CATEGORIAS or clave in _CLAVES_NO_CATEGORIA or not valor:
            continue
        n = len(valor) if isinstance(valor, list) else 1
        avisos.append((FUENTE, f"categoría desconocida de aws-doctor: {redactar(clave, 80)} ({n}); "
                               "actualiza CATEGORIAS en doctor.py"))
    return avisos


def _clave_texto(titulo, status):
    for palabra_titulo, palabra_status, clave in _TEXTO_A_CLAVE:
        if palabra_titulo.lower() in titulo.lower() and palabra_status.lower() in status.lower():
            return clave
    return None


def tablas_desde_texto(texto):
    """Parsea las tablas con bordes de la salida `table`. Devuelve [{titulo, cabecera, grupos}].

    En aws-doctor la columna STATUS es una celda combinada: el texto aparece en una sola fila
    del grupo (la del medio) y los grupos se separan con una línea ├───┤.
    """
    tablas, actual, grupo = [], None, []
    for cruda in texto.splitlines():
        linea = _ANSI.sub("", cruda).strip()
        if linea.startswith("╭"):
            actual, grupo = {"titulo": None, "cabecera": None, "grupos": []}, []
        elif actual is None:
            continue
        elif linea.startswith("│"):
            celdas = [c.strip() for c in linea.strip("│").split("│")]
            if actual["titulo"] is None:
                actual["titulo"] = celdas[0]
            elif actual["cabecera"] is None:
                actual["cabecera"] = celdas
            else:
                grupo.append(celdas)
        elif linea.startswith("├") or linea.startswith("╰"):
            if grupo:
                actual["grupos"].append(grupo)
                grupo = []
            if linea.startswith("╰"):
                tablas.append(actual)
                actual = None
    return tablas


def items_desde_texto(texto):
    """Salida `table` → [{clave, id, usd, info}], misma forma que items_desde_json."""
    items = []
    for t in tablas_desde_texto(texto):
        if not t["titulo"] or not t["cabecera"] or "summary" in t["titulo"].lower():
            continue
        cab = t["cabecera"]
        cols_costo = [i for i, c in enumerate(cab) if "COST" in c.upper() or "SAVING" in c.upper()]
        for grupo in t["grupos"]:
            status = next((f[0] for f in grupo if f and f[0]), "")
            clave = _clave_texto(t["titulo"], status)
            if not clave:
                continue
            for fila in grupo:
                if len(fila) < 2:
                    continue
                # Preferimos una celda con forma de ID de AWS (ej. eipalloc- sobre la IP); si no, la 2a columna.
                rid = next((c for c in fila[1:] if _ID_AWS.match(c)), fila[1])
                usd = next((_dolares(fila[i]) for i in cols_costo if i < len(fila) and _dolares(fila[i]) is not None), None)
                info = ", ".join(f"{cab[i].lower()}={fila[i]}" for i in range(1, min(len(cab), len(fila)))
                                 if fila[i] and fila[i] != rid and i not in cols_costo)
                items.append({"clave": clave, "id": rid, "usd": usd, "info": f"{status}; {info}" if status else info})
    return items


def _cargar_json(texto):
    texto = texto or ""
    inicio = texto.find("{")
    if inicio >= 0:
        try:
            return json.loads(texto[inicio:])
        except ValueError:
            pass
    return None


def parsear_salida(texto):
    """Devuelve (items, formato). Intenta JSON; si no, tabla de texto."""
    datos = _cargar_json(texto)
    if isinstance(datos, dict):
        return items_desde_json(datos), "json"
    return items_desde_texto(texto or ""), "texto"


# ---------- items → findings ----------

def items_a_findings(items_por_cuenta_region):
    """{(account_id, region): [items]} → findings, deduplicando y agrupando las categorías ruidosas."""
    vistos, agrupados, findings = set(), {}, []
    for (account_id, region), items in sorted(items_por_cuenta_region.items()):
        for it in items:
            conf = CATEGORIAS[it["clave"]]
            reg = None if conf.get("global_") else region
            llave = (account_id, it["clave"], reg, it["id"])
            if llave in vistos:  # S3 aparece repetido en cada región consultada
                continue
            vistos.add(llave)
            if conf.get("agrupar"):
                agrupados.setdefault((account_id, reg, it["clave"]), []).append(it)
                continue
            usd = it["usd"]
            findings.append(hallazgo(conf["cat"], severidad_por_costo(usd, conf["sev"]), account_id, reg, it["id"],
                                     conf["titulo"], it["info"] or conf["titulo"], usd, FUENTE,
                                     rule_id=f"{FUENTE}.{it['clave']}"))

    for (account_id, reg, clave), grupo in agrupados.items():
        conf = CATEGORIAS[clave]
        costos = [i["usd"] for i in grupo if i["usd"] is not None]
        usd = sum(costos) if costos else None
        ids = [str(i["id"]) for i in grupo]
        extra = f" (+{len(ids) - MAX_EJEMPLOS} más)" if len(ids) > MAX_EJEMPLOS else ""
        findings.append(hallazgo(conf["cat"], severidad_por_costo(usd, conf["sev"]), account_id, reg, None,
                                 f"{conf['titulo']} ({len(ids)})",
                                 f"{len(ids)} recursos: " + ", ".join(ids[:MAX_EJEMPLOS]) + extra, usd, FUENTE,
                                 rule_id=f"{FUENTE}.{clave}"))
    return findings


# ---------- ejecución ----------

def _entorno(cfg, account_id, env_base, creds_fn):
    env = dict(env_base)
    env["NO_COLOR"] = "1"
    if cfg["access_mode"] != "sso":
        # assume_role: pasamos credenciales temporales por variables de entorno, sin perfil.
        c = creds_fn(account_id)
        env.pop("AWS_PROFILE", None)
        env.update(AWS_ACCESS_KEY_ID=c.access_key, AWS_SECRET_ACCESS_KEY=c.secret_key, AWS_SESSION_TOKEN=c.token)
    return env


def correr_uno(cfg, account_id, region, carpeta, env, timeout):
    """Ejecuta aws-doctor para una cuenta × región y guarda la salida cruda.
    Devuelve (items, error, avisos); avisos = [(colector, texto)] de avisos_desde_json."""
    cmd = comando(account_id, region, cfg["access_mode"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], f"timeout ({timeout}s)", []
    except OSError as e:
        return [], error_corto(e), []

    items, formato = parsear_salida(proc.stdout)
    avisos = avisos_desde_json(_cargar_json(proc.stdout)) if formato == "json" else []
    extension = "json" if formato == "json" else "txt"
    crudo = proc.stdout if proc.returncode == 0 else f"{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    (carpeta / f"{account_id}_{region}.{extension}").write_text(crudo)
    if proc.returncode != 0:
        return items, f"exit {proc.returncode}: {redactar(proc.stderr or proc.stdout)}", avisos
    return items, None, avisos


def ejecutar(cfg, discovery, salida, timeout=900, creds_fn=None, habilitadas=None):
    """Corre aws-doctor en paralelo sobre cada cuenta × región con costo. Devuelve (findings, errores).

    `habilitadas` = {account_id: [regiones]} (de inventory.json): las regiones no habilitadas se omiten,
    porque ahí aws-doctor solo agota reintentos. Una cuenta sin lista no se filtra.
    """
    habilitadas = habilitadas or {}
    if not disponible():
        print("⚠ aws-doctor no está instalado (brew install elC0mpa/homebrew-tap/aws-doctor); sigo sin él.")
        return [], [{"account_id": None, "region": None, "collector": FUENTE, "error": "aws-doctor no instalado"}]

    env_base = dict(os.environ)
    if cfg["access_mode"] == "sso":
        from common.aws import DIVE_AWS_CONFIG
        if not DIVE_AWS_CONFIG.exists():
            print(f"⚠ No existe {DIVE_AWS_CONFIG}; corre setup/generar_perfiles.py. Sigo sin aws-doctor.")
            return [], [{"account_id": None, "region": None, "collector": FUENTE, "error": "faltan perfiles dive-*"}]
        env_base["AWS_CONFIG_FILE"] = str(DIVE_AWS_CONFIG)

    carpeta = salida / "aws-doctor"
    carpeta.mkdir(parents=True, exist_ok=True)
    tareas = [(a, r) for a, c in discovery["accounts"].items() for r in c.get("regions", {})
              if r != "global" and (a not in habilitadas or r in habilitadas[a])]
    print(f"aws-doctor: {len(tareas)} corridas (cuenta × región)")

    items, errores = {}, []
    with ThreadPoolExecutor(max_workers=cfg["max_workers"]) as pool:
        futuros = {}
        for account_id, region in tareas:
            try:
                env = _entorno(cfg, account_id, env_base, creds_fn)
            except Exception as e:  # noqa: BLE001 — una cuenta sin acceso no detiene la corrida
                errores.append({"account_id": account_id, "region": region, "collector": FUENTE,
                                "error": error_corto(e)})
                continue
            futuros[pool.submit(correr_uno, cfg, account_id, region, carpeta, env, timeout)] = (account_id, region)
        avisos_vistos = set()
        for i, fut in enumerate(as_completed(futuros), 1):
            account_id, region = futuros[fut]
            try:
                encontrados, error, avisos = fut.result()
            except Exception as e:  # noqa: BLE001 — una salida inesperada de aws-doctor no tumba la fase
                encontrados, error, avisos = [], f"no se pudo procesar la salida: {error_corto(e)}", []
            items[(account_id, region)] = encontrados
            if error:
                errores.append({"account_id": account_id, "region": region, "collector": FUENTE, "error": error})
            for colector, texto in avisos:
                # Una categoría desconocida se avisa una sola vez; un check fallido, por cuenta × región.
                llave = texto if texto.startswith("categoría desconocida") else (account_id, region, colector, texto)
                if llave in avisos_vistos:
                    continue
                avisos_vistos.add(llave)
                errores.append({"account_id": account_id, "region": region, "collector": colector, "error": texto})
            if i % 10 == 0 or i == len(futuros):
                print(f"  {i}/{len(futuros)} corridas · {len(errores)} errores")
    return items_a_findings(items), errores
