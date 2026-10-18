"""Utilidades compartidas por todas las fases del Método DIVE."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent

# Todo lo que escriben las fases (JSON, salidas crudas de aws-doctor, reporte) es el reconocimiento
# completo de la organización: archivos y carpetas nuevos solo para el usuario. Todas las fases
# importan este módulo, así que vale aunque se pasen rutas explícitas (--inventory, --discovery...).
os.umask(0o077)

# Servicios de gobierno que se activan en todas las regiones (trail multi-región, Config, Security Hub...).
# Una región cuyo único costo viene de estos no es "sorpresa": no hay recursos olvidados ahí.
SERVICIOS_GOBIERNO = {
    "AWS CloudTrail", "AWS Config", "AWS Security Hub", "Amazon GuardDuty",
    "Amazon Inspector", "AWS Cost Explorer", "AWS Identity and Access Management Access Analyzer",
}


def cargar_config(ruta=None):
    ruta = Path(ruta or os.environ.get("DIVE_CONFIG") or RAIZ / "dive.config.yaml")
    if not ruta.exists():
        raise SystemExit(f"No existe {ruta}. Copia dive.config.example.yaml como dive.config.yaml.")
    cfg = yaml.safe_load(ruta.read_text()) or {}
    cfg.setdefault("access_mode", "sso")
    cfg.setdefault("scan_role_name", "DIVE_ReadOnly")
    cfg.setdefault("billing_months", 6)
    cfg.setdefault("threshold_usd", 0.01)
    cfg.setdefault("include_accounts", [])
    cfg.setdefault("exclude_accounts", [])
    cfg.setdefault("max_workers", 10)
    cfg.setdefault("output_dir", "salida")
    cfg.setdefault("anonymize", {})
    for clave in ("include_accounts", "exclude_accounts"):
        for acc in cfg[clave] or []:
            # Sin comillas, YAML lee 012345670123 como octal: la cuenta queda otra y el filtro no la encuentra.
            if not isinstance(acc, str) or not re.fullmatch(r"\d{12}", acc):
                raise SystemExit(f"{clave}: {acc!r} no es un ID de cuenta de 12 dígitos entre comillas "
                                 f"(ej. \"012345678901\"). Sin comillas YAML lo lee como número.")
    return cfg


def ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_RE_SESION = re.compile(r"(assumed-role/[^/\s]+/)[^\s:\"']+")
_RE_USUARIO = re.compile(r"(:user/)[^\s:\"']+")
_RE_EMAIL = re.compile(r"[\w.+-]+(@|%40)[\w-]+(\.[\w-]+)+")


def redactar(texto, largo=300):
    """Quita la sesión, el usuario IAM y los emails del operador (los AccessDenied los traen), en una
    sola línea y recortado. Se redacta antes de recortar para no dejar medio email."""
    texto = _RE_SESION.sub(r"\1<sesion>", str(texto))
    texto = _RE_USUARIO.sub(r"\1<usuario>", texto)
    texto = _RE_EMAIL.sub("<email>", texto)
    return " ".join(texto.split())[:largo]


def error_corto(e, largo=300):
    """'Tipo: mensaje' de una excepción, redactado (ver redactar)."""
    return redactar(f"{type(e).__name__}: {e}", largo)


ULTIMA = "ULTIMA"  # archivo en output_dir con el run_id de la última corrida empezada por la fase 1


def _base_salida(cfg):
    base = Path(cfg["output_dir"]).expanduser()  # "~/clientes/x/runs" no debe acabar en <repo>/~/...
    return base if base.is_absolute() else RAIZ / base


def nueva_corrida(cfg):
    """Solo la fase 1: crea salida/<run_id>/ (UTC, con segundos: dos corridas seguidas no se pisan) y
    apunta ULTIMA a ella. Las salidas son el reconocimiento completo de la organización: solo el
    usuario puede leerlas (ver el umask al importar este módulo)."""
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    base = _base_salida(cfg)
    carpeta = base / run_id
    carpeta.mkdir(parents=True, exist_ok=False)
    return carpeta


def marcar_ultima(carpeta):
    """Apunta ULTIMA a esta corrida. La fase 1 lo llama recién con discovery.json escrito (si falla antes,
    ULTIMA sigue en la corrida anterior) y con reemplazo atómico."""
    carpeta = Path(carpeta)
    tmp = carpeta.parent / (ULTIMA + ".tmp")
    tmp.write_text(carpeta.name + "\n")
    os.replace(tmp, carpeta.parent / ULTIMA)


def corrida_actual(cfg):
    """Fases 2-4: la corrida a la que apunta ULTIMA. Sin ULTIMA (corridas viejas), salida/<fecha de hoy>/.
    Nunca crea nada: si la carpeta no existe, el que llama falla con un mensaje claro."""
    base = _base_salida(cfg)
    ultima = base / ULTIMA
    if ultima.exists() and not ultima.is_symlink():
        run_id = ultima.read_text().strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{6}Z)?", run_id):
            return base / run_id
    return base / datetime.now().strftime("%Y-%m-%d")


def version_dive():
    """Commit del repo (y "-dirty" si hay cambios sin commitear); "desconocida" sin git. Sin nombres de tags:
    son locales y pueden decir cosas que no hacen falta en un archivo que se comparte."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(RAIZ), "describe", "--always", "--dirty", "--match=NONE"], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip() or "desconocida"
    except (OSError, subprocess.SubprocessError):
        return "desconocida"


def run_de(carpeta, organization_id, management_account_id, cfg, currency):
    """Bloque `run` de discovery.json: identifica la corrida y con qué se hizo (ver CONTRATO.md)."""
    return {
        "run_id": Path(carpeta).name,
        "dive_version": version_dive(),
        "organization_id": organization_id,
        "management_account_id": management_account_id,
        "org_alias": cfg.get("org_alias") or organization_id,
        "currency": currency,
        "config": {k: cfg.get(k) for k in ("billing_months", "threshold_usd", "include_accounts",
                                            "exclude_accounts", "access_mode")},
        "flags": {},
    }


def heredar_run(previo, cfg, fase, flags):
    """Copia el `run` del archivo de la fase anterior y agrega las opciones de esta fase. Aborta si la
    corrida es de otra organización que la del config (output_dir compartido entre configs)."""
    run = dict((previo or {}).get("run") or {})
    alias = cfg.get("org_alias")
    # Solo si el alias de la corrida vino de un config (no el o-xxxx por defecto): agregar org_alias al
    # config después de una corrida no debe bloquearla.
    if alias and run.get("org_alias") and run["org_alias"] != run.get("organization_id") and run["org_alias"] != alias:
        raise SystemExit(f"La corrida {run.get('run_id')} es de '{run['org_alias']}', no de '{alias}' "
                         "(org_alias del config). Pasa la ruta explícita o revisa output_dir.")
    run["flags"] = {**(run.get("flags") or {}), fase: flags}
    return run


def leer_json(ruta):
    return json.loads(Path(ruta).read_text())


def escribir_json(ruta, datos):
    Path(ruta).write_text(json.dumps(datos, indent=2, ensure_ascii=False, default=str))
    return ruta


def tags_a_dict(tags):
    """Convierte [{'Key': k, 'Value': v}] (o {'key':..,'value':..}) a {k: v}."""
    if not tags:
        return {}
    if isinstance(tags, dict):
        return dict(tags)
    salida = {}
    for t in tags:
        k = t.get("Key", t.get("key"))
        if k is not None:
            salida[k] = t.get("Value", t.get("value", ""))
    return salida


def recurso(account_id, region, service, type, id, arn=None, name=None, state=None,
            tags=None, attrs=None, relations=None):
    """Construye un recurso con la forma exacta de inventory.json (ver CONTRATO.md)."""
    tags = tags_a_dict(tags)
    return {
        "account_id": account_id,
        "region": region,
        "service": service,
        "type": type,
        "id": id,
        "arn": arn,
        "name": name if name is not None else tags.get("Name"),
        "state": state,
        "tags": tags,
        "attrs": attrs or {},
        "relations": {k: v for k, v in (relations or {}).items() if v},
    }
