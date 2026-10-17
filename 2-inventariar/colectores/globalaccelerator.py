"""Aceleradores de Global Accelerator (global; la API solo existe en us-west-2)."""

from common import recurso
from common.aws import client

NAME = "globalaccelerator"
TRIGGERS = ["AWS Global Accelerator"]
ALWAYS = False
GLOBAL = True

REGION_API = "us-west-2"


def collect(session, region, account_id):
    ga = client(session, "globalaccelerator", REGION_API)
    recursos = []
    for pagina in ga.get_paginator("list_accelerators").paginate():
        for a in pagina.get("Accelerators", []):
            listeners, endpoints = [], []
            for p in ga.get_paginator("list_listeners").paginate(AcceleratorArn=a["AcceleratorArn"]):
                for li in p.get("Listeners", []):
                    grupos = [g for q in ga.get_paginator("list_endpoint_groups").paginate(ListenerArn=li["ListenerArn"])
                              for g in q.get("EndpointGroups", [])]
                    listeners.append({
                        "protocol": li.get("Protocol"),
                        "ports": [f"{r['FromPort']}-{r['ToPort']}" if r["FromPort"] != r["ToPort"] else str(r["FromPort"])
                                  for r in li.get("PortRanges", [])],
                        "endpoint_groups": [{
                            "region": g.get("EndpointGroupRegion"),
                            "endpoints": [{"id": e.get("EndpointId"), "weight": e.get("Weight"), "health": e.get("HealthState")}
                                          for e in g.get("EndpointDescriptions", [])],
                        } for g in grupos],
                    })
                    endpoints += [e["EndpointId"] for g in grupos for e in g.get("EndpointDescriptions", []) if e.get("EndpointId")]
            recursos.append(recurso(
                account_id, "global", NAME, "accelerator", a["AcceleratorArn"], arn=a["AcceleratorArn"],
                name=a.get("Name"), state=a.get("Status"),
                attrs={
                    "enabled": a.get("Enabled"),
                    "dns_name": a.get("DnsName"),
                    "ip_type": a.get("IpAddressType"),
                    "ips": [ip for s in a.get("IpSets", []) for ip in s.get("IpAddresses", [])],
                    "listeners": listeners,
                },
                relations={"target_ids": endpoints},
            ))
    return recursos
