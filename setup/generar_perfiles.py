"""Genera ~/.aws/dive-config con un perfil de solo lectura por cuenta (modo sso).

Toma el sso-session del perfil de management en ~/.aws/config y crea:
  [profile dive-management]      → la cuenta management con el rol de lectura
  [profile dive-<account_id>]    → cada cuenta ACTIVE de la organización

No modifica ~/.aws/config. Los scripts del método usan este archivo vía AWS_CONFIG_FILE.

Uso: python setup/generar_perfiles.py [--config dive.config.yaml]
"""

import argparse
import configparser
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from common import cargar_config  # noqa: E402
from common.aws import DIVE_AWS_CONFIG, client  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    args = parser.parse_args()
    cfg = cargar_config(args.config)

    origen = configparser.RawConfigParser()
    origen.read(Path.home() / ".aws" / "config")
    seccion = f"profile {cfg['management_profile']}"
    if seccion not in origen:
        raise SystemExit(f"No encuentro [{seccion}] en ~/.aws/config")
    sso_session = origen[seccion].get("sso_session")
    if not sso_session or f"sso-session {sso_session}" not in origen:
        raise SystemExit("El perfil de management no usa sso_session; usa access_mode: assume_role")
    region = origen[seccion].get("region", "us-east-1")

    session = boto3.Session(profile_name=cfg["management_profile"])
    mgmt_id = client(session, "sts").get_caller_identity()["Account"]
    cuentas = {}
    for pagina in client(session, "organizations", "us-east-1").get_paginator("list_accounts").paginate():
        for a in pagina["Accounts"]:
            if a["Status"] == "ACTIVE":
                cuentas[a["Id"]] = a["Name"]

    destino = configparser.RawConfigParser()
    destino[f"sso-session {sso_session}"] = dict(origen[f"sso-session {sso_session}"])

    def perfil(account_id):
        return {"sso_session": sso_session, "sso_account_id": account_id,
                "sso_role_name": cfg["scan_role_name"], "region": region, "output": "json"}

    destino["profile dive-management"] = perfil(mgmt_id)
    for account_id in sorted(cuentas):
        destino[f"profile dive-{account_id}"] = perfil(account_id)

    with open(DIVE_AWS_CONFIG, "w") as f:
        destino.write(f)
    print(f"{len(cuentas)} perfiles escritos en {DIVE_AWS_CONFIG} (rol {cfg['scan_role_name']})")
    print(f"Prueba: AWS_CONFIG_FILE={DIVE_AWS_CONFIG} aws sts get-caller-identity --profile dive-management")


if __name__ == "__main__":
    main()
