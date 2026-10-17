"""AMIs propias y los snapshots que las respaldan (y que se siguen cobrando)."""

from common import recurso
from common.aws import client

NAME = "ami"
TRIGGERS = ["EC2 - Other"]  # el costo de una AMI son sus snapshots EBS
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []
    # Owners=self: sin él, describe_images lista miles de AMIs públicas.
    for pagina in ec2.get_paginator("describe_images").paginate(Owners=["self"]):
        for img in pagina["Images"]:
            snapshots = [b["Ebs"]["SnapshotId"] for b in img.get("BlockDeviceMappings", [])
                         if b.get("Ebs", {}).get("SnapshotId")]
            recursos.append(recurso(
                account_id, region, NAME, "image", img["ImageId"],
                arn=f"arn:aws:ec2:{region}::image/{img['ImageId']}",
                name=img.get("Name"),
                state=img.get("State"),
                tags=img.get("Tags"),
                attrs={
                    "creation_date": img.get("CreationDate"),
                    "deprecation_time": img.get("DeprecationTime"),
                    "architecture": img.get("Architecture"),
                    "platform": img.get("PlatformDetails"),
                    "root_device_type": img.get("RootDeviceType"),
                    "public": img.get("Public", False),
                    "snapshot_ids": snapshots,
                },
                relations={"attached_to": snapshots},
            ))
    return recursos
