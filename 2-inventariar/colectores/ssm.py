"""Parámetros y documentos propios de Systems Manager.

JAMÁS get_parameter / get_parameters: devuelven el valor (y un SecureString descifrado).
describe_parameters solo trae metadatos.
"""

from common import recurso
from common.aws import client

NAME = "ssm"
TRIGGERS = ["AWS Systems Manager"]
ALWAYS = True  # los parámetros Standard son gratis: el billing no los muestra
GLOBAL = False


def collect(session, region, account_id):
    ssm = client(session, "ssm", region)
    recursos = []
    for pagina in ssm.get_paginator("describe_parameters").paginate():
        for p in pagina["Parameters"]:
            nombre = p["Name"]
            ruta = nombre if nombre.startswith("/") else f"/{nombre}"
            recursos.append(recurso(
                account_id, region, NAME, "parameter", nombre,
                arn=p.get("ARN") or f"arn:aws:ssm:{region}:{account_id}:parameter{ruta}",
                name=nombre,
                attrs={
                    "type": p.get("Type"),          # String | StringList | SecureString
                    "tier": p.get("Tier"),
                    "kms_key_id": p.get("KeyId"),
                    "last_modified": p.get("LastModifiedDate"),
                    "version": p.get("Version"),
                },
            ))
    for pagina in ssm.get_paginator("list_documents").paginate(Filters=[{"Key": "Owner", "Values": ["Self"]}]):
        for d in pagina["DocumentIdentifiers"]:
            recursos.append(recurso(
                account_id, region, NAME, "document", d["Name"],
                arn=f"arn:aws:ssm:{region}:{account_id}:document/{d['Name']}",
                name=d["Name"], tags=d.get("Tags"),
                attrs={"document_type": d.get("DocumentType"), "format": d.get("DocumentFormat"),
                       "version": d.get("DocumentVersion")},
            ))
    return recursos
