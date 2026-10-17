"""Clusters EKS y sus node groups administrados."""

from common import recurso
from common.aws import client

NAME = "eks"
TRIGGERS = ["Amazon Elastic Container Service for Kubernetes"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    eks = client(session, "eks", region)
    recursos = []
    for pagina in eks.get_paginator("list_clusters").paginate():
        for nombre in pagina["clusters"]:
            c = eks.describe_cluster(name=nombre)["cluster"]
            # certificateAuthority e identity no se copian (certificado y emisor OIDC).
            red = c.get("resourcesVpcConfig") or {}
            sgs = list(red.get("securityGroupIds", []))
            if red.get("clusterSecurityGroupId"):
                sgs.append(red["clusterSecurityGroupId"])
            recursos.append(recurso(
                account_id, region, NAME, "cluster", nombre,
                arn=c.get("arn"),
                name=nombre,
                state=c.get("status"),
                tags=c.get("tags"),
                attrs={
                    "version": c.get("version"),
                    "platform_version": c.get("platformVersion"),
                    "endpoint_public": red.get("endpointPublicAccess"),
                    "endpoint_private": red.get("endpointPrivateAccess"),
                    "public_access_cidrs": red.get("publicAccessCidrs", []),
                    "created_at": c.get("createdAt"),
                },
                relations={
                    "vpc_id": red.get("vpcId"),
                    "subnet_ids": red.get("subnetIds", []),
                    "security_group_ids": sgs,
                },
            ))
            recursos.extend(_nodegroups(eks, nombre, region, account_id))
    return recursos


def _nodegroups(eks, cluster, region, account_id):
    salida = []
    for pagina in eks.get_paginator("list_nodegroups").paginate(clusterName=cluster):
        for ng in pagina["nodegroups"]:
            n = eks.describe_nodegroup(clusterName=cluster, nodegroupName=ng)["nodegroup"]
            esc = n.get("scalingConfig") or {}
            asgs = [a["name"] for a in (n.get("resources") or {}).get("autoScalingGroups", [])]
            salida.append(recurso(
                account_id, region, NAME, "nodegroup", f"{cluster}/{ng}",
                arn=n.get("nodegroupArn"),
                name=ng,
                state=n.get("status"),
                tags=n.get("tags"),
                attrs={
                    "cluster": cluster,
                    "instance_types": n.get("instanceTypes", []),
                    "capacity_type": n.get("capacityType"),
                    "ami_type": n.get("amiType"),
                    "min_size": esc.get("minSize"),
                    "max_size": esc.get("maxSize"),
                    "desired_size": esc.get("desiredSize"),
                    "version": n.get("version"),
                },
                relations={
                    "subnet_ids": n.get("subnets", []),
                    "attached_to": asgs,
                },
            ))
    return salida
