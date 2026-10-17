"""Colas SQS. Solo atributos de configuración: nunca se reciben ni se leen mensajes."""

import json

from common import recurso
from common.aws import client

NAME = "sqs"
TRIGGERS = ["Amazon Simple Queue Service"]
ALWAYS = False
GLOBAL = False

# Lista cerrada de atributos: no se pide "All" para no traer la Policy.
_ATRIBUTOS = ["QueueArn", "VisibilityTimeout", "MessageRetentionPeriod", "DelaySeconds",
              "MaximumMessageSize", "ApproximateNumberOfMessages", "SqsManagedSseEnabled",
              "KmsMasterKeyId", "RedrivePolicy"]


def collect(session, region, account_id):
    sqs = client(session, "sqs", region)
    recursos = []
    for pagina in sqs.get_paginator("list_queues").paginate():
        for url in pagina.get("QueueUrls", []):
            a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=_ATRIBUTOS)["Attributes"]
            try:
                tags = sqs.list_queue_tags(QueueUrl=url).get("Tags", {})
            except Exception:  # noqa: BLE001 — sin tags no se pierde la cola
                tags = {}
            nombre = url.rstrip("/").rsplit("/", 1)[-1]
            dlq = None
            if a.get("RedrivePolicy"):
                dlq = json.loads(a["RedrivePolicy"]).get("deadLetterTargetArn")
            recursos.append(recurso(
                account_id, region, NAME, "queue", nombre,
                arn=a.get("QueueArn"),
                name=nombre,
                tags=tags,
                attrs={
                    "fifo": url.endswith(".fifo"),  # FifoQueue da InvalidAttributeName en colas estándar
                    "visibility_timeout_s": int(a.get("VisibilityTimeout", 0)),
                    "retention_s": int(a.get("MessageRetentionPeriod", 0)),
                    "delay_s": int(a.get("DelaySeconds", 0)),
                    "approx_messages": int(a.get("ApproximateNumberOfMessages", 0)),
                    "sse_managed": a.get("SqsManagedSseEnabled") == "true",
                    "kms_key_id": a.get("KmsMasterKeyId"),
                    "dead_letter_target": dlq,
                },
                relations={"target_ids": [dlq] if dlq else []},
            ))
    return recursos
