"""Volúmenes EBS y snapshots propios."""

from common import recurso
from common.aws import client

NAME = "ebs"
TRIGGERS = ["EC2 - Other"]  # EBS (volúmenes y snapshots) se factura bajo "EC2 - Other"
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []
    for pagina in ec2.get_paginator("describe_volumes").paginate():
        for v in pagina["Volumes"]:
            adjuntos = [a["InstanceId"] for a in v.get("Attachments", []) if a.get("InstanceId")]
            recursos.append(recurso(
                account_id, region, NAME, "volume", v["VolumeId"],
                arn=f"arn:aws:ec2:{region}:{account_id}:volume/{v['VolumeId']}",
                state=v.get("State"),  # "available" = sin adjuntar (desperdicio)
                tags=v.get("Tags"),
                attrs={
                    "size_gb": v.get("Size"),
                    "volume_type": v.get("VolumeType"),
                    "iops": v.get("Iops"),
                    "throughput": v.get("Throughput"),
                    "encrypted": v.get("Encrypted", False),
                    "az": v.get("AvailabilityZone"),
                    "snapshot_id": v.get("SnapshotId") or None,
                    "create_time": v.get("CreateTime"),
                },
                relations={"attached_to": adjuntos},
            ))

    # Solo snapshots de la cuenta: sin OwnerIds saldrían también los públicos de todo AWS.
    for pagina in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        for s in pagina["Snapshots"]:
            recursos.append(recurso(
                account_id, region, NAME, "snapshot", s["SnapshotId"],
                arn=f"arn:aws:ec2:{region}::snapshot/{s['SnapshotId']}",
                state=s.get("State"),
                tags=s.get("Tags"),
                attrs={
                    "size_gb": s.get("VolumeSize"),
                    "start_time": s.get("StartTime"),
                    "encrypted": s.get("Encrypted", False),
                    "storage_tier": s.get("StorageTier"),
                    "description": s.get("Description"),
                },
                # vol-ffffffff es el marcador de AWS para snapshots copiados/sin volumen origen.
                relations={"attached_to": [s["VolumeId"]] if s.get("VolumeId") and s["VolumeId"] != "vol-ffffffff" else []},
            ))
    return recursos
