"""Acceso a AWS: sesión de management, sesión por cuenta y clientes con reintentos."""

import os
from pathlib import Path

import boto3
from botocore.config import Config

# Perfiles dive-<account_id> generados por setup/generar_perfiles.py.
# Van en un archivo aparte para no tocar ~/.aws/config.
DIVE_AWS_CONFIG = Path.home() / ".aws" / "dive-config"

_REINTENTOS = Config(retries={"max_attempts": 10, "mode": "adaptive"}, connect_timeout=10, read_timeout=60)


def client(session, servicio, region=None):
    return session.client(servicio, region_name=region, config=_REINTENTOS)


def _usar_perfiles_dive():
    if not DIVE_AWS_CONFIG.exists():
        raise SystemExit("Faltan los perfiles dive-*. Corre: python setup/generar_perfiles.py")
    os.environ["AWS_CONFIG_FILE"] = str(DIVE_AWS_CONFIG)


def sesion_management(cfg):
    """En modo sso usa el perfil de solo lectura dive-management; si no, management_profile."""
    if cfg["access_mode"] == "sso":
        _usar_perfiles_dive()
        return boto3.Session(profile_name="dive-management")
    return boto3.Session(profile_name=cfg["management_profile"])


def listar_cuentas(cfg, session=None):
    """{account_id: nombre} de las cuentas ACTIVE, aplicando include/exclude del config."""
    session = session or sesion_management(cfg)
    orgs = client(session, "organizations", "us-east-1")
    cuentas = {}
    for pagina in orgs.get_paginator("list_accounts").paginate():
        for a in pagina["Accounts"]:
            if a["Status"] == "ACTIVE":
                cuentas[a["Id"]] = a["Name"]
    incluir = {str(x) for x in cfg["include_accounts"]}
    excluir = {str(x) for x in cfg["exclude_accounts"]}
    return {k: v for k, v in cuentas.items() if (not incluir or k in incluir) and k not in excluir}


def sesion_cuenta(cfg, account_id, session_management=None):
    """Sesión de solo lectura dentro de una cuenta miembro."""
    if cfg["access_mode"] == "sso":
        _usar_perfiles_dive()
        return boto3.Session(profile_name=f"dive-{account_id}")

    session_management = session_management or sesion_management(cfg)
    rol = f"arn:aws:iam::{account_id}:role/{cfg['scan_role_name']}"
    cred = client(session_management, "sts").assume_role(RoleArn=rol, RoleSessionName="dive-inventario")["Credentials"]
    return boto3.Session(
        aws_access_key_id=cred["AccessKeyId"],
        aws_secret_access_key=cred["SecretAccessKey"],
        aws_session_token=cred["SessionToken"],
    )
