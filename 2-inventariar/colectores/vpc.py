"""Red de la VPC: VPCs, subnets, route tables, IGW, NAT, endpoints, peerings y security groups.

La red casi no factura pero es la base de los diagramas, por eso corre siempre (ALWAYS).
Solo se emiten recursos cuyo dueño es esta cuenta: en VPCs compartidas (RAM) la cuenta
participante ve las subnets del dueño, y así evitamos duplicados.
"""

from common import recurso, tags_a_dict
from common.aws import client

NAME = "vpc"
TRIGGERS = ["Amazon Virtual Private Cloud", "EC2 - Other"]
ALWAYS = True
GLOBAL = False

MUNDO = {"0.0.0.0/0", "::/0"}

# Campo del target de una ruta → tipo legible. GatewayId se resuelve por prefijo.
_TARGETS = [
    ("NatGatewayId", "nat"),
    ("TransitGatewayId", "tgw"),
    ("VpcPeeringConnectionId", "peering"),
    ("NetworkInterfaceId", "eni"),
    ("InstanceId", "instance"),
    ("EgressOnlyInternetGatewayId", "eigw"),
    ("LocalGatewayId", "local-gateway"),
    ("CarrierGatewayId", "carrier"),
    ("CoreNetworkArn", "core-network"),
]
_PREFIJOS_GW = {"igw-": "igw", "vgw-": "vgw", "vpce-": "vpce"}


def _paginar(ec2, operacion, clave):
    for pagina in ec2.get_paginator(operacion).paginate():
        yield from pagina.get(clave, [])


def _propio(r, account_id):
    return r.get("OwnerId") in (None, account_id)


def resumir_ruta(ruta):
    """{'dest', 'target_type', 'target', 'state'} de una ruta de route table."""
    destino = ruta.get("DestinationCidrBlock") or ruta.get("DestinationIpv6CidrBlock") or ruta.get("DestinationPrefixListId")
    tipo, target = None, None
    gw = ruta.get("GatewayId")
    if gw:
        target = gw
        tipo = "local" if gw == "local" else next((t for p, t in _PREFIJOS_GW.items() if gw.startswith(p)), "gateway")
    else:
        for campo, t in _TARGETS:
            if ruta.get(campo):
                tipo, target = t, ruta[campo]
                break
    return {"dest": destino, "target_type": tipo, "target": target, "state": ruta.get("State")}


def _puertos(p):
    if p.get("IpProtocol") == "-1":
        return "all"
    desde, hasta = p.get("FromPort"), p.get("ToPort")
    if desde is None or desde == -1:
        return "all"
    return str(desde) if desde == hasta else f"{desde}-{hasta}"


def resumir_ingreso(permisos):
    """Aplana IpPermissions a [{'protocol', 'ports', 'source'}]; source es CIDR, sg-, o prefix list."""
    reglas = []
    for p in permisos:
        proto = "all" if p.get("IpProtocol") == "-1" else p.get("IpProtocol")
        puertos = _puertos(p)
        origenes = [r["CidrIp"] for r in p.get("IpRanges", [])]
        origenes += [r["CidrIpv6"] for r in p.get("Ipv6Ranges", [])]
        origenes += [r["PrefixListId"] for r in p.get("PrefixListIds", [])]
        origenes += [g["GroupId"] for g in p.get("UserIdGroupPairs", []) if g.get("GroupId")]
        for o in origenes:
            reglas.append({"protocol": proto, "ports": puertos, "source": o})
    return reglas


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []
    arn = lambda tipo, i: f"arn:aws:ec2:{region}:{account_id}:{tipo}/{i}"  # noqa: E731

    # Route tables primero: definen si una subnet es pública.
    tablas = [t for t in _paginar(ec2, "describe_route_tables", "RouteTables") if _propio(t, account_id)]
    tabla_de_subnet, tabla_principal = {}, {}
    for t in tablas:
        for a in t.get("Associations", []):
            if a.get("Main"):
                tabla_principal[t["VpcId"]] = t
            elif a.get("SubnetId"):
                tabla_de_subnet[a["SubnetId"]] = t

    def tiene_igw(t):
        return any((r.get("GatewayId") or "").startswith("igw-") for r in t.get("Routes", []))

    for v in _paginar(ec2, "describe_vpcs", "Vpcs"):
        if not _propio(v, account_id):
            continue
        recursos.append(recurso(
            account_id, region, NAME, "vpc", v["VpcId"], arn=arn("vpc", v["VpcId"]),
            state=v.get("State"), tags=v.get("Tags"),
            attrs={
                "cidr": v.get("CidrBlock"),
                "cidrs": [c["CidrBlock"] for c in v.get("CidrBlockAssociationSet", [])],
                "ipv6_cidrs": [c.get("Ipv6CidrBlock") for c in v.get("Ipv6CidrBlockAssociationSet", [])],
                "is_default": v.get("IsDefault", False),
                "tenancy": v.get("InstanceTenancy"),
            },
        ))

    for s in _paginar(ec2, "describe_subnets", "Subnets"):
        if not _propio(s, account_id):
            continue
        tabla = tabla_de_subnet.get(s["SubnetId"]) or tabla_principal.get(s["VpcId"])
        recursos.append(recurso(
            account_id, region, NAME, "subnet", s["SubnetId"], arn=s.get("SubnetArn"),
            state=s.get("State"), tags=s.get("Tags"),
            attrs={
                "cidr": s.get("CidrBlock"),
                "az": s.get("AvailabilityZone"),
                "public": bool(tabla and tiene_igw(tabla)),
                "map_public_ip": s.get("MapPublicIpOnLaunch", False),
                "available_ips": s.get("AvailableIpAddressCount"),
                "default_for_az": s.get("DefaultForAz", False),
                "route_table_id": tabla["RouteTableId"] if tabla else None,
            },
            relations={"vpc_id": s["VpcId"], "attached_to": [tabla["RouteTableId"]] if tabla else []},
        ))

    for t in tablas:
        rutas = [resumir_ruta(r) for r in t.get("Routes", [])]
        asociaciones = t.get("Associations", [])
        recursos.append(recurso(
            account_id, region, NAME, "route_table", t["RouteTableId"], arn=arn("route-table", t["RouteTableId"]),
            tags=t.get("Tags"),
            attrs={
                "main": any(a.get("Main") for a in asociaciones),
                "routes": rutas,
                "has_igw_route": tiene_igw(t),
                "gateway_ids": [a["GatewayId"] for a in asociaciones if a.get("GatewayId")],
            },
            relations={
                "vpc_id": t["VpcId"],
                "subnet_ids": [a["SubnetId"] for a in asociaciones if a.get("SubnetId")],
                "target_ids": sorted({r["target"] for r in rutas if r["target"] and r["target_type"] != "local"}),
            },
        ))

    for g in _paginar(ec2, "describe_internet_gateways", "InternetGateways"):
        if not _propio(g, account_id):
            continue
        vpcs = [a["VpcId"] for a in g.get("Attachments", []) if a.get("VpcId")]
        recursos.append(recurso(
            account_id, region, NAME, "internet_gateway", g["InternetGatewayId"],
            arn=arn("internet-gateway", g["InternetGatewayId"]),
            state=(g.get("Attachments") or [{}])[0].get("State", "detached"), tags=g.get("Tags"),
            relations={"vpc_id": vpcs[0] if vpcs else None, "attached_to": vpcs},
        ))

    for n in _paginar(ec2, "describe_nat_gateways", "NatGateways"):
        direcciones = n.get("NatGatewayAddresses", [])
        recursos.append(recurso(
            account_id, region, NAME, "nat_gateway", n["NatGatewayId"], arn=arn("natgateway", n["NatGatewayId"]),
            state=n.get("State"), tags=n.get("Tags"),
            attrs={
                "connectivity": n.get("ConnectivityType", "public"),
                "public_ips": [d["PublicIp"] for d in direcciones if d.get("PublicIp")],
                "private_ips": [d["PrivateIp"] for d in direcciones if d.get("PrivateIp")],
                "allocation_ids": [d["AllocationId"] for d in direcciones if d.get("AllocationId")],
            },
            relations={
                "vpc_id": n.get("VpcId"),
                "subnet_ids": [n["SubnetId"]] if n.get("SubnetId") else [],
                "attached_to": [d["NetworkInterfaceId"] for d in direcciones if d.get("NetworkInterfaceId")],
            },
        ))

    for e in _paginar(ec2, "describe_vpc_endpoints", "VpcEndpoints"):
        if not _propio(e, account_id):
            continue
        # PolicyDocument no se copia: no es secreto, pero es ruido para el inventario.
        recursos.append(recurso(
            account_id, region, NAME, "vpc_endpoint", e["VpcEndpointId"], arn=arn("vpc-endpoint", e["VpcEndpointId"]),
            state=e.get("State"), tags=e.get("Tags"),
            attrs={
                "service_name": e.get("ServiceName"),
                "endpoint_type": e.get("VpcEndpointType"),
                "private_dns": e.get("PrivateDnsEnabled"),
            },
            relations={
                "vpc_id": e.get("VpcId"),
                "subnet_ids": e.get("SubnetIds", []),
                "security_group_ids": [g["GroupId"] for g in e.get("Groups", [])],
                "attached_to": e.get("RouteTableIds", []),
            },
        ))

    for p in _paginar(ec2, "describe_vpc_peering_connections", "VpcPeeringConnections"):
        lados = {}
        for lado, clave in (("requester", "RequesterVpcInfo"), ("accepter", "AccepterVpcInfo")):
            info = p.get(clave, {})
            lados[lado] = {
                "account_id": info.get("OwnerId"),
                "vpc_id": info.get("VpcId"),
                "region": info.get("Region"),
                "cidr": info.get("CidrBlock"),
                "cidrs": [c["CidrBlock"] for c in info.get("CidrBlockSet", [])],
            }
        propia = next((lados[l]["vpc_id"] for l in ("requester", "accepter") if lados[l]["account_id"] == account_id), None)
        recursos.append(recurso(
            account_id, region, NAME, "peering_connection", p["VpcPeeringConnectionId"],
            arn=arn("vpc-peering-connection", p["VpcPeeringConnectionId"]),
            state=p.get("Status", {}).get("Code"), tags=p.get("Tags"),
            attrs={**lados, "cross_account": lados["requester"]["account_id"] != lados["accepter"]["account_id"]},
            relations={
                "vpc_id": propia,
                "target_ids": [x for x in (lados["requester"]["vpc_id"], lados["accepter"]["vpc_id"]) if x],
            },
        ))

    for g in _paginar(ec2, "describe_security_groups", "SecurityGroups"):
        if not _propio(g, account_id):
            continue
        ingreso = resumir_ingreso(g.get("IpPermissions", []))
        abiertas = [r for r in ingreso if r["source"] in MUNDO]
        recursos.append(recurso(
            account_id, region, NAME, "security_group", g["GroupId"], arn=arn("security-group", g["GroupId"]),
            name=tags_a_dict(g.get("Tags")).get("Name") or g.get("GroupName"), tags=g.get("Tags"),
            attrs={
                "description": g.get("Description"),
                "is_default": g.get("GroupName") == "default",
                "ingress": ingreso,
                "open_to_world": bool(abiertas),
                "open_to_world_ports": sorted({f"{r['protocol']}/{r['ports']}" for r in abiertas}),
                "ingress_source_sgs": sorted({r["source"] for r in ingreso if r["source"].startswith("sg-")}),
                "egress_rule_count": len(g.get("IpPermissionsEgress", [])),
            },
            relations={"vpc_id": g.get("VpcId")},
        ))

    return recursos
