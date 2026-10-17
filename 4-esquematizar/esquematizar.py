"""Fase 4 · Esquematizar: inventory.json (+ findings.json, discovery.json) → diagramas draw.io.

Genera en salida/<fecha>/diagramas/:
  - 00-organizacion.drawio: cuentas como contenedores (recursos por servicio, costo) y
    conexiones entre cuentas (Transit Gateway, VPC peering, VPN).
  - <account_id>-<nombre>.drawio: una página por región con VPC → subredes (pública/privada,
    por AZ) → recursos, franja de conectividad, franja de servicios sin VPC y flechas LB→targets.

El layout es una grilla calculada: no hay posiciones aleatorias ni dependencias externas, así
que el mismo inventario produce exactamente el mismo XML.

Uso:
  python 4-esquematizar/esquematizar.py                        # usa salida/<hoy>/inventory.json
  python 4-esquematizar/esquematizar.py --inventory salida/2026-09-26/inventory.json
  python 4-esquematizar/esquematizar.py --inventory x.json --salida /tmp/out --umbral 10

Supuestos sobre el formato de red (los colectores se escriben en paralelo; todo se lee con
.get() y lo que no se reconoce se dibuja como icono genérico, nunca rompe la corrida):
  - VPC: type "vpc". Subred: type "subnet", relations.vpc_id, attrs.az|availability_zone.
  - Subred pública si (en orden): attrs.public|is_public es bool; o su route table (type
    "route_table", relations.subnet_ids, o attrs.main=True para las no asociadas) tiene una ruta
    cuyo valor empieza por "igw-"; o attrs.map_public_ip_on_launch; o el nombre contiene "pub".
  - Transit Gateway: type "transit_gateway"|"tgw". Attachment: type que contiene "attachment",
    attrs.transit_gateway_id (o un "tgw-" en relations.attached_to), attrs.transit_gateway_owner_id,
    attrs.resource_type (vpc|vpn|peering...), attrs.resource_owner_id, relations.vpc_id.
  - Peering: type que contiene "peering" (sin "attachment"); VPCs en attrs.requester_vpc_id /
    accepter_vpc_id (o relations.vpc_id), cuentas en attrs.requester_owner_id / accepter_owner_id
    (o owner_id / peer_owner_id).
  - VPN: type "vpn_connection" con attrs.customer_gateway_id, vpn_gateway_id, transit_gateway_id;
    type "customer_gateway"; type "vpn_gateway" con relations.vpc_id o attached_to.
  - Load balancers: service elb/elbv2, type load_balancer|alb|nlb|clb|gwlb; subtipo en
    attrs.type|lb_type|load_balancer_type; relations.target_ids. Si hay recursos type
    "target_group", sus relations.target_ids se suman al LB que aparezca en su attached_to o en
    attrs.load_balancer_arns.
  - Un recurso va en la subred de relations.subnet_ids si es una sola; si son varias, a la franja
    "multi-subred" de su nivel (privada si alguna es privada); si solo trae vpc_id, a la franja
    "nivel VPC"; si no trae nada de red, a la franja "sin VPC".
"""

import argparse
import html
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

from common import leer_json  # noqa: E402

UMBRAL_COLAPSO = 8          # más de N recursos del mismo tipo en un contenedor → un icono "N× tipo"
SEVERIDAD_ALTA = {"alta", "high", "critical", "critica", "crítica"}

# Tipos que no se dibujan como icono (son contenedores o ruido para un diagrama de arquitectura).
# Se cuentan en una nota al pie de cada página para no esconder que existen.
NO_DIBUJAR_TIPOS = {
    "security_group", "route_table", "network_acl", "nacl", "volume", "snapshot",
    "network_interface", "eni", "target_group", "listener", "listener_rule", "dhcp_options",
    "key_pair", "launch_template", "image", "ami", "db_subnet_group", "subnet_group",
    "parameter_group", "option_group", "log_group", "alarm", "prefix_list",
    "transit_gateway_route_table", "route_table_association",
}
NO_DIBUJAR_SERVICIOS = {"iam", "cloudwatch", "logs", "cloudtrail", "config", "kms", "acm"}

# ---------------------------------------------------------------------------------------------
# Estilos (shapes mxgraph.aws4 de la librería AWS 2019+ de draw.io)
# ---------------------------------------------------------------------------------------------

C_COMPUTO, C_RED, C_DATOS, C_ALMAC = "#ED7100", "#8C4FFF", "#C925D1", "#7AA116"
C_INTEGR, C_SEGUR, C_GESTION = "#E7157B", "#DD344C", "#E7157B"

_ICONO = ("sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;dashed=0;"
          "verticalLabelPosition=bottom;verticalAlign=top;align=center;html=1;fontSize=10;"
          "fontStyle=0;aspect=fixed;pointerEvents=1;")


def _recurso(shape, color):
    return _ICONO + f"strokeColor=none;fillColor={color};shape=mxgraph.aws4.{shape};"


def _servicio(res_icon, color):
    return _ICONO + f"strokeColor=#ffffff;fillColor={color};shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{res_icon};"


GENERICO = ("rounded=1;arcSize=12;whiteSpace=wrap;html=1;fillColor=#F4F6F6;strokeColor=#879196;"
            "fontColor=#232F3E;fontSize=10;align=center;verticalAlign=middle;")
STUB = ("rounded=1;arcSize=12;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#879196;dashed=1;"
        "fontColor=#5A6C86;fontSize=10;align=center;verticalAlign=middle;")
HALO = "rounded=1;arcSize=10;fillColor=none;strokeColor=#D0021B;strokeWidth=3;html=1;"

ICONOS = {  # (service, type) o (service, "*")
    ("ec2", "instance"): _recurso("instance2", C_COMPUTO),
    ("ec2", "elastic_ip"): _recurso("elastic_ip_address", C_RED),
    ("ec2", "eip"): _recurso("elastic_ip_address", C_RED),
    ("autoscaling", "*"): _servicio("auto_scaling2", C_COMPUTO),
    ("lambda", "*"): _recurso("lambda_function", C_COMPUTO),
    ("ecs", "*"): _servicio("ecs", C_COMPUTO),
    ("eks", "*"): _servicio("eks", C_COMPUTO),
    ("ecr", "*"): _servicio("ecr", C_COMPUTO),
    ("elasticbeanstalk", "*"): _servicio("elastic_beanstalk", C_COMPUTO),
    ("rds", "*"): _servicio("rds", C_DATOS),
    ("dynamodb", "*"): _servicio("dynamodb", C_DATOS),
    ("elasticache", "*"): _recurso("elasticache_for_redis", C_DATOS),
    ("redshift", "*"): _servicio("redshift", C_RED),
    ("opensearch", "*"): _servicio("elasticsearch_service", C_RED),
    ("es", "*"): _servicio("elasticsearch_service", C_RED),
    ("docdb", "*"): _servicio("documentdb_with_mongodb_compatibility", C_DATOS),
    ("s3", "*"): _recurso("bucket", C_ALMAC),
    ("efs", "*"): _servicio("elastic_file_system", C_ALMAC),
    ("backup", "*"): _servicio("backup", C_ALMAC),
    ("transfer", "*"): _servicio("transfer_family", C_ALMAC),
    ("cloudfront", "*"): _servicio("cloudfront", C_RED),
    ("route53", "*"): _servicio("route_53", C_RED),
    ("apigateway", "*"): _servicio("api_gateway", C_INTEGR),
    ("apigatewayv2", "*"): _servicio("api_gateway", C_INTEGR),
    ("sqs", "*"): _servicio("sqs", C_INTEGR),
    ("sns", "*"): _servicio("sns", C_INTEGR),
    ("stepfunctions", "*"): _servicio("step_functions", C_INTEGR),
    ("events", "*"): _servicio("eventbridge", C_INTEGR),
    ("eventbridge", "*"): _servicio("eventbridge", C_INTEGR),
    ("kinesis", "*"): _servicio("kinesis_data_streams", C_RED),
    ("cognito", "*"): _servicio("cognito", C_SEGUR),
    ("wafv2", "*"): _servicio("waf", C_SEGUR),
    ("waf", "*"): _servicio("waf", C_SEGUR),
    ("secretsmanager", "*"): _servicio("secrets_manager", C_SEGUR),
    ("guardduty", "*"): _servicio("guardduty", C_SEGUR),
    ("codebuild", "*"): _servicio("codebuild", "#C925D1"),
}
ICONOS_TIPO = {  # por type, sin importar el service (red)
    "internet_gateway": _recurso("internet_gateway", C_RED),
    "nat_gateway": _recurso("nat_gateway", C_RED),
    "vpc_endpoint": _recurso("endpoints", C_RED),
    "transit_gateway": _servicio("transit_gateway", C_RED),
    "tgw": _servicio("transit_gateway", C_RED),
    "attachment": _recurso("transit_gateway_attachment", C_RED),
    "peering": _recurso("peering", C_RED),
    "vpn_connection": _recurso("vpn_connection", C_RED),
    "customer_gateway": _recurso("customer_gateway", C_RED),
    "vpn_gateway": _recurso("vpn_gateway", C_RED),
    "mount_target": _servicio("elastic_file_system", C_ALMAC),
}
LB_SHAPES = {"application": "application_load_balancer", "network": "network_load_balancer",
             "classic": "classic_load_balancer", "gateway": "gateway_load_balancer"}

_CONT = ("points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],[1,0.75],[1,1],[0.75,1],"
         "[0.5,1],[0.25,1],[0,1],[0,0.75],[0,0.5],[0,0.25]];outlineConnect=0;gradientColor=none;"
         "html=1;whiteSpace=wrap;fontSize=11;fontStyle=0;container=1;pointerEvents=0;collapsible=0;"
         "recursiveResize=0;verticalAlign=top;align=left;spacingLeft=30;dashed=0;")
CONTENEDORES = {
    "cuenta": _CONT + "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_account;strokeColor=#CD2264;fillColor=none;fontColor=#CD2264;fontSize=12;",
    "region": _CONT + "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_region;strokeColor=#00A4A6;fillColor=none;fontColor=#147EBA;dashed=1;fontSize=12;",
    "vpc": _CONT + "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_vpc2;strokeColor=#8C4FFF;fillColor=none;fontColor=#8C4FFF;",
    "publica": _CONT + "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_security_group;grStroke=0;strokeColor=#7AA116;fillColor=#F2F6E8;fontColor=#248814;",
    "privada": _CONT + "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_security_group;grStroke=0;strokeColor=#147EBA;fillColor=#E6F2F8;fontColor=#147EBA;",
    "sin_clasificar": _CONT + "shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_security_group;grStroke=0;strokeColor=#879196;fillColor=#F4F6F6;fontColor=#5A6C86;",
    "franja": ("rounded=1;arcSize=3;dashed=1;fillColor=none;strokeColor=#879196;fontColor=#5A6C86;"
               "verticalAlign=top;align=left;spacingLeft=10;spacingTop=4;fontSize=11;container=1;"
               "collapsible=0;recursiveResize=0;html=1;whiteSpace=wrap;"),
    "invisible": ("fillColor=none;strokeColor=none;container=1;collapsible=0;recursiveResize=0;html=1;"
                  "verticalAlign=top;align=left;spacingLeft=4;fontSize=11;fontStyle=1;fontColor=#5A6C86;"),
}
TEXTO = "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=top;whiteSpace=wrap;fontColor=#232F3E;"
NOTA = ("shape=note;size=12;whiteSpace=wrap;html=1;fillColor=#F4F6F6;strokeColor=#879196;"
        "fontColor=#232F3E;fontSize=10;align=left;verticalAlign=top;spacingLeft=6;spacingTop=4;")
FLECHAS = {
    "lb": "endArrow=block;endFill=1;html=1;strokeColor=#8C4FFF;strokeWidth=1.5;edgeStyle=orthogonalEdgeStyle;rounded=1;",
    "tgw": "endArrow=none;html=1;strokeColor=#8C4FFF;strokeWidth=2;edgeStyle=orthogonalEdgeStyle;rounded=1;fontSize=9;fontColor=#8C4FFF;",
    "peering": "endArrow=none;html=1;strokeColor=#147EBA;strokeWidth=2;dashed=1;edgeStyle=orthogonalEdgeStyle;rounded=1;fontSize=9;fontColor=#147EBA;",
    "vpn": "endArrow=none;html=1;strokeColor=#DD344C;strokeWidth=2;dashed=1;dashPattern=8 4;edgeStyle=orthogonalEdgeStyle;rounded=1;fontSize=9;fontColor=#DD344C;",
}

SLOT_W, SLOT_H, ICON = 140, 96, 48

# ---------------------------------------------------------------------------------------------
# Utilidades de lectura defensiva
# ---------------------------------------------------------------------------------------------


def lista(v):
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple, set)):
        return [x for x in v if x not in (None, "")]
    return [v]


def rels(r, k):
    return lista((r.get("relations") or {}).get(k))


def attr(r, *claves):
    a = r.get("attrs") or {}
    for k in claves:
        if a.get(k) not in (None, ""):
            return a[k]
    return None


def svc(r):
    return str(r.get("service") or "?").lower()


def tipo(r):
    return str(r.get("type") or "?").lower()


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def h(s):
    """Escapa texto que va dentro de una etiqueta HTML de draw.io."""
    return html.escape("" if s is None else str(s), quote=False)


def corto(s, n=24):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def slug(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "")).strip("-")
    return s[:60] or "cuenta"


def usd(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def es_vpc(r):
    return tipo(r) == "vpc"


def es_subred(r):
    return tipo(r) == "subnet"


def es_attachment(r):
    return "attachment" in tipo(r) and (
        "transit" in svc(r) or "tgw" in svc(r) or "transit" in tipo(r) or tgw_de(r) is not None)


def es_tgw(r):
    t = tipo(r)
    return t in ("transit_gateway", "tgw") or (svc(r) in ("transitgateway", "tgw") and t == "gateway")


def es_peering(r):
    return "peering" in tipo(r) and "attachment" not in tipo(r)


def es_vpn_conn(r):
    return tipo(r) == "vpn_connection" or (svc(r) == "vpn" and tipo(r) == "connection")


def es_cgw(r):
    return "customer_gateway" in tipo(r) or tipo(r) == "cgw"


def es_vgw(r):
    return "vpn_gateway" in tipo(r) or tipo(r) in ("vgw", "virtual_private_gateway")


def es_lb(r):
    t = tipo(r)
    if "target_group" in t or "listener" in t:
        return False
    return t in ("load_balancer", "alb", "nlb", "clb", "gwlb") or (svc(r) in ("elb", "elbv2") and "balancer" in t)


def tipo_lb(r):
    v = str(attr(r, "type", "lb_type", "load_balancer_type") or tipo(r)).lower()
    for clave, pal in (("application", ("application", "alb")), ("network", ("network", "nlb")),
                       ("gateway", ("gateway", "gwlb")), ("classic", ("classic", "clb"))):
        if any(p in v for p in pal):
            return clave
    return "application" if svc(r) == "elbv2" else "classic"


def tgw_de(r):
    t = attr(r, "transit_gateway_id", "tgw_id")
    if t:
        return t
    for x in rels(r, "attached_to"):
        if isinstance(x, str) and x.startswith("tgw-") and not x.startswith("tgw-attach"):
            return x
    return None


def no_dibujar(r):
    return tipo(r) in NO_DIBUJAR_TIPOS or svc(r) in NO_DIBUJAR_SERVICIOS


def estilo_de(r):
    s, t = svc(r), tipo(r)
    if es_lb(r):
        return _recurso(LB_SHAPES[tipo_lb(r)], C_RED), False
    if es_attachment(r):
        return ICONOS_TIPO["attachment"], False
    if es_peering(r):
        return ICONOS_TIPO["peering"], False
    if s == "rds":
        motor = str(attr(r, "engine") or "").lower()
        if "aurora" in motor or t == "cluster":
            return _recurso("aurora_instance", C_DATOS), False
        if "postgres" in motor:
            return _recurso("rds_postgresql_instance", C_DATOS), False
        if "mysql" in motor or "maria" in motor:
            return _recurso("rds_mysql_instance", C_DATOS), False
    if t in ICONOS_TIPO:
        return ICONOS_TIPO[t], False
    if (s, t) in ICONOS:
        return ICONOS[(s, t)], False
    if (s, "*") in ICONOS:
        return ICONOS[(s, "*")], False
    return GENERICO, True


def detalle(r):
    """Segunda línea del label: el atributo que más ayuda a leer el diagrama."""
    if es_attachment(r):
        return f"→ TGW {tgw_de(r) or '?'}"
    if es_vgw(r):
        return "VPN gateway"
    if es_lb(r):
        esquema = attr(r, "scheme")
        return f"{tipo_lb(r)}{' · ' + esquema if esquema else ''}"
    v = attr(r, "instance_type", "instance_class", "node_type", "engine", "runtime", "launch_type",
             "cidr", "cidr_block", "service_name", "endpoint_type")
    return str(v) if v is not None else tipo(r)


def apagado(r):
    return str(r.get("state") or "").lower() in ("stopped", "stopping", "terminated", "shutting-down", "deleted")


# ---------------------------------------------------------------------------------------------
# Motor de layout: cajas anidadas con posiciones relativas al padre (como draw.io)
# ---------------------------------------------------------------------------------------------


class Ids:
    def __init__(self, prefijo):
        self.prefijo, self.n = prefijo, 0

    def __call__(self):
        self.n += 1
        return f"{self.prefijo}-{self.n}"


class Icono:
    def __init__(self, cid, label, estilo, generico=False, halo=False, datos=None):
        self.cid, self.label, self.estilo = cid, label, estilo
        self.generico, self.halo, self.datos = generico, halo, datos or {}
        self.w, self.h = SLOT_W, SLOT_H


class Caja:
    def __init__(self, cid, label, estilo, layout="grid", cols=4, pad=(44, 16, 16, 16), gap=16,
                 hijos=None, min_w=180, min_h=90, estirar=False):
        self.cid, self.label, self.estilo, self.layout = cid, label, estilo, layout
        self.cols, self.pad, self.gap = cols, pad, gap
        self.hijos = hijos if hijos is not None else []
        self.min_w, self.min_h, self.estirar = min_w, min_h, estirar
        self.w = self.h = 0
        self.pos = []


def medir(n):
    if isinstance(n, Icono):
        return n.w, n.h
    t, r, b, l = n.pad
    tams = [medir(x) for x in n.hijos]
    n.pos = []
    if not tams:
        w, hh = l + r, t + b
    elif n.layout == "grid":
        cw, ch = max(a for a, _ in tams), max(c for _, c in tams)
        cols = max(1, min(n.cols, len(tams)))
        filas = math.ceil(len(tams) / cols)
        n.pos = [(l + (i % cols) * (cw + n.gap), t + (i // cols) * (ch + n.gap)) for i in range(len(tams))]
        w = l + cols * cw + (cols - 1) * n.gap + r
        hh = t + filas * ch + (filas - 1) * n.gap + b
    elif n.layout == "row":
        x, y, alto_linea, ancho = l, t, 0, 0
        for i, (a, c) in enumerate(tams):
            if i and i % n.cols == 0:
                y += alto_linea + n.gap
                x, alto_linea = l, 0
            n.pos.append((x, y))
            x += a + n.gap
            alto_linea = max(alto_linea, c)
            ancho = max(ancho, x - n.gap)
        w, hh = ancho + r, y + alto_linea + b
    else:  # col
        y = t
        for a, c in tams:
            n.pos.append((l, y))
            y += c + n.gap
        w = l + max(a for a, _ in tams) + r
        hh = y - n.gap + b
    n.w, n.h = max(w, n.min_w), max(hh, n.min_h)
    if n.layout == "col" and n.estirar:
        for x in n.hijos:
            if isinstance(x, Caja):
                x.w = n.w - l - r
    return n.w, n.h


def emitir(n, parent, x, y, out):
    if isinstance(n, Icono):
        if n.generico:
            gx, gy, gw, gh = x + 5, y + 8, SLOT_W - 10, ICON + 16
        else:
            gx, gy, gw, gh = x + (SLOT_W - ICON) // 2, y + 6, ICON, ICON
        if n.halo:
            out.append(vertice(n.cid + "-halo", "", HALO, parent, gx - 5, gy - 5, gw + 10, gh + 10))
        out.append(vertice(n.cid, n.label, n.estilo, parent, gx, gy, gw, gh, n.datos))
        return
    out.append(vertice(n.cid, n.label, n.estilo, parent, x, y, n.w, n.h))
    for hijo, (cx, cy) in zip(n.hijos, n.pos):
        emitir(hijo, n.cid, cx, cy, out)


def vertice(cid, label, estilo, parent, x, y, w, hh, datos=None):
    geo = f'<mxGeometry x="{int(x)}" y="{int(y)}" width="{int(w)}" height="{int(hh)}" as="geometry"/>'
    if datos:
        extra = "".join(f' {k}="{esc(v)}"' for k, v in sorted(datos.items()) if v not in (None, ""))
        return (f'<UserObject label="{esc(label)}" id="{cid}"{extra}>'
                f'<mxCell style="{esc(estilo)}" vertex="1" parent="{parent}">{geo}</mxCell></UserObject>')
    return f'<mxCell id="{cid}" value="{esc(label)}" style="{esc(estilo)}" vertex="1" parent="{parent}">{geo}</mxCell>'


def arista(cid, src, dst, estilo, label=""):
    return (f'<mxCell id="{cid}" value="{esc(label)}" style="{esc(estilo)}" edge="1" parent="1" '
            f'source="{src}" target="{dst}"><mxGeometry relative="1" as="geometry"/></mxCell>')


def modelo(celdas, ancho, alto):
    return (f'<mxGraphModel dx="1400" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
            f'arrows="1" fold="1" page="1" pageScale="1" pageWidth="{int(max(ancho, 1169))}" '
            f'pageHeight="{int(max(alto, 827))}" math="0" shadow="0"><root><mxCell id="0"/>'
            f'<mxCell id="1" parent="0"/>' + "".join(celdas) + "</root></mxGraphModel>")


def archivo(paginas):
    """paginas: [(nombre, xml_modelo)] → documento .drawio con una pestaña por página."""
    partes = ['<?xml version="1.0" encoding="UTF-8"?>\n<mxfile host="metodo-dive" agent="4-esquematizar">']
    for i, (nombre, xml) in enumerate(paginas):
        partes.append(f'<diagram id="pag-{i + 1}" name="{esc(nombre)}">{xml}</diagram>')
    partes.append("</mxfile>\n")
    return "\n".join(partes)


# ---------------------------------------------------------------------------------------------
# Contexto global: índices, hallazgos, nombres de cuenta
# ---------------------------------------------------------------------------------------------


class Contexto:
    def __init__(self, inventory, findings=None, discovery=None, umbral=UMBRAL_COLAPSO):
        self.inv = inventory or {}
        self.recursos = [r for r in self.inv.get("resources") or [] if isinstance(r, dict)]
        self.discovery = discovery or {}
        self.umbral = umbral
        self.cuentas = {}
        for acc, d in (self.discovery.get("accounts") or {}).items():
            self.cuentas[str(acc)] = (d or {}).get("name") or str(acc)
        for acc, d in (self.inv.get("accounts") or {}).items():
            self.cuentas[str(acc)] = (d or {}).get("name") or self.cuentas.get(str(acc)) or str(acc)
        for r in self.recursos:
            self.cuentas.setdefault(str(r.get("account_id")), str(r.get("account_id")))
        self.por_id = {}
        for r in self.recursos:
            for k in (r.get("id"), r.get("arn")):
                if k:
                    self.por_id.setdefault(k, r)
        self.hallazgos_altos = set()
        self.hallazgos = defaultdict(list)
        for f in (findings or {}).get("findings") or []:
            rid = f.get("resource_id")
            if not rid:
                continue
            self.hallazgos[rid].append(f)
            if str(f.get("severity") or "").lower() in SEVERIDAD_ALTA:
                self.hallazgos_altos.add(rid)
        self.errores = [e for e in self.inv.get("errors") or [] if isinstance(e, dict)]

    def nombre(self, acc):
        return self.cuentas.get(str(acc), str(acc))

    def alto(self, r):
        return r.get("id") in self.hallazgos_altos or r.get("arn") in self.hallazgos_altos

    def costo(self, acc):
        d = (self.discovery.get("accounts") or {}).get(str(acc)) or {}
        return d.get("total_usd")

    def dueno_tgw(self, tgw_id, r=None):
        if r is not None:
            o = attr(r, "transit_gateway_owner_id", "tgw_owner_id")
            if o:
                return str(o)
        for x in self.recursos:
            if es_tgw(x) and x.get("id") == tgw_id:
                return str(x.get("account_id"))
        o = None
        for x in self.recursos:  # algún attachment que sí traiga el dueño
            if es_attachment(x) and tgw_de(x) == tgw_id:
                o = o or attr(x, "transit_gateway_owner_id", "tgw_owner_id")
        return str(o) if o else None


# ---------------------------------------------------------------------------------------------
# Iconos con colapso "N× tipo"
# ---------------------------------------------------------------------------------------------


def label_recurso(r, ctx, extra=None):
    nombre = r.get("name") or r.get("id") or "?"
    sub = extra if extra is not None else detalle(r)
    return f"<b>{h(corto(nombre))}</b><br>{h(corto(sub, 26))}"


def crear_iconos(recursos, ctx, ids, mapa, extra_label=None):
    """Agrupa por (service, type); si un grupo pasa el umbral se dibuja como un solo icono.
    `mapa` recibe id/arn → cid para poder trazar flechas después."""
    grupos = defaultdict(list)
    for r in recursos:
        grupos[(svc(r), tipo(r))].append(r)
    salida = []
    for clave in sorted(grupos):
        g = sorted(grupos[clave], key=lambda r: (str(r.get("name") or ""), str(r.get("id") or "")))
        if len(g) > ctx.umbral:
            cid = ids()
            estilo, generico = estilo_de(g[0])
            altos = sum(1 for r in g if ctx.alto(r))
            txt = f"<b>{len(g)}× {h(clave[0])} {h(clave[1])}</b>"
            if altos:
                txt += f"<br>{altos} con hallazgo alto"
            datos = {"dive_tipo": f"{clave[0]}/{clave[1]}", "dive_ids": ", ".join(str(r.get("id")) for r in g[:50])
                     + (" …" if len(g) > 50 else "")}
            salida.append(Icono(cid, txt, estilo + "fontStyle=0;", generico, bool(altos), datos))
            for r in g:
                for k in (r.get("id"), r.get("arn")):
                    if k:
                        mapa[k] = cid
            continue
        for r in g:
            cid = ids()
            estilo, generico = estilo_de(r)
            if apagado(r):
                estilo += "opacity=45;textOpacity=70;"
            lab = label_recurso(r, ctx, extra_label(r) if extra_label else None)
            datos = {"dive_id": r.get("id"), "dive_tipo": f"{svc(r)}/{tipo(r)}", "dive_estado": r.get("state"),
                     "tooltip": "; ".join(f"{f.get('severity')}: {f.get('title')}" for f in
                                          ctx.hallazgos.get(r.get("id"), []) + ctx.hallazgos.get(r.get("arn"), []))}
            salida.append(Icono(cid, lab, estilo, generico, ctx.alto(r), datos))
            for k in (r.get("id"), r.get("arn")):
                if k:
                    mapa[k] = cid
    return salida


def stub(cid, texto):
    return Icono(cid, texto, STUB, generico=True)


# ---------------------------------------------------------------------------------------------
# Página por cuenta × región
# ---------------------------------------------------------------------------------------------


def clasificar_subredes(subredes, recursos):
    """Devuelve {subnet_id: 'publica'|'privada'} según los supuestos del docstring."""
    rt_publica = {}  # subnet_id → bool
    main_publica = {}  # vpc_id → bool
    for r in recursos:
        if tipo(r) != "route_table":
            continue
        a = r.get("attrs") or {}
        con_igw = any(isinstance(v, str) and v.startswith("igw-")
                      for ruta in lista(a.get("routes")) if isinstance(ruta, dict) for v in ruta.values())
        con_igw = con_igw or any(str(x).startswith("igw-") for x in rels(r, "attached_to"))
        for s in rels(r, "subnet_ids"):
            rt_publica[s] = rt_publica.get(s, False) or con_igw
        if a.get("main") is True:
            for v in rels(r, "vpc_id"):
                main_publica[v] = main_publica.get(v, False) or con_igw
    res = {}
    for sid, s in subredes.items():
        a = s.get("attrs") or {}
        v = a.get("public", a.get("is_public"))
        if isinstance(v, bool):
            res[sid] = "publica" if v else "privada"
        elif sid in rt_publica:
            res[sid] = "publica" if rt_publica[sid] else "privada"
        elif s.get("_sintetica"):
            res[sid] = "sin_clasificar"
        elif any(vv in main_publica for vv in rels(s, "vpc_id")):
            res[sid] = "publica" if main_publica[rels(s, "vpc_id")[0]] else "privada"
        elif a.get("map_public_ip_on_launch") is True:
            res[sid] = "publica"
        elif "pub" in str(s.get("name") or "").lower():
            res[sid] = "publica"
        else:
            res[sid] = "privada"
    return res


NIVELES = (("publica", "Subredes públicas"), ("privada", "Subredes privadas"), ("sin_clasificar", "Subredes no inventariadas"))


def pagina_region(ctx, acc, region, recursos, n_pagina):
    ids = Ids(f"p{n_pagina}")
    vpcs = {r["id"]: r for r in recursos if es_vpc(r) and r.get("id")}
    subredes = {r["id"]: r for r in recursos if es_subred(r) and r.get("id")}

    def asegurar_vpc(vid):
        if vid and vid not in vpcs:
            vpcs[vid] = {"id": vid, "name": None, "attrs": {}, "relations": {}, "_sintetica": True}

    def asegurar_subred(sid, vid):
        if sid not in subredes:
            subredes[sid] = {"id": sid, "name": None, "attrs": {}, "relations": {"vpc_id": vid}, "_sintetica": True}

    for s in list(subredes.values()):
        asegurar_vpc((rels(s, "vpc_id") or [None])[0] or "vpc-desconocida")

    conect, sin_vpc, omitidos = [], [], Counter()
    nivel_vpc = defaultdict(list)
    multi = defaultdict(lambda: defaultdict(list))
    en_subred = defaultdict(list)
    locales = set(vpcs)

    for r in recursos:
        if es_vpc(r) or es_subred(r):
            continue
        if no_dibujar(r):
            omitidos[f"{svc(r)}/{tipo(r)}"] += 1
            continue
        vid = (rels(r, "vpc_id") or [None])[0]
        if es_tgw(r) or es_vpn_conn(r) or es_cgw(r) or es_peering(r):
            conect.append(r)
            continue
        if es_attachment(r):
            dueno_recurso = attr(r, "resource_owner_id", "vpc_owner_id")
            ajeno = (dueno_recurso and str(dueno_recurso) != str(acc)) or not vid or (locales and vid not in locales)
            if ajeno:
                conect.append(r)
                continue
        sids = [] if (es_attachment(r) or es_vgw(r)) else rels(r, "subnet_ids")
        if es_vgw(r) and not vid:
            vid = next((x for x in rels(r, "attached_to") if str(x).startswith("vpc-")), None)
        if not vid and sids and sids[0] in subredes:
            vid = (rels(subredes[sids[0]], "vpc_id") or [None])[0]
        if sids and not vid:
            vid = "vpc-desconocida"
        if vid:
            asegurar_vpc(vid)
        for sid in sids:
            asegurar_subred(sid, vid)
        if len(set(sids)) == 1:
            en_subred[sids[0]].append(r)
        elif sids:
            multi[vid][tuple(sorted(set(sids)))].append(r)
        elif vid:
            nivel_vpc[vid].append(r)
        else:
            sin_vpc.append(r)

    nivel = clasificar_subredes(subredes, recursos)
    mapa = {}  # id/arn de recurso → cid

    # --- franja de conectividad (TGW, VPN, CGW, peering, attachments de otras cuentas)
    iconos_conect = crear_iconos(conect, ctx, ids, mapa, extra_label=lambda r: etiqueta_conect(ctx, acc, r))
    stubs = {}  # clave → Icono stub (TGW/VPC/CGW de otra cuenta o no inventariados)

    cids_stub = set()

    def stub_de(clave, texto):
        if clave not in stubs:
            stubs[clave] = stub(ids(), texto)
            cids_stub.add(stubs[clave].cid)
            mapa.setdefault(clave, stubs[clave].cid)
        return mapa[clave]

    # --- VPCs
    cajas_vpc = []
    for vid in sorted(vpcs, key=lambda v: (bool(vpcs[v].get("_sintetica")), str(attr(vpcs[v], "cidr", "cidr_block") or ""), v)):
        v = vpcs[vid]
        cidr = attr(v, "cidr", "cidr_block")
        titulo = f"<b>{h(v.get('name') or vid)}</b> · {h(vid)}" + (f" · {h(cidr)}" if cidr else "")
        if v.get("_sintetica"):
            titulo += " · <i>(VPC no inventariada)</i>"
        if ctx.alto(v):
            titulo += " · <font color='#D0021B'>hallazgo alto</font>"
        hijos = []
        if nivel_vpc.get(vid):
            hijos.append(Caja(ids(), "Nivel VPC (gateways, endpoints, attachments)", CONTENEDORES["franja"], cols=8,
                              pad=(28, 10, 10, 10), gap=10,
                              hijos=crear_iconos(nivel_vpc[vid], ctx, ids, mapa), min_h=60))
        subs_vpc = sorted((sid for sid, s in subredes.items() if (rels(s, "vpc_id") or ["vpc-desconocida"])[0] == vid),
                          key=lambda sid: (str(attr(subredes[sid], "az", "availability_zone") or "~"),
                                           str(attr(subredes[sid], "cidr", "cidr_block") or ""), sid))
        for clave_nivel, titulo_nivel in NIVELES:
            subs = [s for s in subs_vpc if nivel[s] == clave_nivel]
            multis = [(k, lst) for k, lst in sorted(multi[vid].items())
                      if nivel_de_multi(k, nivel) == clave_nivel]
            if not subs and not multis:
                continue
            caja_nivel = Caja(ids(), titulo_nivel, CONTENEDORES["invisible"], layout="col", pad=(22, 0, 0, 0),
                              gap=12, estirar=True, min_h=40)
            if multis:
                todos = [r for _, lst in multis for r in lst]
                azs = sorted({str(attr(subredes[s], "az", "availability_zone") or "?") for k, _ in multis for s in k})
                caja_nivel.hijos.append(Caja(ids(), f"Multi-subred ({', '.join(azs)})", CONTENEDORES["franja"], cols=6,
                                             pad=(28, 10, 10, 10), gap=10, min_h=60,
                                             hijos=crear_iconos(todos, ctx, ids, mapa,
                                                                extra_label=lambda r: f"{detalle(r)} · {len(rels(r, 'subnet_ids'))} subredes")))
            fila = Caja(ids(), "", CONTENEDORES["invisible"], layout="row", cols=4, pad=(0, 0, 0, 0), gap=16, min_h=0, min_w=0)
            for sid in subs:
                s = subredes[sid]
                az = attr(s, "az", "availability_zone") or "AZ ?"
                cidr = attr(s, "cidr", "cidr_block")
                lab = f"<b>{h(corto(s.get('name') or sid, 30))}</b><br>{h(az)}" + (f" · {h(cidr)}" if cidr else "")
                items = crear_iconos(en_subred.get(sid, []), ctx, ids, mapa)
                cols = max(1, min(4, math.ceil(math.sqrt(len(items))))) if items else 1
                fila.hijos.append(Caja(ids(), lab, CONTENEDORES[clave_nivel], cols=cols, pad=(46, 14, 14, 14), gap=12,
                                       hijos=items, min_w=200, min_h=110))
            if fila.hijos:
                caja_nivel.hijos.append(fila)
            hijos.append(caja_nivel)
        cajas_vpc.append(Caja(ids(), titulo, CONTENEDORES["vpc"], layout="col", pad=(44, 20, 20, 20), gap=20,
                              hijos=hijos, estirar=True, min_w=260, min_h=100))
        mapa.setdefault("vpc:" + vid, cajas_vpc[-1].cid)

    # --- recursos sin VPC
    hijos_region = list(cajas_vpc)
    if sin_vpc:
        hijos_region.append(Caja(ids(), "Servicios sin VPC (regionales / globales)", CONTENEDORES["franja"], cols=8,
                                 pad=(30, 12, 12, 12), gap=10, hijos=crear_iconos(sin_vpc, ctx, ids, mapa), min_h=70))
    if not hijos_region:
        hijos_region.append(Caja(ids(), "Sin recursos dibujables en esta región", CONTENEDORES["invisible"],
                                 min_h=40, min_w=320, pad=(10, 10, 10, 10)))
    caja_region = Caja(ids(), f"<b>{h(region)}</b>", CONTENEDORES["region"], layout="col", pad=(44, 20, 20, 20),
                       gap=24, hijos=hijos_region, estirar=True, min_w=400)

    # --- aristas (necesitan el mapa completo, por eso van al final; los stubs se crean aquí)
    aristas = []
    vistas = set()

    def unir(src, dst, estilo, label=""):
        if not src or not dst or src == dst or (src, dst, estilo) in vistas:
            return
        vistas.add((src, dst, estilo))
        if dst in cids_stub:  # los stubs van en columna a la derecha: entrar por la izquierda evita cruzarlos
            estilo += "entryX=0;entryY=0.5;entryDx=0;entryDy=0;"
        aristas.append((src, dst, estilo, label))

    tgs_por_lb = defaultdict(list)
    for r in recursos:
        if "target_group" in tipo(r):
            for lb in rels(r, "attached_to") + lista(attr(r, "load_balancer_arns")):
                tgs_por_lb[lb].extend(rels(r, "target_ids"))
    for r in recursos:
        if es_lb(r) and r.get("id") in mapa:
            destinos = rels(r, "target_ids") + tgs_por_lb.get(r.get("id"), []) + tgs_por_lb.get(r.get("arn"), [])
            for t in sorted(set(map(str, destinos))):
                unir(mapa[r["id"]], mapa.get(t), FLECHAS["lb"])
        if es_attachment(r) and r.get("id") in mapa:
            tg = tgw_de(r)
            if tg:
                dueno = ctx.dueno_tgw(tg, r)
                destino = mapa.get(tg) or stub_de(tg, f"<b>TGW {h(tg)}</b><br>{h(etiqueta_cuenta(ctx, dueno, acc))}")
                unir(mapa[r["id"]], destino, FLECHAS["tgw"])
        if es_vpn_conn(r) and r.get("id") in mapa:
            vg = attr(r, "vpn_gateway_id", "vgw_id")
            tg = tgw_de(r)
            cg = attr(r, "customer_gateway_id", "cgw_id")
            if tg:
                unir(mapa[r["id"]], mapa.get(tg) or stub_de(tg, f"<b>TGW {h(tg)}</b>"), FLECHAS["vpn"])
            if vg:
                unir(mapa[r["id"]], mapa.get(vg) or stub_de(vg, f"<b>VGW {h(vg)}</b>"), FLECHAS["vpn"])
            if cg:
                unir(mapa[r["id"]], mapa.get(cg) or stub_de(cg, f"<b>Customer gateway</b><br>{h(cg)}"), FLECHAS["vpn"])
        if es_peering(r) and r.get("id") in mapa:
            for vid, dueno, reg_vpc in vpcs_de_peering(r):
                destino = mapa.get("vpc:" + vid) if vid in vpcs and not vpcs[vid].get("_sintetica") else None
                if not destino:
                    destino = stub_de("vpc:" + vid, f"<b>VPC {h(vid)}</b><br>{h(etiqueta_cuenta(ctx, dueno, acc, reg_vpc))}")
                unir(mapa[r["id"]], destino, FLECHAS["peering"])

    # --- armado de la página
    celdas = []
    y = 20
    total = sum(1 for _ in recursos)
    celdas.append(vertice(ids(), f"<b style='font-size:16px'>{h(ctx.nombre(acc))}</b> · {h(acc)} · {h(region)}"
                          f"<br><span style='color:#5A6C86'>{total} recursos inventariados · generado desde inventory.json "
                          f"{h(ctx.inv.get('generated_at') or '')}</span>", TEXTO, "1", 20, y, 900, 44))
    y += 60
    if iconos_conect or stubs:
        franja = Caja(ids(), "Conectividad (Transit Gateway · VPN · Peering)", CONTENEDORES["franja"], layout="row",
                      cols=4, pad=(30, 12, 12, 12), gap=16, min_h=70)
        grupos = (("Transit Gateway", lambda r: es_tgw(r) or es_attachment(r)), ("VPN", lambda r: es_vpn_conn(r) or es_cgw(r)),
                  ("Peering", es_peering))
        for titulo_g, pertenece in grupos:
            ids_g = {mapa.get(r.get("id")) for r in conect if pertenece(r)}
            items = [i for i in iconos_conect if i.cid in ids_g]
            if items:
                franja.hijos.append(Caja(ids(), titulo_g, CONTENEDORES["invisible"], cols=4, pad=(22, 4, 4, 4), gap=10,
                                         hijos=items, min_w=0, min_h=0))
        if stubs:
            franja.hijos.append(Caja(ids(), "Fuera de esta cuenta / región", CONTENEDORES["invisible"], cols=1,
                                     pad=(22, 4, 4, 4), gap=10, hijos=[stubs[k] for k in sorted(stubs)], min_w=0, min_h=0))
        medir(franja)
        emitir(franja, "1", 20, y, celdas)
        y += franja.h + 30
    medir(caja_region)
    emitir(caja_region, "1", 20, y, celdas)
    ancho = max(caja_region.w + 40, 960)
    y += caja_region.h + 30

    notas = []
    if omitidos:
        notas.append("<b>No dibujados (inventariados):</b> " + ", ".join(f"{n}× {h(k)}" for k, n in sorted(omitidos.items())))
    errs = [e for e in ctx.errores if str(e.get("account_id")) == str(acc) and e.get("region") == region]
    if errs:
        notas.append("<b>Errores de escaneo:</b><br>" + "<br>".join(
            f"{h(e.get('collector'))}: {h(corto(e.get('error'), 90))}" for e in errs[:12]))
    notas.append("<b>Leyenda:</b> borde rojo = hallazgo de severidad alta · atenuado = detenido · "
                 f"'N×' = más de {ctx.umbral} recursos del mismo tipo · punteado = fuera de esta cuenta/región o no inventariado")
    alto_nota = 30 + 16 * sum(n.count("<br>") + 1 + len(n) // 140 for n in notas)
    celdas.append(vertice(ids(), "<br><br>".join(notas), NOTA, "1", 20, y, min(ancho - 40, 1100), alto_nota))
    y += alto_nota + 20
    for src, dst, estilo, label in aristas:
        celdas.append(arista(ids(), src, dst, estilo, label))
    return modelo(celdas, ancho + 40, y + 40)


def nivel_de_multi(sids, nivel):
    niveles = {nivel.get(s) for s in sids}
    if "privada" in niveles:
        return "privada"
    if "publica" in niveles:
        return "publica"
    return "sin_clasificar"


def vpcs_de_peering(r):
    """[(vpc_id, cuenta_dueña, región)] de los dos lados de un peering (región puede ser None)."""
    a = r.get("attrs") or {}
    # El colector vpc guarda cada lado anidado: attrs.requester / attrs.accepter = {account_id, vpc_id, region}
    for lado in ("requester", "accepter"):
        if isinstance(a.get(lado), dict):
            d = a[lado]
            a = {**a, f"{lado}_vpc_id": d.get("vpc_id"), f"{lado}_owner_id": d.get("account_id"),
                 f"{lado}_region": d.get("region")}
    pares = [(a.get("requester_vpc_id"), a.get("requester_owner_id") or a.get("owner_id"), a.get("requester_region")),
             (a.get("accepter_vpc_id"), a.get("accepter_owner_id") or a.get("peer_owner_id"),
              a.get("accepter_region") or a.get("peer_region"))]
    if a.get("peer_vpc_id"):
        pares.append((a.get("peer_vpc_id"), a.get("peer_owner_id"), a.get("peer_region")))
    for v in rels(r, "vpc_id"):
        pares.append((v, r.get("account_id"), r.get("region")))
    vistos, salida = set(), []
    for vid, dueno, region in pares:
        if vid and vid not in vistos:
            vistos.add(vid)
            salida.append((vid, dueno, region))
    return salida


def etiqueta_cuenta(ctx, dueno, acc_actual, region=None):
    if not dueno:
        return "cuenta desconocida"
    base = "esta cuenta" if str(dueno) == str(acc_actual) else f"cuenta {ctx.nombre(dueno)}"
    return base + (f" · {region}" if region else "")


def etiqueta_conect(ctx, acc, r):
    if es_attachment(r):
        rt = attr(r, "resource_type") or "vpc"
        return f"{rt} {attr(r, 'resource_id') or (rels(r, 'vpc_id') or ['?'])[0]} · {etiqueta_cuenta(ctx, attr(r, 'resource_owner_id'), acc)}"
    if es_tgw(r):
        return f"Transit Gateway · {attr(r, 'amazon_side_asn') or r.get('state') or ''}".strip(" ·")
    if es_vpn_conn(r):
        return f"VPN · {attr(r, 'type') or r.get('state') or ''}".strip(" ·")
    if es_cgw(r):
        return f"CGW · BGP {attr(r, 'bgp_asn') or '?'}"
    if es_peering(r):
        return f"peering · {r.get('state') or ''}".strip(" ·")
    return detalle(r)


def diagrama_cuenta(ctx, acc):
    recursos = [r for r in ctx.recursos if str(r.get("account_id")) == str(acc)]
    regiones = sorted({str(r.get("region") or "global") for r in recursos}
                      | {str(e.get("region")) for e in ctx.errores if str(e.get("account_id")) == str(acc) and e.get("region")})
    if not regiones:
        ids = Ids("p1")
        disc = ((ctx.discovery.get("accounts") or {}).get(str(acc)) or {}).get("regions") or {}
        texto = (f"<b style='font-size:16px'>{h(ctx.nombre(acc))}</b> · {h(acc)}<br><br>"
                 "Esta cuenta no tiene recursos en inventory.json.")
        if disc:
            texto += "<br>Regiones con costo en discovery.json: " + ", ".join(
                f"{h(k)} ({usd((v or {}).get('total_usd'))})" for k, v in sorted(disc.items()))
        return archivo([("sin recursos", modelo([vertice(ids(), texto, NOTA, "1", 20, 20, 600, 120)], 700, 200))])
    paginas = []
    for i, reg in enumerate(regiones, 1):
        rs = [r for r in recursos if str(r.get("region") or "global") == reg]
        paginas.append((reg, pagina_region(ctx, acc, reg, rs, i)))
    return archivo(paginas)


# ---------------------------------------------------------------------------------------------
# Vista de organización
# ---------------------------------------------------------------------------------------------


def conexiones_org(ctx):
    """Devuelve (tgws, enlaces, externos):
    tgws: {tgw_id: dueño}; enlaces: [(tipo, origen, destino, label)] donde origen/destino son
    'acc:<id>', 'tgw:<id>' o 'ext:<clave>'; externos: {clave: texto}."""
    tgws, enlaces, externos, vistos = {}, [], {}, set()
    for r in ctx.recursos:
        if es_tgw(r) and r.get("id"):
            tgws[r["id"]] = str(r.get("account_id"))
    for r in sorted(ctx.recursos, key=lambda r: (str(r.get("account_id")), str(r.get("id")))):
        acc = str(r.get("account_id"))
        if es_attachment(r):
            tg = tgw_de(r)
            if not tg:
                continue
            tgws.setdefault(tg, ctx.dueno_tgw(tg, r))
            rt = str(attr(r, "resource_type") or "vpc").lower()
            if rt in ("vpn", "connect", "direct-connect-gateway"):
                continue
            spoke = str(attr(r, "resource_owner_id") or acc)
            if rt == "peering":
                continue
            clave = ("tgw", spoke, tg)
            if clave not in vistos and spoke != tgws.get(tg):
                vistos.add(clave)
                enlaces.append(("tgw", "acc:" + spoke, "tgw:" + tg, ""))
        elif es_peering(r):
            pares = vpcs_de_peering(r)
            duenos = sorted({str(d) for _, d, _ in pares if d})
            if len(duenos) >= 2:
                clave = ("peering", r.get("id") or tuple(duenos))
                if clave not in vistos:
                    vistos.add(clave)
                    enlaces.append(("peering", "acc:" + duenos[0], "acc:" + duenos[1], f"peering {r.get('id') or ''}".strip()))
        elif es_vpn_conn(r):
            cg = attr(r, "customer_gateway_id", "cgw_id") or f"cgw?{r.get('id')}"
            cgw_r = ctx.por_id.get(cg)
            externos.setdefault("cgw:" + cg, f"<b>Red externa (VPN)</b><br>{h((cgw_r or {}).get('name') or cg)}")
            tg = tgw_de(r)
            origen = "tgw:" + tg if tg else "acc:" + acc
            if tg:
                tgws.setdefault(tg, ctx.dueno_tgw(tg, r) or acc)
            clave = ("vpn", r.get("id"))
            if clave not in vistos:
                vistos.add(clave)
                enlaces.append(("vpn", origen, "ext:cgw:" + cg, f"VPN {r.get('id') or ''}".strip()))
    return tgws, enlaces, externos


def diagrama_organizacion(ctx):
    ids = Ids("org")
    tgws, enlaces, externos = conexiones_org(ctx)
    cuentas = sorted(ctx.cuentas, key=lambda a: (-(ctx.costo(a) or 0), ctx.nombre(a).lower(), a))
    for _, o, d, _ in enlaces:  # cuentas referenciadas que no están en el inventario
        for x in (o, d):
            if x.startswith("acc:") and x[4:] not in ctx.cuentas:
                externos.setdefault("acc:" + x[4:], f"<b>Cuenta {h(x[4:])}</b><br>(fuera del inventario)")
    for tg, dueno in tgws.items():
        if not dueno or dueno not in ctx.cuentas:
            externos.setdefault("tgw:" + tg, f"<b>TGW {h(tg)}</b><br>{h('cuenta ' + dueno if dueno else 'dueño desconocido')}")

    por_cuenta = defaultdict(Counter)
    regiones = defaultdict(set)
    for r in ctx.recursos:
        por_cuenta[str(r.get("account_id"))][svc(r)] += 1
        regiones[str(r.get("account_id"))].add(str(r.get("region") or "global"))
    sev = defaultdict(Counter)
    for rid, fs in ctx.hallazgos.items():
        for f in fs:
            sev[str(f.get("account_id") or (ctx.por_id.get(rid) or {}).get("account_id"))][str(f.get("severity") or "?").lower()] += 1

    celdas, cid_de = [], {}
    ancho_c, cols = 300, max(1, min(4, math.ceil(math.sqrt(len(cuentas) or 1))))
    cajas = []
    for acc in cuentas:
        cuenta_cid = ids()
        cid_de["acc:" + acc] = cuenta_cid
        servicios = por_cuenta.get(acc, Counter())
        lineas = [f"<b>{sum(servicios.values())}</b> recursos · {len(regiones.get(acc, ()))} regiones"]
        if regiones.get(acc):
            lineas.append(f"<span style='color:#5A6C86'>{h(', '.join(sorted(regiones[acc])))}</span>")
        top = sorted(servicios.items(), key=lambda kv: (-kv[1], kv[0]))
        for s, n in top[:10]:
            lineas.append(f"{h(s)}: {n}")
        if len(top) > 10:
            lineas.append(f"+{len(top) - 10} servicios más")
        if sev.get(acc):
            lineas.append("Hallazgos: " + " · ".join(f"{n} {h(k)}" for k, n in sorted(sev[acc].items())))
        if not servicios:
            lineas.append("<i>Sin recursos inventariados</i>")
        propios = sorted(tg for tg, d in tgws.items() if d == acc)
        alto_texto = 12 + 15 * len(lineas)
        alto = 56 + alto_texto + (SLOT_H + 10 if propios else 0) + 12
        cajas.append((acc, cuenta_cid, lineas, propios, alto_texto, alto))

    x0, y0 = 20, 90
    filas = [cajas[i:i + cols] for i in range(0, len(cajas), cols)]
    y = y0
    for fila in filas:
        alto_fila = max(c[5] for c in fila)
        for j, (acc, cuenta_cid, lineas, propios, alto_texto, _) in enumerate(fila):
            x = x0 + j * (ancho_c + 60)
            costo = ctx.costo(acc)
            titulo = f"<b>{h(ctx.nombre(acc))}</b><br>{h(acc)}" + (f" · {usd(costo)} ({ctx.discovery.get('period', {}).get('months', 6)} meses)" if costo is not None else "")
            celdas.append(vertice(cuenta_cid, titulo, CONTENEDORES["cuenta"] + "spacingTop=2;", "1", x, y, ancho_c, alto_fila))
            celdas.append(vertice(ids(), "<br>".join(lineas), TEXTO + "fontSize=11;", cuenta_cid, 14, 50, ancho_c - 28, alto_texto))
            for k, tg in enumerate(propios):
                tcid = ids()
                cid_de["tgw:" + tg] = tcid
                tr = ctx.por_id.get(tg) or {}
                ico = Icono(tcid, f"<b>{h(corto(tr.get('name') or tg))}</b><br>Transit Gateway", ICONOS_TIPO["transit_gateway"],
                            halo=ctx.alto(tr) if tr else False, datos={"dive_id": tg})
                emitir(ico, cuenta_cid, 14 + k * (SLOT_W + 6), 50 + alto_texto + 6, celdas)
        y += alto_fila + 60

    ancho_grid = x0 + cols * (ancho_c + 60)
    ye = y0
    for clave in sorted(externos):
        ecid = ids()
        cid_de["ext:" + clave] = ecid
        if clave.startswith(("acc:", "tgw:")):
            cid_de.setdefault(clave, ecid)
        celdas.append(vertice(ecid, externos[clave], STUB, "1", ancho_grid + 40, ye, 200, 60))
        ye += 90

    for tipo_e, o, d, label in enlaces:
        src, dst = cid_de.get(o), cid_de.get(d)
        if src and dst and src != dst:
            celdas.append(arista(ids(), src, dst, FLECHAS[tipo_e], label))

    total = len(ctx.recursos)
    costo_total = sum((ctx.costo(a) or 0) for a in cuentas)
    celdas.insert(0, vertice(ids(), f"<b style='font-size:18px'>Organización AWS</b> · {len(cuentas)} cuentas · {total} recursos"
                             + (f" · {usd(costo_total)} en el periodo de discovery" if ctx.discovery else "")
                             + f"<br><span style='color:#5A6C86'>Morado = Transit Gateway · azul punteado = VPC peering · rojo punteado = VPN · "
                             f"punteado gris = fuera del inventario · inventario {h(ctx.inv.get('generated_at') or '')}</span>",
                             TEXTO, "1", 20, 20, 1100, 50))
    return archivo([("Organización", modelo(celdas, max(ancho_grid + 280, 1200), max(y, ye) + 40))])


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def generar(inventory, findings=None, discovery=None, carpeta=None, umbral=UMBRAL_COLAPSO):
    ctx = Contexto(inventory, findings, discovery, umbral)
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    escritos = []
    ruta = carpeta / "00-organizacion.drawio"
    ruta.write_text(diagrama_organizacion(ctx), encoding="utf-8")
    escritos.append(ruta)
    for acc in sorted(ctx.cuentas):
        ruta = carpeta / f"{acc}-{slug(ctx.nombre(acc))}.drawio"
        ruta.write_text(diagrama_cuenta(ctx, acc), encoding="utf-8")
        escritos.append(ruta)
    return escritos


def opcional(ruta):
    return leer_json(ruta) if ruta and Path(ruta).exists() else None


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config")
    p.add_argument("--inventory", help="Ruta a inventory.json (por defecto salida/<hoy>/inventory.json)")
    p.add_argument("--findings", help="Ruta a findings.json (por defecto, junto a inventory.json)")
    p.add_argument("--discovery", help="Ruta a discovery.json (por defecto, junto a inventory.json)")
    p.add_argument("--salida", help="Carpeta donde escribir diagramas/ (por defecto, la de inventory.json)")
    p.add_argument("--umbral", type=int, default=UMBRAL_COLAPSO, help="Colapsar a 'N× tipo' desde este número")
    a = p.parse_args()

    if a.inventory:
        inv_path = Path(a.inventory)
    else:
        from common import cargar_config, carpeta_salida
        inv_path = carpeta_salida(cargar_config(a.config)) / "inventory.json"
    if not inv_path.exists():
        raise SystemExit(f"No existe {inv_path}. Corre primero la fase 2 o pasa --inventory.")
    base = Path(a.salida) if a.salida else inv_path.parent
    escritos = generar(leer_json(inv_path), opcional(a.findings or inv_path.parent / "findings.json"),
                       opcional(a.discovery or inv_path.parent / "discovery.json"), base / "diagramas", a.umbral)
    for r in escritos:
        print(r)


if __name__ == "__main__":
    main()
