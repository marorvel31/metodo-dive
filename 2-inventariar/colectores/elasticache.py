"""ElastiCache: replication groups (Redis/Valkey) y cache clusters (nodos y Memcached)."""

from common import recurso
from common.aws import client

NAME = "elasticache"
TRIGGERS = ["Amazon ElastiCache"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ec = client(session, "elasticache", region)
    recursos = []

    for pagina in ec.get_paginator("describe_replication_groups").paginate():
        for g in pagina["ReplicationGroups"]:
            # AuthToken nunca se devuelve; solo se registra si está habilitado.
            recursos.append(recurso(
                account_id, region, NAME, "replication_group", g["ReplicationGroupId"],
                arn=g.get("ARN"),
                name=g["ReplicationGroupId"],
                state=g.get("Status"),
                attrs={
                    "node_type": g.get("CacheNodeType"),
                    "engine": g.get("Engine"),
                    "cluster_mode": g.get("ClusterMode") or ("enabled" if g.get("ClusterEnabled") else "disabled"),
                    "multi_az": g.get("MultiAZ"),
                    "automatic_failover": g.get("AutomaticFailover"),
                    "transit_encryption": g.get("TransitEncryptionEnabled"),
                    "at_rest_encryption": g.get("AtRestEncryptionEnabled"),
                    "auth_token_enabled": g.get("AuthTokenEnabled"),
                    "kms_key_id": g.get("KmsKeyId"),
                    "snapshot_retention_days": g.get("SnapshotRetentionLimit"),
                    "members": g.get("MemberClusters", []),
                },
                relations={"attached_to": g.get("MemberClusters", [])},
            ))

    for pagina in ec.get_paginator("describe_cache_clusters").paginate():
        for c in pagina["CacheClusters"]:
            recursos.append(recurso(
                account_id, region, NAME, "cache_cluster", c["CacheClusterId"],
                arn=c.get("ARN"),
                name=c["CacheClusterId"],
                state=c.get("CacheClusterStatus"),
                attrs={
                    "node_type": c.get("CacheNodeType"),
                    "engine": c.get("Engine"),
                    "engine_version": c.get("EngineVersion"),
                    "num_nodes": c.get("NumCacheNodes"),
                    "az": c.get("PreferredAvailabilityZone"),
                    "transit_encryption": c.get("TransitEncryptionEnabled"),
                    "at_rest_encryption": c.get("AtRestEncryptionEnabled"),
                    "replication_group_id": c.get("ReplicationGroupId"),
                    "subnet_group": c.get("CacheSubnetGroupName"),
                },
                relations={
                    "security_group_ids": [g["SecurityGroupId"] for g in c.get("SecurityGroups", [])],
                },
            ))
    return recursos
