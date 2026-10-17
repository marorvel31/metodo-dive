"""Certificados de ACM: dominio, estado, expiración y dónde se usan.

Los certificados públicos de ACM no facturan, así que además de ACM se activa con los
servicios que los consumen (ELB, CloudFront, API Gateway). Solo metadatos: nunca get_certificate.
"""

from common import recurso
from common.aws import client

NAME = "acm"
TRIGGERS = ["AWS Certificate Manager", "Amazon Elastic Load Balancing", "Amazon CloudFront", "Amazon API Gateway"]
ALWAYS = False
GLOBAL = False

# Sin este filtro list_certificates solo devuelve RSA_1024/RSA_2048.
TIPOS_LLAVE = ["RSA_1024", "RSA_2048", "RSA_3072", "RSA_4096", "EC_prime256v1", "EC_secp384r1", "EC_secp521r1"]


def collect(session, region, account_id):
    acm = client(session, "acm", region)
    recursos = []
    for pagina in acm.get_paginator("list_certificates").paginate(Includes={"keyTypes": TIPOS_LLAVE}):
        for resumen in pagina.get("CertificateSummaryList", []):
            # describe_certificate da InUseBy (relaciones); no trae llave ni cuerpo del certificado.
            c = acm.describe_certificate(CertificateArn=resumen["CertificateArn"])["Certificate"]
            en_uso = c.get("InUseBy", [])
            recursos.append(recurso(
                account_id, region, NAME, "certificate", c["CertificateArn"], arn=c["CertificateArn"],
                name=c.get("DomainName"), state=c.get("Status"),
                attrs={
                    "domain": c.get("DomainName"),
                    "sans": c.get("SubjectAlternativeNames", []),
                    "type": c.get("Type"),
                    "key_algorithm": c.get("KeyAlgorithm"),
                    "not_after": c.get("NotAfter"),
                    "in_use": bool(en_uso),
                    "renewal_eligibility": c.get("RenewalEligibility"),
                    "validation_method": next((d.get("ValidationMethod") for d in c.get("DomainValidationOptions", [])), None),
                },
                relations={"attached_to": en_uso},
            ))
    return recursos
