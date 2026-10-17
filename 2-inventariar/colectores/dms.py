"""DMS: instancias de replicación y tareas. Los endpoints no se leen (traen datos de conexión)."""

from common import recurso
from common.aws import client

NAME = "dms"
TRIGGERS = ["AWS Database Migration Service"]
ALWAYS = False
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def collect(session, region, account_id):
    dms = client(session, "dms", region)
    recursos = []
    for pagina in dms.get_paginator("describe_replication_instances").paginate():
        for r in pagina["ReplicationInstances"]:
            grupo = r.get("ReplicationSubnetGroup") or {}
            recursos.append(recurso(
                account_id, region, NAME, "replication_instance", r["ReplicationInstanceIdentifier"],
                arn=r.get("ReplicationInstanceArn"),
                name=r["ReplicationInstanceIdentifier"],
                state=r.get("ReplicationInstanceStatus"),
                attrs={
                    "instance_class": r.get("ReplicationInstanceClass"),
                    "engine_version": r.get("EngineVersion"),
                    "storage_gb": r.get("AllocatedStorage"),
                    "multi_az": r.get("MultiAZ"),
                    "publicly_accessible": r.get("PubliclyAccessible"),
                    "kms_key_id": r.get("KmsKeyId"),
                },
                relations={
                    "vpc_id": grupo.get("VpcId"),
                    "subnet_ids": [s["SubnetIdentifier"] for s in grupo.get("Subnets", [])],
                    "security_group_ids": [g["VpcSecurityGroupId"] for g in r.get("VpcSecurityGroups", [])],
                },
            ))

    # WithoutSettings: los settings de la tarea no hacen falta y pesan.
    for pagina in dms.get_paginator("describe_replication_tasks").paginate(WithoutSettings=True):
        for t in pagina["ReplicationTasks"]:
            recursos.append(recurso(
                account_id, region, NAME, "replication_task", t["ReplicationTaskIdentifier"],
                arn=t.get("ReplicationTaskArn"),
                name=t["ReplicationTaskIdentifier"],
                state=t.get("Status"),
                attrs={
                    "migration_type": t.get("MigrationType"),
                    "last_stop_reason": t.get("StopReason"),
                },
                relations={"attached_to": [t["ReplicationInstanceArn"]] if t.get("ReplicationInstanceArn") else []},
            ))
    return recursos
