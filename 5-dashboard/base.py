"""Base SQLite del dashboard: ingiere las corridas (salida/<run_id>/) y responde las consultas de la UI.

Las corridas son de solo lectura; lo único que el usuario edita es `estado` (aceptado, ignorado...), que se
guarda por organización + fingerprint y sobrevive a cualquier corrida nueva. Sin dependencias: sqlite3.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ESTADOS = ("abierto", "aceptado", "ignorado", "resuelto")
MAX_NOTA = 2000

ESQUEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, org_alias TEXT, carpeta TEXT NOT NULL,
    generated_at TEXT, currency TEXT, sin_doctor INTEGER, dive_version TEXT, n_errores INTEGER
);
CREATE TABLE IF NOT EXISTS cuentas (run_id TEXT, account_id TEXT, name TEXT, PRIMARY KEY (run_id, account_id));
CREATE TABLE IF NOT EXISTS costos (run_id TEXT, account_id TEXT, region TEXT, service TEXT, month TEXT, usd REAL);
CREATE TABLE IF NOT EXISTS usos (run_id TEXT, account_id TEXT, region TEXT, usage_type TEXT, month TEXT, usd REAL);
CREATE TABLE IF NOT EXISTS findings (
    run_id TEXT, fingerprint TEXT, id TEXT, category TEXT, severity TEXT, account_id TEXT, region TEXT,
    resource_id TEXT, title TEXT, detail TEXT, monthly_usd REAL, source TEXT, rule_id TEXT, issue_id TEXT,
    price_source TEXT, is_saving INTEGER, PRIMARY KEY (run_id, fingerprint)
);
-- (cuenta, región) donde no se pudo verificar en esa corrida: aws-doctor falló, o la región no estaba habilitada.
CREATE TABLE IF NOT EXISTS sin_verificar (run_id TEXT, account_id TEXT, region TEXT, motivo TEXT);
CREATE TABLE IF NOT EXISTS uso_total (run_id TEXT, account_id TEXT, month TEXT, usd REAL);
CREATE TABLE IF NOT EXISTS cargos (run_id TEXT, account_id TEXT, record_type TEXT, month TEXT, usd REAL);
CREATE TABLE IF NOT EXISTS recursos (
    run_id TEXT, account_id TEXT, region TEXT, service TEXT, type TEXT, id TEXT, arn TEXT, name TEXT, state TEXT,
    tags TEXT, attrs TEXT, relations TEXT
);
CREATE INDEX IF NOT EXISTS i_recursos_run ON recursos (run_id, account_id);
CREATE TABLE IF NOT EXISTS estado (
    org_id TEXT, fingerprint TEXT, status TEXT NOT NULL, nota TEXT, updated_at TEXT,
    PRIMARY KEY (org_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS i_findings_fp ON findings (fingerprint);
CREATE INDEX IF NOT EXISTS i_costos_run ON costos (run_id);
CREATE INDEX IF NOT EXISTS i_usos_run ON usos (run_id);
"""


VERSION_ESQUEMA = 4  # 2: recursos; 3: cargos; 4: uso_total. Al subirla, las corridas se reingieren (el estado
TABLAS_CORRIDA = ("runs", "cuentas", "costos", "usos", "findings", "sin_verificar", "recursos", "cargos", "uso_total")
# del usuario, tabla estado, se conserva).


def conectar(ruta):
    """Una conexión por uso (el servidor es multihilo). El archivo hereda el umask 077 de common."""
    con = sqlite3.connect(ruta, timeout=10)
    con.row_factory = sqlite3.Row
    if con.execute("PRAGMA user_version").fetchone()[0] < VERSION_ESQUEMA:
        # Las tablas de corridas se derivan de los JSON: se recrean (así un cambio de columnas no rompe la
        # ingesta). estado, lo único que escribe el usuario, no se toca.
        for tabla in TABLAS_CORRIDA:
            con.execute(f"DROP TABLE IF EXISTS {tabla}")
        con.executescript(ESQUEMA)
        con.execute(f"PRAGMA user_version = {VERSION_ESQUEMA}")
        con.commit()
    con.executescript(ESQUEMA)
    return con


def _leer(ruta):
    try:
        return json.loads(Path(ruta).read_text())
    except (OSError, ValueError):
        return None


def ingerir(con, carpetas_base):
    """Agrega las corridas nuevas de cada output_dir. Idempotente por run_id. Devuelve (nuevas, omitidas):
    omitidas = carpetas con findings.json pero sin bloque `run` (corridas anteriores al contrato v2)."""
    ya = {r[0]: r[1] for r in con.execute("SELECT run_id, generated_at FROM runs")}
    nuevas, omitidas = [], []
    for base in carpetas_base:
        for carpeta in sorted(p for p in Path(base).iterdir() if p.is_dir() and not p.name.endswith("-anon")):
            findings = _leer(carpeta / "findings.json")
            if findings is None:
                continue
            run = findings.get("run") if isinstance(findings, dict) else None
            if not isinstance(run, dict) or not run.get("run_id") or not run.get("organization_id"):
                omitidas.append(carpeta.name)
                continue
            rid = run["run_id"]
            otra = con.execute("SELECT org_id FROM runs WHERE run_id = ?", (rid,)).fetchone()
            if otra and otra[0] != run["organization_id"]:
                omitidas.append(f"{carpeta.name} (mismo run_id que otra organización)")
                continue
            if rid in ya and ya[rid] == findings.get("generated_at"):
                continue
            # Nueva, o re-validada (otro generated_at, ej. validar con aws-doctor tras un --sin-doctor): se rehace
            # entera. El estado del usuario no se toca: va por fingerprint.
            try:
                con.execute("SAVEPOINT corrida")
                for tabla in TABLAS_CORRIDA:
                    con.execute(f"DELETE FROM {tabla} WHERE run_id = ?", (rid,))
                _ingerir_corrida(con, carpeta, run, findings, _dict(_leer(carpeta / "discovery.json")),
                                 _dict(_leer(carpeta / "inventory.json")))
                con.execute("RELEASE corrida")
            except (AttributeError, TypeError, KeyError, ValueError, sqlite3.Error):
                con.execute("ROLLBACK TO corrida")
                con.execute("RELEASE corrida")
                omitidas.append(carpeta.name)
                continue
            ya[rid] = findings.get("generated_at")
            nuevas.append(rid)
    con.commit()
    return nuevas, omitidas


def _dict(x):
    return x if isinstance(x, dict) else {}


def _ingerir_corrida(con, carpeta, run, findings, discovery, inventory):
    rid = run["run_id"]
    validar = (run.get("flags") or {}).get("validar") or {}
    errores = [e for e in findings.get("errors") or [] if isinstance(e, dict)]
    # aws-doctor no instalado o sin perfiles: un error sin cuenta. Para la historia es igual que --sin-doctor.
    sin_doctor = bool(validar.get("sin_doctor")) or any(
        e.get("collector") == "aws-doctor" and not e.get("account_id") for e in errores)
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, run["organization_id"], run.get("org_alias"), str(carpeta), findings.get("generated_at"),
                 run.get("currency"), int(sin_doctor), run.get("dive_version"), len(errores)))
    # Todas las cuentas que aparecen: con costo (discovery), solo inventario o solo cargos (ej. la que paga el SP).
    nombres = {a: (d or {}).get("name") for a, d in _dict(inventory.get("accounts")).items()}
    nombres.update({a: (d or {}).get("name") for a, d in _dict(discovery.get("accounts")).items()})
    for a in list(_dict(discovery.get("charges_monthly_usd"))) + [r.get("account_id") for r in inventory.get("resources") or []
                                                                    if isinstance(r, dict)]:
        if a:
            nombres.setdefault(a, None)
    for acc, nombre in nombres.items():
        con.execute("INSERT INTO cuentas VALUES (?,?,?)", (rid, acc, nombre))
    for acc, meses in _dict(discovery.get("usage_total_monthly_usd")).items():
        con.executemany("INSERT INTO uso_total VALUES (?,?,?,?)", [(rid, acc, m, v) for m, v in _dict(meses).items()])
    for acc, d in (discovery.get("accounts") or {}).items():
        for region, r in ((d or {}).get("regions") or {}).items():
            for servicio, meses in ((r or {}).get("services_monthly_usd") or {}).items():
                con.executemany("INSERT INTO costos VALUES (?,?,?,?,?,?)",
                                [(rid, acc, region, servicio, m, v) for m, v in (meses or {}).items()])
            for tipo, meses in ((r or {}).get("usage_types_monthly_usd") or {}).items():
                con.executemany("INSERT INTO usos VALUES (?,?,?,?,?,?)",
                                [(rid, acc, region, tipo, m, v) for m, v in (meses or {}).items()])
    for acc, tipos in _dict(discovery.get("charges_monthly_usd")).items():
        for tipo, meses in _dict(tipos).items():
            con.executemany("INSERT INTO cargos VALUES (?,?,?,?,?)",
                            [(rid, acc, tipo, m, v) for m, v in _dict(meses).items()])
    for f in findings.get("findings") or []:
        if not isinstance(f, dict) or not f.get("fingerprint"):
            continue
        con.execute("INSERT OR IGNORE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, f["fingerprint"], f.get("id"), f.get("category"), f.get("severity"), f.get("account_id"),
                     f.get("region"), f.get("resource_id"), f.get("title"), f.get("detail"), f.get("monthly_usd"),
                     f.get("source"), f.get("rule_id"), f.get("issue_id"), f.get("price_source"),
                     int(bool(f.get("is_saving")))))
    # Una corrida de aws-doctor que falló entera en una cuenta × región: sus findings ahí no se pudieron ver.
    for e in errores:
        colector = str(e.get("collector") or "")
        if colector.startswith("aws-doctor") and e.get("account_id"):
            # "aws-doctor" = la corrida entera falló; "aws-doctor/<check>" = falló un check (ej. Lambda).
            check = colector.partition("/")[2].lower()
            con.execute("INSERT INTO sin_verificar VALUES (?,?,?,?)",
                        (rid, e["account_id"], e.get("region"), "aws-doctor falló" + (f":{check}" if check else "")))
    for e in _dict(discovery).get("errors") or []:
        if isinstance(e, dict) and e.get("region"):  # Cost Explorer falló en una región: el cruce no vio nada ahí
            con.execute("INSERT INTO sin_verificar VALUES (?,?,?,?)", (rid, "*", e["region"], "costos falló"))
    for r in inventory.get("resources") or []:
        if isinstance(r, dict) and r.get("id") is not None:
            con.execute("INSERT INTO recursos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, r.get("account_id"), r.get("region"), r.get("service"), r.get("type"), str(r.get("id")),
                         r.get("arn"), r.get("name"), r.get("state"),
                         json.dumps(r.get("tags") or {}, ensure_ascii=False, default=str),
                         json.dumps(r.get("attrs") or {}, ensure_ascii=False, default=str),
                         json.dumps(r.get("relations") or {}, ensure_ascii=False, default=str)))
    for s in inventory.get("skipped") or []:
        if isinstance(s, dict):
            con.execute("INSERT INTO sin_verificar VALUES (?,?,?,?)",
                        (rid, s.get("account_id"), s.get("region"), "región no habilitada"))
    # Fallos del inventario: sin acceso a la cuenta (todo), o un colector caído en una cuenta × región.
    for e in inventory.get("errors") or []:
        if not isinstance(e, dict) or not e.get("account_id"):
            continue
        if e.get("collector") in ("acceso", "regiones") or not e.get("region"):
            con.execute("INSERT INTO sin_verificar VALUES (?,?,?,?)", (rid, e["account_id"], None, "cuenta sin acceso"))
        else:
            con.execute("INSERT INTO sin_verificar VALUES (?,?,?,?)",
                        (rid, e["account_id"], e["region"], "colector falló"))


# ---------- consultas ----------

def orgs(con):
    filas = con.execute("""SELECT org_id, COUNT(*) n, MAX(run_id) ultima,
                                  (SELECT org_alias FROM runs r2 WHERE r2.org_id = r.org_id ORDER BY run_id DESC LIMIT 1) alias
                           FROM runs r GROUP BY org_id ORDER BY alias""").fetchall()
    return [dict(f) for f in filas]


def corridas(con, org_id, cuenta=None):
    filas = con.execute("""SELECT r.run_id, r.org_alias, r.generated_at, r.currency, r.sin_doctor, r.dive_version,
                                  r.n_errores, COUNT(f.fingerprint) n_findings,
                                  ROUND(COALESCE(SUM(CASE WHEN f.is_saving THEN f.monthly_usd END), 0), 2) ahorro
                           FROM runs r LEFT JOIN findings f ON f.run_id = r.run_id AND (? IS NULL OR f.account_id = ?)
                           WHERE r.org_id = ? GROUP BY r.run_id ORDER BY r.run_id""",
                        (cuenta, cuenta, org_id)).fetchall()
    return [dict(f) for f in filas]


def _run_ids(con, org_id):
    return [r[0] for r in con.execute("SELECT run_id FROM runs WHERE org_id = ? ORDER BY run_id", (org_id,))]


# Check de aws-doctor (como lo nombra en `errors`) → fragmentos de las categorías que cubre (rule_id
# aws-doctor.<categoría>). Un check desconocido bloquea todas las categorías de aws-doctor (conservador).
CHECKS_DOCTOR = {
    "ec2": ("ebs", "elastic_ips", "instance", "ami", "snapshot", "key_pairs", "reserved"),
    "rds": ("rds",), "elb": ("load_balancers",), "vpc": ("nat",), "iam": ("iam", "root"),
    "cloudwatch": ("log_groups",), "s3": ("s3",), "lambda": ("lambda",), "ecr": ("ecr",),
    "sagemaker": ("sagemaker",), "secrets-manager": ("secrets",), "secretsmanager": ("secrets",),
}
REGIONES_GLOBALES = (None, "global", "us-east-1")  # los colectores GLOBAL corren en us-east-1


def _misma_region(reg_fallo, reg_finding):
    """Un fallo en us-east-1/global también tapa los findings sin región (IAM, root, S3, CloudFront)."""
    if reg_fallo == reg_finding:
        return True
    return reg_finding in (None, "global") and reg_fallo in REGIONES_GLOBALES


def _cubre_check(check, rule_id):
    partes = CHECKS_DOCTOR.get(check)
    return partes is None or any(p in (rule_id or "") for p in partes)


def _verificable(sin_verif, sin_doctor, f):
    """¿Una corrida podía ver el finding f? Si no, que falte no significa que se resolvió.
    sin_verif = [(cuenta, región, motivo)] de esa corrida."""
    doctor = f["source"] == "aws-doctor"
    if doctor and sin_doctor:
        return False
    for acc, reg, motivo in sin_verif:
        if acc == "*":  # Cost Explorer falló en la región: el cruce billing ↔ inventario no pudo verla
            if f["source"] == "cruce-billing" and _misma_region(reg, f["region"]):
                return False
            continue
        if acc != f["account_id"]:
            continue
        if motivo == "cuenta sin acceso":
            return False
        if motivo.startswith("aws-doctor falló"):
            check = motivo.partition(":")[2]
            if doctor and (reg is None or _misma_region(reg, f["region"])) and (not check or _cubre_check(check, f["rule_id"])):
                return False
        elif _misma_region(reg, f["region"]):
            return False  # región no habilitada o colector caído: nada de esa región se pudo ver
    return True


def hallazgos(con, org_id, run_id):
    """Findings de la corrida + los que estaban antes y ya no, con su historia y el estado del usuario.
    presencia: presente | resuelto (la corrida podía verlo y no está) | no_verificado."""
    runs = _run_ids(con, org_id)
    if run_id not in runs:
        return []
    previas = runs[:runs.index(run_id) + 1]
    marcas = ",".join("?" * len(previas))
    historia = {}
    for fila in con.execute(f"""SELECT fingerprint, MIN(run_id) primera, MAX(run_id) ultima, COUNT(*) veces
                                FROM findings WHERE run_id IN ({marcas}) GROUP BY fingerprint""", previas):
        historia[fila["fingerprint"]] = dict(fila)
    estados = {e["fingerprint"]: dict(e) for e in con.execute("SELECT * FROM estado WHERE org_id = ?", (org_id,))}
    sin_doctor = {r[0]: r[1] for r in con.execute(f"SELECT run_id, sin_doctor FROM runs WHERE run_id IN ({marcas})", previas)}
    sin_verif = {r: [] for r in previas}
    for fila in con.execute(f"SELECT run_id, account_id, region, motivo FROM sin_verificar WHERE run_id IN ({marcas})",
                            previas):
        sin_verif[fila[0]].append(tuple(fila[1:]))
    ultimos = {}  # último registro de cada fingerprint (para los que ya no están)
    for fila in con.execute(f"""SELECT f.* FROM findings f JOIN (SELECT fingerprint, MAX(run_id) m FROM findings
                                WHERE run_id IN ({marcas}) GROUP BY fingerprint) u
                                ON f.fingerprint = u.fingerprint AND f.run_id = u.m""", previas):
        ultimos[fila["fingerprint"]] = dict(fila)

    actuales = {f["fingerprint"]: dict(f) for f in con.execute("SELECT * FROM findings WHERE run_id = ?", (run_id,))}
    salida = []
    for fp, f in actuales.items():
        f["presencia"] = "presente"
        salida.append(f)
    # Los que aparecieron antes y no están en esta: resuelto si alguna corrida posterior a su última aparición
    # podía verlo (aunque esta no pueda: un --sin-doctor después no lo "des-resuelve").
    for fp, h in historia.items():
        if fp in actuales:
            continue
        f = ultimos[fp]
        posteriores = previas[previas.index(h["ultima"]) + 1:]
        verificado = [r for r in posteriores if _verificable(sin_verif[r], sin_doctor[r], f)]
        f["presencia"] = "resuelto" if verificado else "no_verificado"
        f["resuelto_en"] = verificado[0] if verificado else None
        salida.append(f)
    for f in salida:
        h = historia[f["fingerprint"]]
        e = estados.get(f["fingerprint"]) or {}
        f.update(primera=h["primera"], ultima=h["ultima"], veces=h["veces"],
                 nuevo=f["presencia"] == "presente" and h["primera"] == run_id and len(previas) > 1,
                 estado=e.get("status") or "abierto", nota=e.get("nota") or "", estado_fecha=e.get("updated_at"))
        f["reaparece"] = f["presencia"] == "presente" and f["estado"] == "resuelto"
    orden = {"alta": 0, "media": 1, "baja": 2}
    salida.sort(key=lambda f: (f["presencia"] != "presente", orden.get(f["severity"], 9), -(f["monthly_usd"] or 0)))
    return salida


def resumen(con, org_id, run_id, cuenta=None):
    """Resumen de la corrida; con `cuenta`, todo (costos, usos, hallazgos, evolución) se limita a esa cuenta."""
    run = con.execute("SELECT * FROM runs WHERE run_id = ? AND org_id = ?", (run_id, org_id)).fetchone()
    if not run:
        return None
    cuenta = cuenta or None
    filtro = (cuenta, cuenta)
    lista = [f for f in hallazgos(con, org_id, run_id) if not cuenta or f["account_id"] == cuenta]
    abiertos = [f for f in lista if f["presencia"] == "presente" and f["estado"] not in ("aceptado", "ignorado")]
    meses = [r[0] for r in con.execute("""SELECT month FROM costos WHERE run_id = ? UNION SELECT month FROM cargos
                                          WHERE run_id = ? ORDER BY month""", (run_id, run_id))]
    recursos_por_servicio = [dict(r) for r in con.execute(
        """SELECT service, type, COUNT(*) n FROM recursos WHERE run_id = ? AND (? IS NULL OR account_id = ?)
           GROUP BY service, type ORDER BY n DESC""", (run_id, *filtro))]
    ultimo = meses[-1] if meses else None
    nombres = {r["account_id"]: r["name"] for r in con.execute("SELECT * FROM cuentas WHERE run_id = ?", (run_id,))}
    por_cuenta_mes = [dict(r) for r in con.execute(
        """SELECT account_id, month, ROUND(SUM(usd), 2) usd FROM costos WHERE run_id = ? AND (? IS NULL OR account_id = ?)
           GROUP BY account_id, month ORDER BY month""", (run_id, *filtro))]
    servicios = [dict(r) for r in con.execute(
        """SELECT service, ROUND(SUM(usd), 2) usd FROM costos WHERE run_id = ? AND month = ? AND (? IS NULL OR account_id = ?)
           GROUP BY service ORDER BY usd DESC LIMIT 12""", (run_id, ultimo, *filtro))]
    usos = [dict(r) for r in con.execute(
        """SELECT usage_type, ROUND(SUM(usd), 2) usd FROM usos WHERE run_id = ? AND month = ? AND (? IS NULL OR account_id = ?)
           GROUP BY usage_type ORDER BY usd DESC LIMIT 15""", (run_id, ultimo, *filtro))]
    cargos = [dict(r) for r in con.execute(
        """SELECT record_type, ROUND(SUM(usd), 2) usd FROM cargos WHERE run_id = ? AND month = ?
           AND (? IS NULL OR account_id = ?) GROUP BY record_type ORDER BY ABS(SUM(usd)) DESC""",
        (run_id, ultimo, *filtro))]
    cargos_mes = [dict(r) for r in con.execute(
        """SELECT month, ROUND(SUM(usd), 2) usd FROM cargos WHERE run_id = ? AND (? IS NULL OR account_id = ?)
           GROUP BY month ORDER BY month""", (run_id, *filtro))]
    uso_ultimo = round(sum(r["usd"] for r in por_cuenta_mes if r["month"] == ultimo), 2)
    # Para la factura, el uso sin umbral (usage_total_monthly_usd) si la corrida lo trae; si no, el de services.
    total_mes = [dict(r) for r in con.execute(
        """SELECT month, ROUND(SUM(usd), 2) usd FROM uso_total WHERE run_id = ? AND (? IS NULL OR account_id = ?)
           GROUP BY month ORDER BY month""", (run_id, *filtro))]
    if not total_mes:
        total_mes = [{"month": m, "usd": round(sum(r["usd"] for r in por_cuenta_mes if r["month"] == m), 2)} for m in meses]
    uso_fact_ultimo = round(sum(r["usd"] for r in total_mes if r["month"] == ultimo), 2)
    # Ventana completa (los meses del discovery): uso + cargos = factura; impuestos aparte.
    uso_ventana = round(sum(r["usd"] for r in por_cuenta_mes), 2)
    uso_fact_ventana = round(sum(r["usd"] for r in total_mes), 2)
    cargos_ventana = round(sum(r["usd"] for r in cargos_mes), 2)
    impuestos_ventana = con.execute(
        """SELECT COALESCE(ROUND(SUM(usd), 2), 0) FROM cargos WHERE run_id = ? AND record_type = 'Tax'
           AND (? IS NULL OR account_id = ?)""", (run_id, *filtro)).fetchone()[0]
    regiones = sorted({r[0] for r in con.execute(
        """SELECT region FROM costos WHERE run_id = ? AND (? IS NULL OR account_id = ?)
           UNION SELECT region FROM recursos WHERE run_id = ? AND (? IS NULL OR account_id = ?)""",
        (run_id, *filtro, run_id, *filtro)) if r[0]})
    return {
        "n_cuentas": 1 if cuenta else len(nombres),
        "n_cuentas_con_costo": 1 if cuenta else len({r["account_id"] for r in por_cuenta_mes}),
        "regiones": regiones,
        "ventana": {"desde": meses[0] if meses else None, "hasta": ultimo, "meses": len(meses),
                    "uso": uso_ventana, "factura": round(uso_fact_ventana + cargos_ventana, 2),
                    "impuestos": impuestos_ventana},
        "uso_total_mes": total_mes,
        "run": dict(run),
        "cuenta": cuenta,
        "tiene_cargos": bool(con.execute("SELECT 1 FROM cargos WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()),
        "cargos": cargos,
        "cargos_mes": cargos_mes,
        "impuestos_ultimo_mes": round(sum(c["usd"] for c in cargos if c["record_type"] == "Tax"), 2),
        "uso_factura_ultimo_mes": uso_fact_ultimo,
        "factura_ultimo_mes": round(uso_fact_ultimo + sum(c["usd"] for c in cargos), 2),
        "cuentas": nombres,
        "recursos_por_servicio": recursos_por_servicio,
        "n_recursos": sum(r["n"] for r in recursos_por_servicio),
        "meses": meses,
        "ultimo_mes": ultimo,
        "gasto_ultimo_mes": round(sum(r["usd"] for r in por_cuenta_mes if r["month"] == ultimo), 2),
        "ahorro_abierto": round(sum(f["monthly_usd"] or 0 for f in abiertos if f["is_saving"]), 2),
        "altos_abiertos": sum(1 for f in abiertos if f["severity"] == "alta"),
        "abiertos": len(abiertos),
        "nuevos": sum(1 for f in lista if f["nuevo"]),
        # Resueltos en ESTA corrida (simétrico con "nuevos"); los de antes siguen en la lista con su fecha.
        "resueltos": sum(1 for f in lista if f["presencia"] == "resuelto" and f.get("resuelto_en") == run_id),
        "no_verificados": sum(1 for f in lista if f["presencia"] == "no_verificado"),
        "por_cuenta_mes": por_cuenta_mes,
        "servicios": servicios,
        "usos": usos,
        "evolucion": corridas(con, org_id, cuenta),
    }


def recursos(con, org_id, run_id, cuenta=None):
    """Inventario de la corrida (opcionalmente de una cuenta), con cuántos hallazgos presentes tiene cada recurso."""
    if not con.execute("SELECT 1 FROM runs WHERE run_id = ? AND org_id = ?", (run_id, org_id)).fetchone():
        return []
    cuenta = cuenta or None
    con_hallazgos = {}
    for fila in con.execute("""SELECT account_id, region, resource_id, COUNT(*) n FROM findings WHERE run_id = ?
                               AND resource_id IS NOT NULL GROUP BY account_id, region, resource_id""", (run_id,)):
        con_hallazgos[(fila[0], fila[1], fila[2])] = fila[3]
    salida = []
    for r in con.execute("""SELECT * FROM recursos WHERE run_id = ? AND (? IS NULL OR account_id = ?)
                            ORDER BY account_id, region, service, type, name""", (run_id, cuenta, cuenta)):
        d = dict(r)
        for k in ("tags", "attrs", "relations"):
            d[k] = json.loads(d[k] or "{}")
        # Los findings de recursos globales (S3, IAM, CloudFront) van con region None; el recurso, con "global".
        d["n_hallazgos"] = (con_hallazgos.get((d["account_id"], d["region"], d["id"]), 0) +
                            (con_hallazgos.get((d["account_id"], None, d["id"]), 0) if d["region"] == "global" else 0))
        salida.append(d)
    return salida


def guardar_estado(con, org_id, fingerprint, status, nota):
    """Valida y guarda el estado que el usuario le da a un finding. Devuelve el registro o lanza ValueError."""
    if status not in ESTADOS:
        raise ValueError("estado inválido")
    if not isinstance(nota, str) or len(nota) > MAX_NOTA:
        raise ValueError("nota inválida")
    existe = con.execute("""SELECT 1 FROM findings f JOIN runs r ON r.run_id = f.run_id
                            WHERE r.org_id = ? AND f.fingerprint = ? LIMIT 1""", (org_id, fingerprint)).fetchone()
    if not existe:
        raise ValueError("finding desconocido")
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute("""INSERT INTO estado VALUES (?,?,?,?,?) ON CONFLICT (org_id, fingerprint)
                   DO UPDATE SET status = excluded.status, nota = excluded.nota, updated_at = excluded.updated_at""",
                (org_id, fingerprint, status, nota, ahora))
    con.commit()
    return {"org_id": org_id, "fingerprint": fingerprint, "status": status, "nota": nota, "updated_at": ahora}


def carpeta_de(con, run_id):
    """Carpeta de una corrida conocida (para servir su reporte.html): nunca se arma desde la URL."""
    fila = con.execute("SELECT carpeta FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return Path(fila[0]) if fila else None
