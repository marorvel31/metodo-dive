"""Instancias EC2. Colector de referencia: los demás siguen esta misma forma."""

from common import recurso
from common.aws import client

NAME = "ec2"
TRIGGERS = ["Amazon Elastic Compute Cloud - Compute", "EC2 - Other"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []
    for pagina in ec2.get_paginator("describe_instances").paginate():
        for reserva in pagina["Reservations"]:
            for i in reserva["Instances"]:
                # UserData nunca se lee: describe_instances no lo trae, y no llamamos describe_instance_attribute.
                recursos.append(recurso(
                    account_id, region, NAME, "instance", i["InstanceId"],
                    arn=f"arn:aws:ec2:{region}:{account_id}:instance/{i['InstanceId']}",
                    state=i["State"]["Name"],
                    tags=i.get("Tags"),
                    attrs={
                        "instance_type": i.get("InstanceType"),
                        "platform": i.get("PlatformDetails"),
                        "az": i.get("Placement", {}).get("AvailabilityZone"),
                        "private_ip": i.get("PrivateIpAddress"),
                        "public_ip": i.get("PublicIpAddress"),
                        "launch_time": i.get("LaunchTime"),
                        # "User initiated (2025-01-31 10:00:00 GMT)": de aquí sale la fecha de detención.
                        "state_transition_reason": i.get("StateTransitionReason") or None,
                        "iam_profile": (i.get("IamInstanceProfile") or {}).get("Arn"),
                        "lifecycle": i.get("InstanceLifecycle", "on-demand"),
                    },
                    relations={
                        "vpc_id": i.get("VpcId"),
                        "subnet_ids": [i["SubnetId"]] if i.get("SubnetId") else [],
                        "security_group_ids": [g["GroupId"] for g in i.get("SecurityGroups", [])],
                        "attached_to": [b["Ebs"]["VolumeId"] for b in i.get("BlockDeviceMappings", []) if "Ebs" in b],
                    },
                ))
    return recursos
