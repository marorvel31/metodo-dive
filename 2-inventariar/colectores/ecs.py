"""Clusters y servicios ECS. Las task definitions solo se referencian por ARN: nunca se
llama describe_task_definition, porque containerDefinitions trae environment/secrets."""

from common import recurso
from common.aws import client

NAME = "ecs"
# Fargate factura bajo ECS; ECS sobre EC2 solo aparece como cómputo EC2.
TRIGGERS = ["Amazon Elastic Container Service", "Amazon Elastic Compute Cloud - Compute"]
ALWAYS = False
GLOBAL = False


def _lotes(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def collect(session, region, account_id):
    ecs = client(session, "ecs", region)
    arns = [a for p in ecs.get_paginator("list_clusters").paginate() for a in p["clusterArns"]]
    recursos = []
    for lote in _lotes(arns, 100):  # describe_clusters acepta hasta 100
        for c in ecs.describe_clusters(clusters=lote, include=["TAGS"])["clusters"]:
            recursos.append(recurso(
                account_id, region, NAME, "cluster", c["clusterName"],
                arn=c["clusterArn"],
                name=c["clusterName"],
                state=c.get("status"),
                tags=c.get("tags"),
                attrs={
                    "running_tasks": c.get("runningTasksCount"),
                    "pending_tasks": c.get("pendingTasksCount"),
                    "active_services": c.get("activeServicesCount"),
                    "container_instances": c.get("registeredContainerInstancesCount"),
                    "capacity_providers": c.get("capacityProviders", []),
                },
            ))
            recursos.extend(_servicios(ecs, c["clusterArn"], region, account_id))
    return recursos


def _servicios(ecs, cluster_arn, region, account_id):
    arns = [a for p in ecs.get_paginator("list_services").paginate(cluster=cluster_arn) for a in p["serviceArns"]]
    salida = []
    for lote in _lotes(arns, 10):  # describe_services acepta hasta 10
        for s in ecs.describe_services(cluster=cluster_arn, services=lote, include=["TAGS"])["services"]:
            red = (s.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
            lbs = s.get("loadBalancers", [])
            salida.append(recurso(
                account_id, region, NAME, "service", s["serviceArn"],
                arn=s["serviceArn"],
                name=s["serviceName"],
                state=s.get("status"),
                tags=s.get("tags"),
                attrs={
                    "cluster_arn": cluster_arn,
                    "launch_type": s.get("launchType"),
                    "capacity_provider_strategy": [c.get("capacityProvider") for c in s.get("capacityProviderStrategy", [])],
                    "desired_count": s.get("desiredCount"),
                    "running_count": s.get("runningCount"),
                    "pending_count": s.get("pendingCount"),
                    "task_definition": s.get("taskDefinition"),  # solo el ARN
                    "assign_public_ip": red.get("assignPublicIp"),
                    "load_balancers": [{"target_group_arn": lb.get("targetGroupArn"),
                                        "load_balancer_name": lb.get("loadBalancerName"),
                                        "container_name": lb.get("containerName"),
                                        "container_port": lb.get("containerPort")} for lb in lbs],
                },
                relations={
                    "subnet_ids": red.get("subnets", []),
                    "security_group_ids": red.get("securityGroups", []),
                    "target_ids": [lb["targetGroupArn"] for lb in lbs if lb.get("targetGroupArn")],
                    "attached_to": [cluster_arn],
                },
            ))
    return salida
