"""Dominios OpenSearch/Elasticsearch. De la access policy solo se guarda un booleano."""

import json

from common import recurso
from common.aws import client

NAME = "opensearch"
TRIGGERS = ["Amazon OpenSearch Service"]
ALWAYS = False
GLOBAL = False


def _principal_abierto(principal):
    if principal == "*":
        return True
    if isinstance(principal, dict):
        valores = principal.get("AWS", [])
        valores = valores if isinstance(valores, list) else [valores]
        return "*" in valores
    return False


def _politica_con_asterisco(texto):
    """True si algún Allow tiene Principal "*" (con o sin Condition). La policy no se copia."""
    if not texto:
        return False
    try:
        doc = json.loads(texto)
    except ValueError:
        return None
    sentencias = doc.get("Statement", [])
    sentencias = sentencias if isinstance(sentencias, list) else [sentencias]
    return any(s.get("Effect") == "Allow" and _principal_abierto(s.get("Principal")) for s in sentencias)


def collect(session, region, account_id):
    os_ = client(session, "opensearch", region)
    recursos = []
    nombres = [d["DomainName"] for d in os_.list_domain_names().get("DomainNames", [])]
    # describe_domains acepta máximo 5 dominios por llamada.
    for i in range(0, len(nombres), 5):
        for d in os_.describe_domains(DomainNames=nombres[i:i + 5])["DomainStatusList"]:
            cluster = d.get("ClusterConfig") or {}
            vpc = d.get("VPCOptions") or {}
            estado = "deleted" if d.get("Deleted") else ("processing" if d.get("Processing") else "active")
            recursos.append(recurso(
                account_id, region, NAME, "domain", d["DomainName"],
                arn=d.get("ARN"),
                name=d["DomainName"],
                state=estado,
                attrs={
                    "engine_version": d.get("EngineVersion"),
                    "instance_type": cluster.get("InstanceType"),
                    "instance_count": cluster.get("InstanceCount"),
                    "dedicated_master": cluster.get("DedicatedMasterEnabled"),
                    "zone_awareness": cluster.get("ZoneAwarenessEnabled"),
                    "warm_enabled": cluster.get("WarmEnabled"),
                    "ebs_volume_gb": (d.get("EBSOptions") or {}).get("VolumeSize"),
                    "in_vpc": bool(vpc.get("VPCId")),
                    "endpoint_host": d.get("Endpoint") or (d.get("Endpoints") or {}).get("vpc"),
                    "at_rest_encryption": (d.get("EncryptionAtRestOptions") or {}).get("Enabled"),
                    "node_to_node_encryption": (d.get("NodeToNodeEncryptionOptions") or {}).get("Enabled"),
                    "enforce_https": (d.get("DomainEndpointOptions") or {}).get("EnforceHTTPS"),
                    "fine_grained_access": (d.get("AdvancedSecurityOptions") or {}).get("Enabled"),
                    "access_policy_principal_star": _politica_con_asterisco(d.get("AccessPolicies")),
                },
                relations={
                    "vpc_id": vpc.get("VPCId"),
                    "subnet_ids": vpc.get("SubnetIds", []),
                    "security_group_ids": vpc.get("SecurityGroupIds", []),
                },
            ))
    return recursos
