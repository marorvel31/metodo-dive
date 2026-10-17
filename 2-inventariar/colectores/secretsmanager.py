"""Secretos de Secrets Manager: SOLO metadatos de list_secrets.

JAMÁS se llama get_secret_value ni batch_get_secret_value: el valor del secreto nunca se lee.
Tampoco se copia Description (a veces la gente pega pistas ahí).
"""

from common import recurso
from common.aws import client

NAME = "secretsmanager"
TRIGGERS = ["AWS Secrets Manager"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    sm = client(session, "secretsmanager", region)
    recursos = []
    for pagina in sm.get_paginator("list_secrets").paginate():
        for s in pagina["SecretList"]:
            recursos.append(recurso(
                account_id, region, NAME, "secret", s["Name"],
                arn=s.get("ARN"),
                name=s["Name"],
                state="pending_deletion" if s.get("DeletedDate") else "active",
                tags=s.get("Tags"),
                attrs={
                    "rotation_enabled": s.get("RotationEnabled", False),
                    "rotation_days": (s.get("RotationRules") or {}).get("AutomaticallyAfterDays"),
                    "last_rotated": s.get("LastRotatedDate"),
                    "last_accessed": s.get("LastAccessedDate"),
                    "last_changed": s.get("LastChangedDate"),
                    "created": s.get("CreatedDate"),
                    "kms_key_id": s.get("KmsKeyId"),
                    "owning_service": s.get("OwningService"),
                    "primary_region": s.get("PrimaryRegion"),
                },
                relations={"attached_to": [s["RotationLambdaARN"]] if s.get("RotationLambdaARN") else []},
            ))
    return recursos
