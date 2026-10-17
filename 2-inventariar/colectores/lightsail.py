"""Instancias Lightsail."""

from common import recurso
from common.aws import client

NAME = "lightsail"
TRIGGERS = ["Amazon Lightsail"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ls = client(session, "lightsail", region)
    recursos = []
    for pagina in ls.get_paginator("get_instances").paginate():
        for i in pagina["instances"]:
            hw = i.get("hardware") or {}
            recursos.append(recurso(
                account_id, region, NAME, "instance", i["name"],
                arn=i.get("arn"),
                name=i["name"],
                state=(i.get("state") or {}).get("name"),
                tags=i.get("tags"),
                attrs={
                    "bundle_id": i.get("bundleId"),
                    "blueprint": i.get("blueprintId"),
                    "cpu_count": hw.get("cpuCount"),
                    "ram_gb": hw.get("ramSizeInGb"),
                    "disk_gb": sum(d.get("sizeInGb", 0) for d in hw.get("disks", [])),
                    "az": (i.get("location") or {}).get("availabilityZone"),
                    "private_ip": i.get("privateIpAddress"),
                    "public_ip": i.get("publicIpAddress"),
                    "static_ip": i.get("isStaticIp", False),
                    "created_at": i.get("createdAt"),
                },
            ))
    return recursos
