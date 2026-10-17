"""VPN: Site-to-Site (conexiones, VGWs, customer gateways) y Client VPN.

Nunca se copian PSKs ni configuración de túnel: describe_vpn_connections trae
`CustomerGatewayConfiguration` (XML con llaves) y `Options.TunnelOptions[].PreSharedKey`;
solo se leen campos explícitos. De Client VPN no se copian certificados ni opciones de auth.
"""

from common import recurso
from common.aws import client

NAME = "vpn"
TRIGGERS = ["Amazon Virtual Private Cloud", "EC2 - Other"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []
    arn = lambda tipo, i: f"arn:aws:ec2:{region}:{account_id}:{tipo}/{i}"  # noqa: E731

    # Estas tres operaciones no tienen paginador: devuelven todo en una respuesta.
    cgws = ec2.describe_customer_gateways().get("CustomerGateways", [])
    ip_cgw = {c["CustomerGatewayId"]: c.get("IpAddress") for c in cgws}

    for v in ec2.describe_vpn_connections().get("VpnConnections", []):
        opciones = v.get("Options", {})
        tuneles = [{
            "outside_ip": t.get("OutsideIpAddress"),
            "status": t.get("Status"),
            "status_message": t.get("StatusMessage"),
            "last_change": t.get("LastStatusChange"),
            "accepted_routes": t.get("AcceptedRouteCount"),
        } for t in v.get("VgwTelemetry", [])]
        estatico = opciones.get("StaticRoutesOnly")
        recursos.append(recurso(
            account_id, region, NAME, "vpn_connection", v["VpnConnectionId"], arn=arn("vpn-connection", v["VpnConnectionId"]),
            state=v.get("State"), tags=v.get("Tags"),
            attrs={
                "type": v.get("Type"),
                "category": v.get("Category"),
                "routing": "static" if estatico else "bgp",
                "customer_gateway_ip": ip_cgw.get(v.get("CustomerGatewayId")),
                "tunnels": tuneles,
                "tunnels_up": sum(1 for t in tuneles if t["status"] == "UP"),
                "static_routes": [r.get("DestinationCidrBlock") for r in v.get("Routes", [])],
                "local_ipv4_cidr": opciones.get("LocalIpv4NetworkCidr"),
                "remote_ipv4_cidr": opciones.get("RemoteIpv4NetworkCidr"),
                "outside_ip_type": opciones.get("OutsideIpAddressType"),
            },
            relations={"attached_to": [x for x in (v.get("CustomerGatewayId"), v.get("VpnGatewayId"),
                                                   v.get("TransitGatewayId")) if x]},
        ))

    for g in ec2.describe_vpn_gateways().get("VpnGateways", []):
        vpcs = [a["VpcId"] for a in g.get("VpcAttachments", []) if a.get("State") == "attached"]
        recursos.append(recurso(
            account_id, region, NAME, "vpn_gateway", g["VpnGatewayId"], arn=arn("vpn-gateway", g["VpnGatewayId"]),
            state=g.get("State"), tags=g.get("Tags"),
            attrs={"amazon_side_asn": g.get("AmazonSideAsn"), "az": g.get("AvailabilityZone")},
            relations={"vpc_id": vpcs[0] if vpcs else None, "attached_to": vpcs},
        ))

    for c in cgws:
        # CertificateArn y DeviceName quedan fuera; la IP pública del CGW sí se guarda.
        recursos.append(recurso(
            account_id, region, NAME, "customer_gateway", c["CustomerGatewayId"],
            arn=arn("customer-gateway", c["CustomerGatewayId"]),
            state=c.get("State"), tags=c.get("Tags"),
            attrs={"ip_address": c.get("IpAddress"), "bgp_asn": c.get("BgpAsn"), "type": c.get("Type")},
        ))

    for pagina in ec2.get_paginator("describe_client_vpn_endpoints").paginate():
        for e in pagina.get("ClientVpnEndpoints", []):
            eid = e["ClientVpnEndpointId"]
            subnets = []
            for p in ec2.get_paginator("describe_client_vpn_target_networks").paginate(ClientVpnEndpointId=eid):
                subnets += [t["TargetNetworkId"] for t in p.get("ClientVpnTargetNetworks", []) if t.get("TargetNetworkId")]
            recursos.append(recurso(
                account_id, region, NAME, "client_vpn_endpoint", eid, arn=arn("client-vpn-endpoint", eid),
                state=(e.get("Status") or {}).get("Code"), tags=e.get("Tags"),
                attrs={
                    "description": e.get("Description"),
                    "client_cidr": e.get("ClientCidrBlock"),
                    "split_tunnel": e.get("SplitTunnel"),
                    "transport": e.get("TransportProtocol"),
                    "port": e.get("VpnPort"),
                    "auth_types": [a.get("Type") for a in e.get("AuthenticationOptions", [])],
                },
                relations={"vpc_id": e.get("VpcId"), "subnet_ids": subnets,
                           "security_group_ids": e.get("SecurityGroupIds", [])},
            ))

    return recursos
