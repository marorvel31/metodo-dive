"""Auto Scaling Groups. No facturan por sí mismos: lo que cobra son sus instancias EC2."""

from common import recurso
from common.aws import client

NAME = "autoscaling"
TRIGGERS = ["Amazon Elastic Compute Cloud - Compute", "EC2 - Other"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    asg = client(session, "autoscaling", region)
    recursos = []
    for pagina in asg.get_paginator("describe_auto_scaling_groups").paginate():
        for g in pagina["AutoScalingGroups"]:
            # La plantilla puede venir directa o dentro de MixedInstancesPolicy.
            lt = g.get("LaunchTemplate") or (
                ((g.get("MixedInstancesPolicy") or {}).get("LaunchTemplate") or {}).get("LaunchTemplateSpecification") or {})
            subredes = [s for s in (g.get("VPCZoneIdentifier") or "").split(",") if s]
            recursos.append(recurso(
                account_id, region, NAME, "auto_scaling_group", g["AutoScalingGroupName"],
                arn=g.get("AutoScalingGroupARN"),
                name=g["AutoScalingGroupName"],
                state=g.get("Status") or "active",  # Status solo aparece al borrarse
                tags=g.get("Tags"),
                attrs={
                    "min_size": g.get("MinSize"),
                    "max_size": g.get("MaxSize"),
                    "desired_capacity": g.get("DesiredCapacity"),
                    "launch_template_id": lt.get("LaunchTemplateId"),
                    "launch_template_name": lt.get("LaunchTemplateName"),
                    "launch_template_version": lt.get("Version"),
                    "launch_configuration": g.get("LaunchConfigurationName"),
                    "mixed_instances": "MixedInstancesPolicy" in g,
                    "health_check_type": g.get("HealthCheckType"),
                    "azs": g.get("AvailabilityZones", []),
                },
                relations={
                    "subnet_ids": subredes,
                    "target_ids": g.get("TargetGroupARNs", []) + g.get("LoadBalancerNames", []),
                    "attached_to": [i["InstanceId"] for i in g.get("Instances", [])],
                },
            ))
    return recursos
