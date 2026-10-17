"""Tablas DynamoDB. list_tables solo trae nombres: se hace describe_table por tabla."""

from common import recurso
from common.aws import client

NAME = "dynamodb"
TRIGGERS = ["Amazon DynamoDB"]
ALWAYS = False
GLOBAL = False


def _tags(ddb, arn):
    # Tags opcionales: un AccessDenied aquí no debe perder la tabla.
    try:
        return ddb.list_tags_of_resource(ResourceArn=arn).get("Tags", [])
    except Exception:  # noqa: BLE001
        return []


def collect(session, region, account_id):
    ddb = client(session, "dynamodb", region)
    recursos = []
    for pagina in ddb.get_paginator("list_tables").paginate():
        for nombre in pagina["TableNames"]:
            t = ddb.describe_table(TableName=nombre)["Table"]
            capacidad = t.get("ProvisionedThroughput") or {}
            recursos.append(recurso(
                account_id, region, NAME, "table", nombre,
                arn=t.get("TableArn"),
                name=nombre,
                state=t.get("TableStatus"),
                tags=_tags(ddb, t["TableArn"]),
                attrs={
                    # Sin BillingModeSummary la tabla es PROVISIONED (tablas antiguas).
                    "billing_mode": (t.get("BillingModeSummary") or {}).get("BillingMode", "PROVISIONED"),
                    "table_class": (t.get("TableClassSummary") or {}).get("TableClass", "STANDARD"),
                    "size_bytes": t.get("TableSizeBytes"),
                    "item_count": t.get("ItemCount"),
                    "rcu": capacidad.get("ReadCapacityUnits"),
                    "wcu": capacidad.get("WriteCapacityUnits"),
                    "gsi_count": len(t.get("GlobalSecondaryIndexes", [])),
                    "stream_enabled": (t.get("StreamSpecification") or {}).get("StreamEnabled", False),
                    "sse": (t.get("SSEDescription") or {}).get("SSEType"),
                    "deletion_protection": t.get("DeletionProtectionEnabled"),
                    "replica_regions": [r["RegionName"] for r in t.get("Replicas", [])],
                },
            ))
    return recursos
