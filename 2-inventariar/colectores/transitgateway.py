"""Transit Gateways: TGWs, attachments y route tables (resumen de rutas).

Corre siempre: en un TGW compartido por RAM la cuenta dueña puede no tener costo de VPC,
porque las horas de attachment se cobran a la cuenta de cada VPC.
Un attachment aparece en la cuenta dueña del TGW y en la dueña del recurso; se emite en ambas
con `attrs.seen_from` para que la fase 3/4 pueda deduplicar por id.
"""

from common import recurso
from common.aws import client

NAME = "transitgateway"
TRIGGERS = ["Amazon Virtual Private Cloud", "EC2 - Other"]
ALWAYS = True
GLOBAL = False

MAX_RUTAS = 200  # por route table; si hay más se marca `routes_truncated`


def _paginar(ec2, operacion, clave, **kw):
    for pagina in ec2.get_paginator(operacion).paginate(**kw):
        yield from pagina.get(clave, [])


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []

    propios = set()
    for t in _paginar(ec2, "describe_transit_gateways", "TransitGateways"):
        if t.get("OwnerId") != account_id:
            continue  # TGW compartido: lo inventaría su cuenta dueña
        propios.add(t["TransitGatewayId"])
        op = t.get("Options", {})
        recursos.append(recurso(
            account_id, region, NAME, "transit_gateway", t["TransitGatewayId"], arn=t.get("TransitGatewayArn"),
            state=t.get("State"), tags=t.get("Tags"),
            attrs={
                "description": t.get("Description"),
                "amazon_side_asn": op.get("AmazonSideAsn"),
                "default_association_route_table": op.get("AssociationDefaultRouteTableId"),
                "default_propagation_route_table": op.get("PropagationDefaultRouteTableId"),
                "auto_accept_shared_attachments": op.get("AutoAcceptSharedAttachments"),
                "cidr_blocks": op.get("TransitGatewayCidrBlocks", []),
            },
        ))

    # Subnets de los attachments de VPC (describe_transit_gateway_attachments no las trae).
    subnets_vpc = {
        a["TransitGatewayAttachmentId"]: a.get("SubnetIds", [])
        for a in _paginar(ec2, "describe_transit_gateway_vpc_attachments", "TransitGatewayVpcAttachments")
    }

    for a in _paginar(ec2, "describe_transit_gateway_attachments", "TransitGatewayAttachments"):
        tipo = a.get("ResourceType")
        es_vpc = tipo == "vpc"
        dueno_tgw = a.get("TransitGatewayOwnerId")
        recursos.append(recurso(
            account_id, region, NAME, "tgw_attachment", a["TransitGatewayAttachmentId"],
            arn=f"arn:aws:ec2:{region}:{dueno_tgw or account_id}:transit-gateway-attachment/{a['TransitGatewayAttachmentId']}",
            state=a.get("State"), tags=a.get("Tags"),
            attrs={
                "resource_type": tipo,
                "resource_id": a.get("ResourceId"),
                "resource_owner_id": a.get("ResourceOwnerId"),
                "transit_gateway_owner_id": dueno_tgw,
                "route_table_id": (a.get("Association") or {}).get("TransitGatewayRouteTableId"),
                "seen_from": "tgw_owner" if dueno_tgw == account_id else "resource_owner",
            },
            relations={
                "vpc_id": a.get("ResourceId") if es_vpc else None,
                "subnet_ids": subnets_vpc.get(a["TransitGatewayAttachmentId"], []),
                "attached_to": [a["TransitGatewayId"]],
                "target_ids": [a["ResourceId"]] if a.get("ResourceId") else [],
            },
        ))

    for rt in _paginar(ec2, "describe_transit_gateway_route_tables", "TransitGatewayRouteTables"):
        if rt["TransitGatewayId"] not in propios:
            continue
        rt_id = rt["TransitGatewayRouteTableId"]
        asociados = [x["TransitGatewayAttachmentId"] for x in _paginar(
            ec2, "get_transit_gateway_route_table_associations", "Associations", TransitGatewayRouteTableId=rt_id)]
        propagados = [x["TransitGatewayAttachmentId"] for x in _paginar(
            ec2, "get_transit_gateway_route_table_propagations", "TransitGatewayRouteTablePropagations",
            TransitGatewayRouteTableId=rt_id)]
        # search_transit_gateway_routes no tiene paginador; devuelve hasta MaxResults.
        resp = ec2.search_transit_gateway_routes(
            TransitGatewayRouteTableId=rt_id,
            Filters=[{"Name": "state", "Values": ["active", "blackhole"]}],
            MaxResults=MAX_RUTAS,
        )
        rutas = []
        for r in resp.get("Routes", []):
            destinos = r.get("TransitGatewayAttachments", [])
            rutas.append({
                "dest": r.get("DestinationCidrBlock") or r.get("PrefixListId"),
                "type": r.get("Type"),
                "state": r.get("State"),
                "targets": [{"attachment_id": d.get("TransitGatewayAttachmentId"), "resource_type": d.get("ResourceType"),
                             "resource_id": d.get("ResourceId")} for d in destinos],
            })
        recursos.append(recurso(
            account_id, region, NAME, "tgw_route_table", rt_id,
            arn=f"arn:aws:ec2:{region}:{account_id}:transit-gateway-route-table/{rt_id}",
            state=rt.get("State"), tags=rt.get("Tags"),
            attrs={
                "default_association": rt.get("DefaultAssociationRouteTable"),
                "default_propagation": rt.get("DefaultPropagationRouteTable"),
                "routes": rutas,
                "routes_truncated": bool(resp.get("AdditionalRoutesAvailable")),
                "propagating_attachments": propagados,
            },
            relations={"attached_to": [rt["TransitGatewayId"]], "target_ids": asociados},
        ))

    return recursos
