"""Application Auto Scaling: qué servicios ECS, tablas DynamoDB, réplicas Aurora... escalan solos."""

from common import recurso
from common.aws import client

NAME = "appautoscaling"
TRIGGERS = ["Amazon Elastic Container Service", "Amazon DynamoDB", "Amazon Relational Database Service",
            "Amazon ElastiCache", "AWS Lambda"]
ALWAYS = False
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag

_NAMESPACES = ["ecs", "dynamodb", "rds", "elasticache", "lambda", "ec2"]


def collect(session, region, account_id):
    aas = client(session, "application-autoscaling", region)
    recursos = []
    for ns in _NAMESPACES:
        for pagina in aas.get_paginator("describe_scalable_targets").paginate(ServiceNamespace=ns):
            for t in pagina["ScalableTargets"]:
                recursos.append(recurso(
                    account_id, region, NAME, "scalable_target", t.get("ScalableTargetARN") or f"{ns}/{t['ResourceId']}",
                    arn=t.get("ScalableTargetARN"), name=t["ResourceId"],
                    state="suspended" if any((t.get("SuspendedState") or {}).values()) else "active",
                    attrs={"namespace": ns, "dimension": t.get("ScalableDimension"),
                           "min": t.get("MinCapacity"), "max": t.get("MaxCapacity")},
                    relations={"attached_to": [t["ResourceId"]]},
                ))
    return recursos
