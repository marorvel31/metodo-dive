"""EFS: file systems y sus mount targets (subnets y security groups)."""

from common import recurso
from common.aws import client

NAME = "efs"
TRIGGERS = ["Amazon Elastic File System"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    efs = client(session, "efs", region)
    recursos = []
    for pagina in efs.get_paginator("describe_file_systems").paginate():
        for fs in pagina["FileSystems"]:
            fs_id = fs["FileSystemId"]
            mts = []
            for p in efs.get_paginator("describe_mount_targets").paginate(FileSystemId=fs_id):
                mts.extend(p["MountTargets"])

            for mt in mts:
                sgs = efs.describe_mount_target_security_groups(MountTargetId=mt["MountTargetId"])["SecurityGroups"]
                recursos.append(recurso(
                    account_id, region, NAME, "mount_target", mt["MountTargetId"],
                    state=mt.get("LifeCycleState"),
                    attrs={"az": mt.get("AvailabilityZoneName"), "ip": mt.get("IpAddress"), "file_system_id": fs_id},
                    relations={
                        "vpc_id": mt.get("VpcId"),
                        "subnet_ids": [mt["SubnetId"]] if mt.get("SubnetId") else [],
                        "security_group_ids": sgs,
                        "attached_to": [fs_id],
                    },
                ))

            recursos.append(recurso(
                account_id, region, NAME, "file_system", fs_id,
                arn=fs.get("FileSystemArn"),
                name=fs.get("Name"),
                state=fs.get("LifeCycleState"),
                tags=fs.get("Tags"),
                attrs={
                    "size_bytes": (fs.get("SizeInBytes") or {}).get("Value"),
                    "performance_mode": fs.get("PerformanceMode"),
                    "throughput_mode": fs.get("ThroughputMode"),
                    "provisioned_mibps": fs.get("ProvisionedThroughputInMibps"),
                    "encrypted": fs.get("Encrypted"),
                    "kms_key_id": fs.get("KmsKeyId"),
                    "one_zone_az": fs.get("AvailabilityZoneName"),
                    "mount_targets": len(mts),
                },
                relations={
                    "vpc_id": mts[0].get("VpcId") if mts else None,
                    "subnet_ids": sorted({m["SubnetId"] for m in mts if m.get("SubnetId")}),
                    "attached_to": [m["MountTargetId"] for m in mts],
                },
            ))
    return recursos
