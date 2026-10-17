"""Llaves KMS customer-managed. Las AWS-managed (aws/*) se descartan."""

from botocore.exceptions import ClientError

from common import recurso
from common.aws import client

NAME = "kms"
TRIGGERS = ["AWS Key Management Service"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    kms = client(session, "kms", region)
    alias = {}
    for pagina in kms.get_paginator("list_aliases").paginate():
        for a in pagina["Aliases"]:
            if a.get("TargetKeyId"):
                alias.setdefault(a["TargetKeyId"], []).append(a["AliasName"])

    recursos = []
    for pagina in kms.get_paginator("list_keys").paginate():
        for k in pagina["Keys"]:
            m = kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
            if m.get("KeyManager") != "CUSTOMER":
                continue
            # La rotación solo aplica a simétricas con material de AWS; en otras da error.
            try:
                rotacion = kms.get_key_rotation_status(KeyId=k["KeyId"]).get("KeyRotationEnabled")
            except ClientError:
                rotacion = None
            try:
                tags = kms.list_resource_tags(KeyId=k["KeyId"]).get("Tags", [])
            except ClientError:
                tags = []
            nombres = alias.get(k["KeyId"], [])
            recursos.append(recurso(
                account_id, region, NAME, "key", k["KeyId"],
                arn=m.get("Arn"),
                name=nombres[0] if nombres else None,
                state=m.get("KeyState"),
                tags=[{"Key": t["TagKey"], "Value": t["TagValue"]} for t in tags],
                attrs={
                    "aliases": nombres,
                    "key_usage": m.get("KeyUsage"),
                    "key_spec": m.get("KeySpec"),
                    "origin": m.get("Origin"),
                    "multi_region": m.get("MultiRegion"),
                    "rotation_enabled": rotacion,
                    "created": m.get("CreationDate"),
                    "deletion_date": m.get("DeletionDate"),
                },
            ))
    return recursos
