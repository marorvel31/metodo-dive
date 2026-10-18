"""Distribuciones CloudFront (servicio global; la API se llama en us-east-1)."""

from common import recurso
from common.aws import client

NAME = "cloudfront"
TRIGGERS = ["Amazon CloudFront"]
ALWAYS = False
GLOBAL = True
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def _tipo_origen(o):
    if o.get("S3OriginConfig") is not None:
        return "s3"
    if (o.get("DomainName") or "").endswith(".s3.amazonaws.com") or ".s3." in (o.get("DomainName") or ""):
        return "s3"
    return "custom"


def collect(session, region, account_id):
    cf = client(session, "cloudfront", "us-east-1")
    recursos = []
    for pagina in cf.get_paginator("list_distributions").paginate():
        for d in (pagina.get("DistributionList") or {}).get("Items", []):
            # Solo dominio/id/tipo del origen: los CustomHeaders pueden llevar secretos compartidos.
            origenes = [{
                "id": o.get("Id"),
                "domain": o.get("DomainName"),
                "type": _tipo_origen(o),
                "path": o.get("OriginPath") or None,
                "oac": o.get("OriginAccessControlId") or None,
            } for o in (d.get("Origins") or {}).get("Items", [])]
            waf = d.get("WebACLId") or None
            cert = (d.get("ViewerCertificate") or {}).get("ACMCertificateArn")
            aliases = (d.get("Aliases") or {}).get("Items", [])
            recursos.append(recurso(
                account_id, "global", NAME, "distribution", d["Id"], arn=d.get("ARN"),
                name=aliases[0] if aliases else d.get("Comment") or None,
                state=d.get("Status"),
                attrs={
                    "enabled": d.get("Enabled"),
                    "domain_name": d.get("DomainName"),
                    "aliases": aliases,
                    "origins": origenes,
                    "web_acl": waf,
                    "certificate_arn": cert,
                    "price_class": d.get("PriceClass"),
                    "http_version": d.get("HttpVersion"),
                    "comment": d.get("Comment") or None,
                },
                relations={
                    # Los origins se identifican por dominio (ALB dns_name, bucket, API): la fase 4 los cruza.
                    "target_ids": [o["domain"] for o in origenes if o["domain"]],
                    "attached_to": [x for x in (waf, cert) if x],
                },
            ))
    return recursos
