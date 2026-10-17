"""Instancias RDS y clusters (Aurora, y también DocumentDB/Neptune si existen)."""

from common import recurso
from common.aws import client

NAME = "rds"
TRIGGERS = ["Amazon Relational Database Service"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    rds = client(session, "rds", region)
    recursos = []

    # Subnet groups una sola vez: los clusters solo traen el nombre del grupo.
    subnet_groups = {}
    for pagina in rds.get_paginator("describe_db_subnet_groups").paginate():
        for g in pagina["DBSubnetGroups"]:
            subnet_groups[g["DBSubnetGroupName"]] = {
                "vpc_id": g.get("VpcId"),
                "subnet_ids": [s["SubnetIdentifier"] for s in g.get("Subnets", [])],
            }

    for pagina in rds.get_paginator("describe_db_instances").paginate():
        for db in pagina["DBInstances"]:
            # Solo host y puerto del endpoint; nunca usuario maestro ni password.
            endpoint = db.get("Endpoint") or {}
            grupo = db.get("DBSubnetGroup") or {}
            recursos.append(recurso(
                account_id, region, NAME, "instance", db["DBInstanceIdentifier"],
                arn=db.get("DBInstanceArn"),
                name=db["DBInstanceIdentifier"],
                state=db.get("DBInstanceStatus"),
                tags=db.get("TagList"),
                attrs={
                    "instance_class": db.get("DBInstanceClass"),
                    "engine": db.get("Engine"),
                    "engine_version": db.get("EngineVersion"),
                    "storage_type": db.get("StorageType"),
                    "allocated_storage_gb": db.get("AllocatedStorage"),
                    "multi_az": db.get("MultiAZ"),
                    "publicly_accessible": db.get("PubliclyAccessible"),
                    "storage_encrypted": db.get("StorageEncrypted"),
                    "kms_key_id": db.get("KmsKeyId"),
                    "backup_retention_days": db.get("BackupRetentionPeriod"),
                    "deletion_protection": db.get("DeletionProtection"),
                    "az": db.get("AvailabilityZone"),
                    "endpoint_host": endpoint.get("Address"),
                    "port": endpoint.get("Port"),
                    "cluster_id": db.get("DBClusterIdentifier"),
                    "license_model": db.get("LicenseModel"),
                },
                relations={
                    "vpc_id": grupo.get("VpcId"),
                    "subnet_ids": [s["SubnetIdentifier"] for s in grupo.get("Subnets", [])],
                    "security_group_ids": [g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])],
                },
            ))

    for pagina in rds.get_paginator("describe_db_clusters").paginate():
        for c in pagina["DBClusters"]:
            grupo = subnet_groups.get(c.get("DBSubnetGroup"), {})
            recursos.append(recurso(
                account_id, region, NAME, "cluster", c["DBClusterIdentifier"],
                arn=c.get("DBClusterArn"),
                name=c["DBClusterIdentifier"],
                state=c.get("Status"),
                tags=c.get("TagList"),
                attrs={
                    "engine": c.get("Engine"),
                    "engine_version": c.get("EngineVersion"),
                    "engine_mode": c.get("EngineMode"),
                    "instance_class": c.get("DBClusterInstanceClass"),
                    "storage_type": c.get("StorageType"),
                    "allocated_storage_gb": c.get("AllocatedStorage"),
                    "multi_az": c.get("MultiAZ"),
                    "publicly_accessible": c.get("PubliclyAccessible"),
                    "storage_encrypted": c.get("StorageEncrypted"),
                    "kms_key_id": c.get("KmsKeyId"),
                    "backup_retention_days": c.get("BackupRetentionPeriod"),
                    "deletion_protection": c.get("DeletionProtection"),
                    "endpoint_host": c.get("Endpoint"),
                    "reader_endpoint_host": c.get("ReaderEndpoint"),
                    "port": c.get("Port"),
                    "serverless_v2": c.get("ServerlessV2ScalingConfiguration"),
                    "members": [m["DBInstanceIdentifier"] for m in c.get("DBClusterMembers", [])],
                },
                relations={
                    "vpc_id": grupo.get("vpc_id"),
                    "subnet_ids": grupo.get("subnet_ids", []),
                    "security_group_ids": [g["VpcSecurityGroupId"] for g in c.get("VpcSecurityGroups", [])],
                    "attached_to": [m["DBInstanceIdentifier"] for m in c.get("DBClusterMembers", [])],
                },
            ))
    return recursos
