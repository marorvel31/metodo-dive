"""VPC Flow Logs: qué VPC/subnet/ENI registra su tráfico y hacia dónde (PCI DSS req. 10)."""

from common import recurso
from common.aws import client

NAME = "flowlogs"
TRIGGERS = ["Amazon Virtual Private Cloud"]
ALWAYS = True  # hay que saber también qué VPCs NO tienen flow logs
GLOBAL = False


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []
    for pagina in ec2.get_paginator("describe_flow_logs").paginate():
        for f in pagina["FlowLogs"]:
            recursos.append(recurso(
                account_id, region, NAME, "flow_log", f["FlowLogId"],
                arn=f"arn:aws:ec2:{region}:{account_id}:vpc-flow-log/{f['FlowLogId']}",
                state=f.get("FlowLogStatus"), tags=f.get("Tags"),
                attrs={
                    "traffic_type": f.get("TrafficType"),
                    "destination_type": f.get("LogDestinationType"),
                    "destination": f.get("LogDestination") or f.get("LogGroupName"),
                    "deliver_status": f.get("DeliverLogsStatus"),
                    "max_aggregation_s": f.get("MaxAggregationInterval"),
                },
                relations={"attached_to": [f["ResourceId"]] if f.get("ResourceId") else []},
            ))
    return recursos
