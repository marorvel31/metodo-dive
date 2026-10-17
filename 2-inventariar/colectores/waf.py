"""Web ACLs de WAFv2 con scope REGIONAL (ALB, API Gateway, Cognito, AppSync...).

Las de scope CLOUDFRONT las inventaría `waf_cloudfront` una sola vez por cuenta.
"""

from common import recurso
from common.aws import client

NAME = "waf"
TRIGGERS = ["AWS WAF"]
ALWAYS = False
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag

# Tipos de recurso a los que se asocia una ACL regional; una llamada por tipo.
TIPOS_REGIONALES = ["APPLICATION_LOAD_BALANCER", "API_GATEWAY", "COGNITO_USER_POOL", "APPSYNC"]


def listar_acls(waf, scope):
    """list_web_acls no tiene paginador; se sigue NextMarker a mano."""
    acls, marker = [], None
    while True:
        resp = waf.list_web_acls(Scope=scope, Limit=100, **({"NextMarker": marker} if marker else {}))
        acls += resp.get("WebACLs", [])
        marker = resp.get("NextMarker")
        if not marker or not resp.get("WebACLs"):
            return acls


def resumen_acl(waf, acl, scope):
    """Nombre de reglas y acción por defecto; no se copia el contenido de las reglas."""
    d = waf.get_web_acl(Name=acl["Name"], Scope=scope, Id=acl["Id"])["WebACL"]
    reglas = []
    for r in d.get("Rules", []):
        st = r.get("Statement", {})
        grupo = (st.get("ManagedRuleGroupStatement") or {})
        reglas.append({
            "name": r.get("Name"),
            "priority": r.get("Priority"),
            "managed": f"{grupo['VendorName']}/{grupo['Name']}" if grupo else None,
            "action": next(iter(r.get("Action") or r.get("OverrideAction") or {}), None),
        })
    return {
        "scope": scope,
        "description": d.get("Description") or None,
        "default_action": next(iter(d.get("DefaultAction") or {}), None),
        "rules": reglas,
        "capacity": d.get("Capacity"),
    }


def collect(session, region, account_id):
    waf = client(session, "wafv2", region)
    recursos = []
    for acl in listar_acls(waf, "REGIONAL"):
        asociados = []
        for tipo in TIPOS_REGIONALES:
            asociados += waf.list_resources_for_web_acl(WebACLArn=acl["ARN"], ResourceType=tipo).get("ResourceArns", [])
        recursos.append(recurso(
            account_id, region, NAME, "web_acl", acl["Id"], arn=acl["ARN"], name=acl.get("Name"),
            attrs=resumen_acl(waf, acl, "REGIONAL"),
            relations={"attached_to": asociados},
        ))
    return recursos
