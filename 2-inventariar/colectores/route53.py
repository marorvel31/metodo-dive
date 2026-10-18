"""Hosted zones de Route 53 (global). No se listan registros, solo el conteo."""

from common import recurso
from common.aws import client

NAME = "route53"
TRIGGERS = ["Amazon Route 53"]
ALWAYS = False
GLOBAL = True
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def collect(session, region, account_id):
    r53 = client(session, "route53", "us-east-1")
    recursos = []
    for pagina in r53.get_paginator("list_hosted_zones").paginate():
        for z in pagina["HostedZones"]:
            zid = z["Id"].split("/")[-1]
            privada = (z.get("Config") or {}).get("PrivateZone", False)
            vpcs = []
            if privada:
                # get_hosted_zone es la única forma de ver las VPCs asociadas (sin paginador).
                vpcs = r53.get_hosted_zone(Id=zid).get("VPCs", [])
            recursos.append(recurso(
                account_id, "global", NAME, "hosted_zone", zid, arn=f"arn:aws:route53:::hostedzone/{zid}",
                name=z.get("Name", "").rstrip("."),
                attrs={
                    "private": privada,
                    "record_count": z.get("ResourceRecordSetCount"),
                    "comment": (z.get("Config") or {}).get("Comment"),
                    "vpcs": [{"vpc_id": v.get("VPCId"), "region": v.get("VPCRegion")} for v in vpcs],
                    "linked_service": (z.get("LinkedService") or {}).get("ServicePrincipal"),
                },
                relations={"attached_to": [v["VPCId"] for v in vpcs if v.get("VPCId")]},
            ))
    return recursos
