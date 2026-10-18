"""Resumen de la cuenta en IAM: solo los indicadores del usuario root (MFA, password, access keys).

`get_account_summary` devuelve contadores, nunca credenciales. No se listan usuarios ni policies.
"""

from common import recurso
from common.aws import client

NAME = "iam"
TRIGGERS = []
ALWAYS = True   # IAM no factura: el billing nunca lo activa
GLOBAL = True
TAGS = False    # el resumen de cuenta no tiene tags


def collect(session, region, account_id):
    resumen = client(session, "iam", "us-east-1").get_account_summary()["SummaryMap"]
    return [recurso(
        account_id, "global", NAME, "account_summary", account_id,
        arn=f"arn:aws:iam::{account_id}:root",
        name="root",
        attrs={
            "mfa_enabled": bool(resumen.get("AccountMFAEnabled")),
            "access_keys_present": bool(resumen.get("AccountAccessKeysPresent")),
            "password_present": bool(resumen.get("AccountPasswordPresent")),
            "users": resumen.get("Users"),
        },
    )]
