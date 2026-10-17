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


def carpeta_salida(cfg, fecha=None):
    """salida/<YYYY-MM-DD>/ relativa a la raíz del repo, salvo que output_dir sea absoluta.

    Las salidas son el reconocimiento completo de la organización: solo el usuario puede leerlas
    (ver el umask al importar este módulo).
    """
    base = Path(cfg["output_dir"])
    if not base.is_absolute():
        base = RAIZ / base
    carpeta = base / (fecha or datetime.now().strftime("%Y-%m-%d"))
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


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
