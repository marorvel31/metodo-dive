"""Log groups de CloudWatch Logs: retención, tamaño y cifrado. Nunca se leen eventos de log."""

from common import recurso
from common.aws import client

NAME = "logs"
TRIGGERS = ["AmazonCloudWatch"]
ALWAYS = True  # un log group sin ingesta no factura, pero existe (y suele no tener retención)
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def collect(session, region, account_id):
    logs = client(session, "logs", region)
    recursos = []
    for pagina in logs.get_paginator("describe_log_groups").paginate():
        for g in pagina["logGroups"]:
            arn = g.get("logGroupArn") or g.get("arn", "").removesuffix(":*")
            recursos.append(recurso(
                account_id, region, NAME, "log_group", g["logGroupName"],
                arn=arn, name=g["logGroupName"],
                attrs={
                    "retention_days": g.get("retentionInDays"),  # None = se guarda para siempre
                    "stored_gb": round(g.get("storedBytes", 0) / 1024 ** 3, 3),
                    "kms_key_id": g.get("kmsKeyId"),
                    "log_group_class": g.get("logGroupClass"),
                    "created_ms": g.get("creationTime"),
                },
            ))
    return recursos
