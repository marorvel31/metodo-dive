"""Genera un inventario sintético (sin llamar a AWS) en tests/fixtures/.

Tres cuentas con recursos + una cuenta vacía, dos regiones: hub de red con Transit Gateway y
VPN, producción con ALB→EC2, RDS, ECS, Lambda, EFS y un grupo grande de instancias (para probar
el colapso "N×"), desarrollo con peering a producción. IDs y nombres son ficticios.

Uso: .venv/bin/python 4-esquematizar/tests/generar_fixtures.py
"""

import json
from pathlib import Path

DESTINO = Path(__file__).resolve().parent / "fixtures"
HUB, PROD, DEV, VACIA = "111111111111", "222222222222", "333333333333", "444444444444"
GEN = "2026-09-26T15:20:00Z"
recursos = []


def r(acc, region, service, type, id, name=None, state=None, attrs=None, relations=None, tags=None, arn=None):
    tags = dict(tags or {})
    if name:
        tags.setdefault("Name", name)
    recursos.append({
        "account_id": acc, "region": region, "service": service, "type": type, "id": id,
        "arn": arn or f"arn:aws:{service}:{region}:{acc}:{type}/{id}", "name": name, "state": state,
        "tags": tags, "attrs": attrs or {},
        "relations": {k: v for k, v in (relations or {}).items() if v},
    })


def vpc_basica(acc, region, vid, cidr, nombre, azs, sufijo, publicas=True):
    """VPC con subred pública y privada por AZ. Devuelve {('pub'|'priv', az): subnet_id}."""
    r(acc, region, "vpc", "vpc", vid, nombre, "available", {"cidr": cidr, "is_default": False})
    subs = {}
    base = cidr.split(".")
    for i, az in enumerate(azs):
        for j, nivel in enumerate(("pub", "priv") if publicas else ("priv",)):
            sid = f"subnet-{sufijo}{nivel}{az[-1]}"
            subs[(nivel, az)] = sid
            r(acc, region, "vpc", "subnet", sid, f"{nombre}-{nivel}-{az[-1]}", "available",
              {"az": az, "cidr": f"{base[0]}.{base[1]}.{i * 10 + j * 100}.0/24",
               "map_public_ip_on_launch": nivel == "pub"},
              {"vpc_id": vid})
    return subs


# ---------------------------------------------------------------- HUB (red central) us-east-1
E1, W2 = "us-east-1", "us-west-2"
AZ_E1, AZ_W2 = ["us-east-1a", "us-east-1b"], ["us-west-2a", "us-west-2b"]
hs = vpc_basica(HUB, E1, "vpc-0hub", "10.0.0.0/16", "hub", AZ_E1, "h")
# Sin map_public_ip_on_launch: la clasificación pública debe salir de la route table.
for x in recursos:
    if x["type"] == "subnet" and x["account_id"] == HUB:
        x["attrs"].pop("map_public_ip_on_launch")
r(HUB, E1, "vpc", "route_table", "rtb-0hubpub", "hub-public-rt", None,
  {"main": False, "routes": [{"destination": "10.0.0.0/16", "target": "local"},
                             {"destination": "0.0.0.0/0", "target": "igw-0hub"}]},
  {"vpc_id": "vpc-0hub", "subnet_ids": [hs[("pub", a)] for a in AZ_E1]})
r(HUB, E1, "vpc", "route_table", "rtb-0hubmain", "hub-main-rt", None,
  {"main": True, "routes": [{"destination": "0.0.0.0/0", "target": "nat-0hub"},
                            {"destination": "10.0.0.0/8", "target": "tgw-0central"}]},
  {"vpc_id": "vpc-0hub"})
r(HUB, E1, "vpc", "internet_gateway", "igw-0hub", "hub-igw", "available", {}, {"vpc_id": "vpc-0hub"})
r(HUB, E1, "vpc", "nat_gateway", "nat-0hub", "hub-nat-a", "available", {"connectivity_type": "public"},
  {"vpc_id": "vpc-0hub", "subnet_ids": [hs[("pub", "us-east-1a")]]})
r(HUB, E1, "vpc", "security_group", "sg-0hubdef", "default", None, {}, {"vpc_id": "vpc-0hub"})
r(HUB, E1, "transitgateway", "transit_gateway", "tgw-0central", "tgw-central", "available",
  {"amazon_side_asn": 64512, "owner_id": HUB})
for att, vpc, owner in (("tgw-attach-0hub", "vpc-0hub", HUB), ("tgw-attach-0prod", "vpc-0prod", PROD),
                        ("tgw-attach-0dev", "vpc-0dev", DEV)):
    r(HUB, E1, "transitgateway", "transit_gateway_attachment", att, None, "available",
      {"transit_gateway_id": "tgw-0central", "transit_gateway_owner_id": HUB, "resource_type": "vpc",
       "resource_id": vpc, "resource_owner_id": owner},
      {"vpc_id": vpc, "attached_to": ["tgw-0central"]})
r(HUB, E1, "vpn", "customer_gateway", "cgw-0oficina", "oficina-principal", "available",
  {"bgp_asn": 65000, "type": "ipsec.1"})
r(HUB, E1, "vpn", "vpn_connection", "vpn-0oficina", "vpn-oficina", "available",
  {"type": "ipsec.1", "customer_gateway_id": "cgw-0oficina", "transit_gateway_id": "tgw-0central",
   "tunnels_up": 1, "tunnels_total": 2},
  {"attached_to": ["tgw-0central", "cgw-0oficina"]})
r(HUB, E1, "transitgateway", "transit_gateway_attachment", "tgw-attach-0vpn", None, "available",
  {"transit_gateway_id": "tgw-0central", "transit_gateway_owner_id": HUB, "resource_type": "vpn",
   "resource_id": "vpn-0oficina", "resource_owner_id": HUB},
  {"attached_to": ["tgw-0central"]})
r(HUB, E1, "ec2", "instance", "i-0bastion", "bastion", "running",
  {"instance_type": "t3.micro", "az": "us-east-1a", "private_ip": "10.0.0.10", "public_ip": "198.51.100.10"},
  {"vpc_id": "vpc-0hub", "subnet_ids": [hs[("pub", "us-east-1a")]], "security_group_ids": ["sg-0hubdef"]})
r(HUB, E1, "route53", "hosted_zone", "Z0HUBZONA", "ejemplo.internal.", None, {"private": True, "records": 14})
r(HUB, E1, "s3", "bucket", "hub-flow-logs-ejemplo", "hub-flow-logs-ejemplo", None, {"versioning": "Enabled"},
  arn="arn:aws:s3:::hub-flow-logs-ejemplo")
r(HUB, E1, "iam", "role", "RolDeEjemplo", "RolDeEjemplo")

# ---------------------------------------------------------------- PROD us-east-1
ps = vpc_basica(PROD, E1, "vpc-0prod", "10.1.0.0/16", "prod", AZ_E1, "p")
r(PROD, E1, "vpc", "internet_gateway", "igw-0prod", "prod-igw", "available", {}, {"vpc_id": "vpc-0prod"})
for az in AZ_E1:
    r(PROD, E1, "vpc", "nat_gateway", f"nat-0prod{az[-1]}", f"prod-nat-{az[-1]}", "available",
      {"connectivity_type": "public"}, {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("pub", az)]]})
r(PROD, E1, "vpc", "vpc_endpoint", "vpce-0prods3", "s3-gateway", "available",
  {"endpoint_type": "Gateway", "service_name": "com.amazonaws.us-east-1.s3"}, {"vpc_id": "vpc-0prod"})
r(PROD, E1, "vpc", "vpc_endpoint", "vpce-0prodecr", "ecr-api", "available",
  {"endpoint_type": "Interface", "service_name": "com.amazonaws.us-east-1.ecr.api"},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", a)] for a in AZ_E1]})
r(PROD, E1, "vpc", "security_group", "sg-0alb", "alb-sg", None, {}, {"vpc_id": "vpc-0prod"})
r(PROD, E1, "vpc", "security_group", "sg-0app", "app-sg", None, {}, {"vpc_id": "vpc-0prod"})
r(PROD, E1, "transitgateway", "transit_gateway_attachment", "tgw-attach-0prod", "prod-a-central", "available",
  {"transit_gateway_id": "tgw-0central", "transit_gateway_owner_id": HUB, "resource_type": "vpc",
   "resource_id": "vpc-0prod", "resource_owner_id": PROD},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", a)] for a in AZ_E1]})
app = []
for i, az in enumerate(AZ_E1):
    iid = f"i-0app{i + 1}"
    app.append(iid)
    r(PROD, E1, "ec2", "instance", iid, f"api-prod-{i + 1}", "running",
      {"instance_type": "m5.large", "az": az, "private_ip": f"10.1.{100 + i * 10}.10"},
      {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", az)]], "security_group_ids": ["sg-0app"],
       "attached_to": [f"vol-0app{i + 1}"]})
    r(PROD, E1, "ec2", "volume", f"vol-0app{i + 1}", None, "in-use", {"size_gb": 50, "volume_type": "gp3"},
      {"attached_to": [iid]})
r(PROD, E1, "ec2", "instance", "i-0legacy", "reportes-legacy", "stopped",
  {"instance_type": "t2.xlarge", "az": "us-east-1a"},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", "us-east-1a")]]})
r(PROD, E1, "ec2", "volume", "vol-0huerfano", None, "available", {"size_gb": 200, "volume_type": "gp2"})
# Grupo grande en una subred → debe colapsar a "30× ec2 instance"
workers = []
for i in range(30):
    iid = f"i-0wrk{i:02d}"
    workers.append(iid)
    r(PROD, E1, "ec2", "instance", iid, f"worker-{i:02d}", "running",
      {"instance_type": "c6i.large", "az": "us-east-1b"},
      {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", "us-east-1b")]], "tags_asg": None})
alb_arn = f"arn:aws:elasticloadbalancing:{E1}:{PROD}:loadbalancer/app/prod-web/abc123"
r(PROD, E1, "elb", "load_balancer", "prod-web", "prod-web", "active",
  {"type": "application", "scheme": "internet-facing", "dns_name": "prod-web-123.us-east-1.elb.amazonaws.com"},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("pub", a)] for a in AZ_E1], "security_group_ids": ["sg-0alb"],
   "target_ids": app}, arn=alb_arn)
nlb_arn = f"arn:aws:elasticloadbalancing:{E1}:{PROD}:loadbalancer/net/prod-workers/def456"
r(PROD, E1, "elb", "load_balancer", "prod-workers", "prod-workers", "active",
  {"type": "network", "scheme": "internal"},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", a)] for a in AZ_E1]}, arn=nlb_arn)
r(PROD, E1, "elb", "target_group", "tg-workers", "tg-workers", None, {"target_type": "instance", "port": 8080},
  {"vpc_id": "vpc-0prod", "attached_to": [nlb_arn], "target_ids": workers[:5] + ["10.9.9.9"]})
r(PROD, E1, "rds", "db_instance", "prod-db", "prod-db", "available",
  {"engine": "postgres", "engine_version": "15.7", "instance_class": "db.r6g.large", "multi_az": True,
   "publicly_accessible": False, "storage_gb": 200},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", a)] for a in AZ_E1], "security_group_ids": ["sg-0app"]})
r(PROD, E1, "ecs", "service", "pagos-api", "pagos-api", "ACTIVE",
  {"launch_type": "FARGATE", "desired_count": 2, "cluster": "prod-cluster"},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", "us-east-1a")]], "security_group_ids": ["sg-0app"]})
r(PROD, E1, "lambda", "function", "procesar-eventos", "procesar-eventos", "Active",
  {"runtime": "python3.12", "memory_mb": 256},
  {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", a)] for a in AZ_E1], "security_group_ids": ["sg-0app"]})
r(PROD, E1, "lambda", "function", "thumbnail", "thumbnail", "Active", {"runtime": "nodejs20.x", "memory_mb": 512})
r(PROD, E1, "efs", "file_system", "fs-0prod", "prod-compartido", "available", {"performance_mode": "generalPurpose"})
for az in AZ_E1:
    r(PROD, E1, "efs", "mount_target", f"fsmt-0prod{az[-1]}", None, "available", {"file_system_id": "fs-0prod"},
      {"vpc_id": "vpc-0prod", "subnet_ids": [ps[("priv", az)]], "attached_to": ["fs-0prod"]})
r(PROD, E1, "dynamodb", "table", "sesiones", "sesiones", "ACTIVE", {"billing_mode": "PAY_PER_REQUEST"})
r(PROD, E1, "apigateway", "rest_api", "a1b2c3d4", "api-publica", None, {"endpoint_type": "REGIONAL"})
r(PROD, E1, "cloudfront", "distribution", "E0PRODCDN", "cdn-web", "Deployed", {"price_class": "PriceClass_100"},
  {"attached_to": ["prod-web-estaticos"]})
for b in ("prod-web-estaticos", "prod-backups", "prod-logs"):
    r(PROD, E1, "s3", "bucket", b, b, None, {}, arn=f"arn:aws:s3:::{b}")
r(PROD, E1, "vpc", "peering", "pcx-0proddr", "prod-a-dr", "active",
  {"requester_vpc_id": "vpc-0prod", "requester_owner_id": PROD, "requester_region": E1,
   "accepter_vpc_id": "vpc-0dr", "accepter_owner_id": PROD, "accepter_region": W2},
  {"vpc_id": "vpc-0prod"})

# ---------------------------------------------------------------- PROD us-west-2 (DR)
ds = vpc_basica(PROD, W2, "vpc-0dr", "10.2.0.0/16", "dr", AZ_W2, "d", publicas=False)
r(PROD, W2, "rds", "db_instance", "prod-db-replica", "prod-db-replica", "available",
  {"engine": "postgres", "instance_class": "db.r6g.large", "multi_az": False},
  {"vpc_id": "vpc-0dr", "subnet_ids": [ds[("priv", a)] for a in AZ_W2]})
r(PROD, W2, "s3", "bucket", "prod-backups-dr", "prod-backups-dr", None, {"replication": True},
  arn="arn:aws:s3:::prod-backups-dr")
r(PROD, W2, "vpc", "peering", "pcx-0proddr", "prod-a-dr", "active",
  {"requester_vpc_id": "vpc-0prod", "requester_owner_id": PROD, "requester_region": E1,
   "accepter_vpc_id": "vpc-0dr", "accepter_owner_id": PROD, "accepter_region": W2},
  {"vpc_id": "vpc-0dr"})

# ---------------------------------------------------------------- DEV us-east-1
dvs = vpc_basica(DEV, E1, "vpc-0dev", "10.3.0.0/16", "dev", ["us-east-1a"], "v")
r(DEV, E1, "vpc", "internet_gateway", "igw-0dev", "dev-igw", "available", {}, {"attached_to": ["vpc-0dev"], "vpc_id": "vpc-0dev"})
r(DEV, E1, "transitgateway", "transit_gateway_attachment", "tgw-attach-0dev", "dev-a-central", "available",
  {"transit_gateway_id": "tgw-0central", "transit_gateway_owner_id": HUB, "resource_type": "vpc",
   "resource_id": "vpc-0dev", "resource_owner_id": DEV},
  {"vpc_id": "vpc-0dev", "subnet_ids": [dvs[("priv", "us-east-1a")]]})
r(DEV, E1, "ec2", "instance", "i-0devapi", "api-dev", "running",
  {"instance_type": "t3.medium", "az": "us-east-1a", "public_ip": "203.0.113.20"},
  {"vpc_id": "vpc-0dev", "subnet_ids": [dvs[("pub", "us-east-1a")]]})
r(DEV, E1, "rds", "db_instance", "dev-db", "dev-db", "available",
  {"engine": "mysql", "instance_class": "db.t3.small", "publicly_accessible": True},
  {"vpc_id": "vpc-0dev", "subnet_ids": [dvs[("priv", "us-east-1a")]]})
r(DEV, E1, "vpc", "peering", "pcx-0devprod", "dev-a-prod", "active",
  {"requester_vpc_id": "vpc-0dev", "requester_owner_id": DEV, "accepter_vpc_id": "vpc-0prod",
   "accepter_owner_id": PROD, "accepter_region": E1},
  {"vpc_id": "vpc-0dev"})
r(DEV, E1, "sqs", "queue", "dev-eventos", "dev-eventos", None, {"fifo": False})
r(DEV, E1, "appsync", "graphql_api", "gql0dev", "dev-graphql", None, {})  # sin icono → genérico

inventory = {
    "generated_at": GEN,
    "accounts": {HUB: {"name": "Red Central"}, PROD: {"name": "Produccion"}, DEV: {"name": "Desarrollo"},
                 VACIA: {"name": "Sandbox"}},
    "scanned": [],
    "resources": recursos,
    "errors": [
        {"account_id": DEV, "region": W2, "collector": "rds", "error": "AccessDenied: User is not authorized to perform rds:DescribeDBInstances"},
        {"account_id": PROD, "region": E1, "collector": "elasticache", "error": "ThrottlingException: Rate exceeded (reintentos agotados)"},
    ],
}
conteo = {}
for x in recursos:
    k = (x["account_id"], x["region"], x["service"])
    conteo[k] = conteo.get(k, 0) + 1
inventory["scanned"] = [{"account_id": a, "region": g, "collector": s, "count": n, "seconds": round(0.3 + n / 20, 2)}
                        for (a, g, s), n in sorted(conteo.items())]

findings = {
    "generated_at": "2026-09-26T15:40:00Z",
    "findings": [
        {"id": "F001", "category": "riesgo", "severity": "alta", "account_id": DEV, "region": E1, "resource_id": "dev-db",
         "title": "RDS con acceso público", "detail": "publicly_accessible=true en una base de datos.", "monthly_usd": None, "source": "regla"},
        {"id": "F002", "category": "riesgo", "severity": "alta", "account_id": PROD, "region": E1, "resource_id": "i-0wrk07",
         "title": "Instancia con IMDSv1 habilitado", "detail": "HttpTokens=optional.", "monthly_usd": None, "source": "regla"},
        {"id": "F003", "category": "desperdicio", "severity": "media", "account_id": PROD, "region": E1, "resource_id": "vol-0huerfano",
         "title": "Volumen EBS sin adjuntar", "detail": "200 GB gp2 en estado available.", "monthly_usd": 20.0, "source": "aws-doctor"},
        {"id": "F004", "category": "desperdicio", "severity": "media", "account_id": PROD, "region": E1, "resource_id": "i-0legacy",
         "title": "Instancia detenida hace más de 90 días", "detail": "Sigue pagando EBS.", "monthly_usd": 8.0, "source": "regla"},
        {"id": "F005", "category": "riesgo", "severity": "alta", "account_id": HUB, "region": E1, "resource_id": "i-0bastion",
         "title": "Bastión con SSH abierto a 0.0.0.0/0", "detail": "sg-0hubdef permite 22/tcp desde internet.", "monthly_usd": None, "source": "regla"},
        {"id": "F006", "category": "costo", "severity": "baja", "account_id": PROD, "region": E1, "resource_id": "prod-logs",
         "title": "Bucket sin regla de ciclo de vida", "detail": "Logs sin expiración.", "monthly_usd": 3.5, "source": "regla"},
        {"id": "F007", "category": "inventario", "severity": "baja", "account_id": PROD, "region": W2, "resource_id": None,
         "title": "Costo de CloudWatch sin recurso inventariado", "detail": "Cruce billing vs inventario.", "monthly_usd": 1.2, "source": "cruce-billing"},
    ],
}

def region_costos(total, servicios):
    meses = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    return {"total_usd": total, "services": servicios, "monthly_usd": {m: round(total / 6, 2) for m in meses}}


discovery = {
    "generated_at": "2026-09-26T15:00:00Z",
    "period": {"start": "2026-03-01", "end": "2026-09-01", "months": 6},
    "threshold_usd": 0.01,
    "accounts": {
        HUB: {"name": "Red Central", "total_usd": 1450.30, "regions": {
            E1: region_costos(1420.30, {"Amazon Virtual Private Cloud": 820.0, "EC2 - Other": 410.30,
                                        "Amazon Elastic Compute Cloud - Compute": 190.0}),
            "global": region_costos(30.0, {"Amazon Route 53": 30.0})}},
        PROD: {"name": "Produccion", "total_usd": 18230.75, "regions": {
            E1: region_costos(16100.75, {"Amazon Elastic Compute Cloud - Compute": 9100.0,
                                         "Amazon Relational Database Service": 3900.0,
                                         "Amazon Elastic Container Service": 1200.0,
                                         "Amazon Elastic Load Balancing": 700.0, "Amazon Simple Storage Service": 600.75,
                                         "AWS Lambda": 120.0, "Amazon DynamoDB": 480.0}),
            W2: region_costos(2130.0, {"Amazon Relational Database Service": 1900.0, "Amazon Simple Storage Service": 230.0})}},
        DEV: {"name": "Desarrollo", "total_usd": 640.10, "regions": {
            E1: region_costos(640.10, {"Amazon Elastic Compute Cloud - Compute": 300.0,
                                       "Amazon Relational Database Service": 320.10, "Amazon Simple Queue Service": 20.0})}},
        VACIA: {"name": "Sandbox", "total_usd": 0.42, "regions": {
            E1: region_costos(0.42, {"AWS Key Management Service": 0.42})}},
    },
}

DESTINO.mkdir(parents=True, exist_ok=True)
for nombre, datos in (("inventory.json", inventory), ("findings.json", findings), ("discovery.json", discovery)):
    (DESTINO / nombre).write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
    print(DESTINO / nombre)
