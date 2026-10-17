"""Web ACLs de WAFv2 con scope CLOUDFRONT (viven en us-east-1, una vez por cuenta).

list_resources_for_web_acl no aplica a CloudFront: las distribuciones asociadas salen de
cloudfront.list_distributions_by_web_acl_id (con WAFv2 el id es el ARN de la ACL).
"""

from common import recurso
from common.aws import client

from colectores.waf import listar_acls, resumen_acl

NAME = "waf_cloudfront"
TRIGGERS = ["AWS WAF", "Amazon CloudFront"]
ALWAYS = False
GLOBAL = True
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def _distribuciones(cf, arn):
    ids, marker = [], None
    while True:
        resp = cf.list_distributions_by_web_acl_id(WebACLId=arn, **({"Marker": marker} if marker else {}))
        lista = resp.get("DistributionList") or {}
        ids += [d["Id"] for d in lista.get("Items", [])]
        marker = lista.get("NextMarker")
        if not lista.get("IsTruncated") or not marker:
            return ids


def collect(session, region, account_id):
    waf = client(session, "wafv2", "us-east-1")
    cf = client(session, "cloudfront", "us-east-1")
    recursos = []
    for acl in listar_acls(waf, "CLOUDFRONT"):
        recursos.append(recurso(
            account_id, "global", NAME, "web_acl", acl["Id"], arn=acl["ARN"], name=acl.get("Name"),
            attrs=resumen_acl(waf, acl, "CLOUDFRONT"),
            relations={"attached_to": _distribuciones(cf, acl["ARN"])},
        ))
    return recursos
