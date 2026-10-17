"""Alarmas de CloudWatch (métricas y compuestas). Las 10 primeras son gratis, por eso ALWAYS."""

from common import recurso
from common.aws import client

NAME = "cloudwatch"
TRIGGERS = ["AmazonCloudWatch"]
ALWAYS = True
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag

# Dimensiones que apuntan a un recurso inventariado: sirven para ligar la alarma a lo que vigila.
_DIMENSIONES_RECURSO = {"InstanceId", "DBInstanceIdentifier", "DBClusterIdentifier", "LoadBalancer", "TargetGroup",
                        "FunctionName", "ClusterName", "ServiceName", "QueueName", "TableName", "VolumeId",
                        "NatGatewayId", "TransitGateway", "VpnId", "CacheClusterId", "DomainName"}


def collect(session, region, account_id):
    cw = client(session, "cloudwatch", region)
    recursos = []
    for pagina in cw.get_paginator("describe_alarms").paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"]):
        for a in pagina.get("MetricAlarms", []):
            vigila = [d["Value"] for d in a.get("Dimensions", []) if d["Name"] in _DIMENSIONES_RECURSO]
            recursos.append(recurso(
                account_id, region, NAME, "alarm", a["AlarmName"],
                arn=a["AlarmArn"], name=a["AlarmName"], state=a.get("StateValue"),
                attrs={
                    "namespace": a.get("Namespace"),
                    "metric": a.get("MetricName"),
                    "actions_enabled": a.get("ActionsEnabled"),
                    "has_actions": bool(a.get("AlarmActions")),
                    "state_updated": a.get("StateUpdatedTimestamp"),
                },
                # Las acciones (tópicos SNS, políticas de autoscaling) son ARNs: sirven para saber quién se entera.
                relations={"attached_to": vigila, "target_ids": a.get("AlarmActions", [])},
            ))
        for a in pagina.get("CompositeAlarms", []):
            recursos.append(recurso(
                account_id, region, NAME, "composite_alarm", a["AlarmName"],
                arn=a["AlarmArn"], name=a["AlarmName"], state=a.get("StateValue"),
                attrs={"actions_enabled": a.get("ActionsEnabled"), "has_actions": bool(a.get("AlarmActions"))},
                relations={"target_ids": a.get("AlarmActions", [])},
            ))
    return recursos
