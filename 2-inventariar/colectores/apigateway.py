"""API Gateway: REST (v1), HTTP/WebSocket (v2), dominios custom y VPC links.

De los stages solo se guarda el nombre: las stage variables pueden llevar secretos.
"""

from common import recurso
from common.aws import client

NAME = "apigateway"
TRIGGERS = ["Amazon API Gateway"]
ALWAYS = False
GLOBAL = False


def _rest(session, region, account_id):
    api = client(session, "apigateway", region)
    recursos = []
    base = f"arn:aws:apigateway:{region}::"

    for p in api.get_paginator("get_rest_apis").paginate():
        for a in p.get("items", []):
            conf = a.get("endpointConfiguration") or {}
            # get_stages no tiene paginador.
            stages = [s["stageName"] for s in api.get_stages(restApiId=a["id"]).get("item", [])]
            recursos.append(recurso(
                account_id, region, NAME, "rest_api", a["id"], arn=f"{base}/restapis/{a['id']}",
                name=a.get("name"), tags=a.get("tags"),
                attrs={
                    "protocol": "REST",
                    "endpoint_types": conf.get("types", []),
                    "stages": stages,
                    "disable_default_endpoint": a.get("disableExecuteApiEndpoint", False),
                    "description": a.get("description"),
                },
                relations={"attached_to": conf.get("vpcEndpointIds", [])},
            ))

    for p in api.get_paginator("get_domain_names").paginate():
        for d in p.get("items", []):
            nombre = d["domainName"]
            mapeos = [m for q in api.get_paginator("get_base_path_mappings").paginate(domainName=nombre)
                      for m in q.get("items", [])]
            recursos.append(recurso(
                account_id, region, NAME, "rest_domain", nombre, arn=f"{base}/domainnames/{nombre}",
                name=nombre, state=d.get("domainNameStatus"), tags=d.get("tags"),
                attrs={
                    "endpoint_types": (d.get("endpointConfiguration") or {}).get("types", []),
                    "target_domain": d.get("regionalDomainName") or d.get("distributionDomainName"),
                    "certificate_arn": d.get("regionalCertificateArn") or d.get("certificateArn"),
                    "mappings": [{"path": m.get("basePath"), "api_id": m.get("restApiId"), "stage": m.get("stage")}
                                 for m in mapeos],
                },
                relations={"target_ids": sorted({m["restApiId"] for m in mapeos if m.get("restApiId")})},
            ))

    for p in api.get_paginator("get_vpc_links").paginate():
        for v in p.get("items", []):
            recursos.append(recurso(
                account_id, region, NAME, "rest_vpc_link", v["id"], arn=f"{base}/vpclinks/{v['id']}",
                name=v.get("name"), state=v.get("status"), tags=v.get("tags"),
                relations={"target_ids": v.get("targetArns", [])},
            ))
    return recursos


def _v2(session, region, account_id):
    api = client(session, "apigatewayv2", region)
    recursos = []
    base = f"arn:aws:apigateway:{region}::"

    for p in api.get_paginator("get_apis").paginate():
        for a in p.get("Items", []):
            stages = [s["StageName"] for q in api.get_paginator("get_stages").paginate(ApiId=a["ApiId"])
                      for s in q.get("Items", [])]
            recursos.append(recurso(
                account_id, region, NAME, "http_api" if a.get("ProtocolType") == "HTTP" else "websocket_api",
                a["ApiId"], arn=f"{base}/apis/{a['ApiId']}",
                name=a.get("Name"), tags=a.get("Tags"),
                attrs={
                    "protocol": a.get("ProtocolType"),
                    "endpoint": a.get("ApiEndpoint"),
                    "stages": stages,
                    "disable_default_endpoint": a.get("DisableExecuteApiEndpoint", False),
                    "description": a.get("Description"),
                },
            ))

    for p in api.get_paginator("get_domain_names").paginate():
        for d in p.get("Items", []):
            nombre = d["DomainName"]
            # get_api_mappings no tiene paginador; se sigue NextToken a mano.
            mapeos, token = [], None
            while True:
                kw = {"DomainName": nombre, **({"NextToken": token} if token else {})}
                resp = api.get_api_mappings(**kw)
                mapeos += resp.get("Items", [])
                token = resp.get("NextToken")
                if not token:
                    break
            conf = (d.get("DomainNameConfigurations") or [{}])[0]
            recursos.append(recurso(
                account_id, region, NAME, "v2_domain", nombre, arn=f"{base}/domainnames/{nombre}",
                name=nombre, state=conf.get("DomainNameStatus"), tags=d.get("Tags"),
                attrs={
                    "endpoint_type": conf.get("EndpointType"),
                    "target_domain": conf.get("ApiGatewayDomainName"),
                    "certificate_arn": conf.get("CertificateArn"),
                    "mappings": [{"path": m.get("ApiMappingKey"), "api_id": m.get("ApiId"), "stage": m.get("Stage")}
                                 for m in mapeos],
                },
                relations={"target_ids": sorted({m["ApiId"] for m in mapeos if m.get("ApiId")})},
            ))

    token = None
    while True:
        resp = api.get_vpc_links(**({"NextToken": token} if token else {}))
        for v in resp.get("Items", []):
            recursos.append(recurso(
                account_id, region, NAME, "v2_vpc_link", v["VpcLinkId"], arn=f"{base}/vpclinks/{v['VpcLinkId']}",
                name=v.get("Name"), state=v.get("VpcLinkStatus"), tags=v.get("Tags"),
                relations={"subnet_ids": v.get("SubnetIds", []), "security_group_ids": v.get("SecurityGroupIds", [])},
            ))
        token = resp.get("NextToken")
        if not token:
            break
    return recursos


def collect(session, region, account_id):
    return _rest(session, region, account_id) + _v2(session, region, account_id)
