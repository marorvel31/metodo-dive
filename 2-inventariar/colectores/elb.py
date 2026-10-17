"""Load balancers: ALB/NLB/GWLB (elbv2) con listeners y target groups, y Classic ELB."""

from common import recurso
from common.aws import client

NAME = "elb"
TRIGGERS = ["Amazon Elastic Load Balancing"]
ALWAYS = False
GLOBAL = False


def _tags_por_arn(cliente, arns, clave):
    """describe_tags acepta 20 recursos por llamada y no tiene paginador."""
    tags = {}
    for i in range(0, len(arns), 20):
        lote = arns[i:i + 20]
        for d in cliente.describe_tags(**{clave: lote}).get("TagDescriptions", []):
            tags[d.get("ResourceArn") or d.get("LoadBalancerName")] = d.get("Tags", [])
    return tags


def _elbv2(session, region, account_id):
    elb = client(session, "elbv2", region)
    recursos = []

    lbs = [lb for p in elb.get_paginator("describe_load_balancers").paginate() for lb in p["LoadBalancers"]]
    tgs = [tg for p in elb.get_paginator("describe_target_groups").paginate() for tg in p["TargetGroups"]]
    tags = _tags_por_arn(elb, [x["LoadBalancerArn"] for x in lbs] + [x["TargetGroupArn"] for x in tgs], "ResourceArns")

    tgs_de_lb = {}
    for tg in tgs:
        for lb_arn in tg.get("LoadBalancerArns", []):
            tgs_de_lb.setdefault(lb_arn, []).append(tg["TargetGroupArn"])

    for lb in lbs:
        arn = lb["LoadBalancerArn"]
        listeners = []
        for p in elb.get_paginator("describe_listeners").paginate(LoadBalancerArn=arn):
            for li in p["Listeners"]:
                acciones = li.get("DefaultActions", [])
                listeners.append({
                    "port": li.get("Port"),
                    "protocol": li.get("Protocol"),
                    "ssl_policy": li.get("SslPolicy"),
                    "certificate_arns": [c["CertificateArn"] for c in li.get("Certificates", []) if c.get("CertificateArn")],
                    "default_actions": [a.get("Type") for a in acciones],
                    "default_target_groups": [a["TargetGroupArn"] for a in acciones if a.get("TargetGroupArn")],
                })
        recursos.append(recurso(
            account_id, region, NAME, "load_balancer", arn, arn=arn,
            name=lb.get("LoadBalancerName"), state=(lb.get("State") or {}).get("Code"), tags=tags.get(arn),
            attrs={
                "lb_type": lb.get("Type"),
                "scheme": lb.get("Scheme"),
                "dns_name": lb.get("DNSName"),
                "ip_address_type": lb.get("IpAddressType"),
                "azs": [z.get("ZoneName") for z in lb.get("AvailabilityZones", [])],
                "listeners": listeners,
            },
            relations={
                "vpc_id": lb.get("VpcId"),
                "subnet_ids": [z["SubnetId"] for z in lb.get("AvailabilityZones", []) if z.get("SubnetId")],
                "security_group_ids": lb.get("SecurityGroups", []),
                "target_ids": tgs_de_lb.get(arn, []),
            },
        ))

    for tg in tgs:
        arn = tg["TargetGroupArn"]
        # describe_target_health no tiene paginador.
        salud = elb.describe_target_health(TargetGroupArn=arn).get("TargetHealthDescriptions", [])
        targets = [{
            "id": s["Target"]["Id"],
            "port": s["Target"].get("Port"),
            "az": s["Target"].get("AvailabilityZone"),
            "health": (s.get("TargetHealth") or {}).get("State"),
        } for s in salud]
        recursos.append(recurso(
            account_id, region, NAME, "target_group", arn, arn=arn,
            name=tg.get("TargetGroupName"), tags=tags.get(arn),
            attrs={
                "target_type": tg.get("TargetType"),
                "protocol": tg.get("Protocol"),
                "port": tg.get("Port"),
                "health_check": {"protocol": tg.get("HealthCheckProtocol"), "path": tg.get("HealthCheckPath"),
                                 "port": tg.get("HealthCheckPort")},
                "targets": targets,
                "healthy": sum(1 for t in targets if t["health"] == "healthy"),
            },
            relations={
                "vpc_id": tg.get("VpcId"),
                "target_ids": [t["id"] for t in targets],
                "attached_to": tg.get("LoadBalancerArns", []),
            },
        ))
    return recursos


def _classic(session, region, account_id):
    elb = client(session, "elb", region)
    recursos = []
    lbs = [lb for p in elb.get_paginator("describe_load_balancers").paginate() for lb in p["LoadBalancerDescriptions"]]
    tags = _tags_por_arn(elb, [lb["LoadBalancerName"] for lb in lbs], "LoadBalancerNames")
    for lb in lbs:
        nombre = lb["LoadBalancerName"]
        recursos.append(recurso(
            account_id, region, NAME, "classic_load_balancer", nombre,
            arn=f"arn:aws:elasticloadbalancing:{region}:{account_id}:loadbalancer/{nombre}",
            name=nombre, tags=tags.get(nombre),
            attrs={
                "lb_type": "classic",
                "scheme": lb.get("Scheme"),
                "dns_name": lb.get("DNSName"),
                "azs": lb.get("AvailabilityZones", []),
                "listeners": [{
                    "port": d["Listener"].get("LoadBalancerPort"),
                    "protocol": d["Listener"].get("Protocol"),
                    "instance_port": d["Listener"].get("InstancePort"),
                    "certificate_arns": [d["Listener"]["SSLCertificateId"]] if d["Listener"].get("SSLCertificateId") else [],
                } for d in lb.get("ListenerDescriptions", [])],
                "health_check_target": (lb.get("HealthCheck") or {}).get("Target"),
            },
            relations={
                "vpc_id": lb.get("VPCId"),
                "subnet_ids": lb.get("Subnets", []),
                "security_group_ids": lb.get("SecurityGroups", []),
                "target_ids": [i["InstanceId"] for i in lb.get("Instances", [])],
            },
        ))
    return recursos


def collect(session, region, account_id):
    return _elbv2(session, region, account_id) + _classic(session, region, account_id)
