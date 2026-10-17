"""Funciones Lambda. El archivo lleva guion bajo porque `lambda` es palabra reservada."""

from common import recurso
from common.aws import client

NAME = "lambda"
TRIGGERS = ["AWS Lambda"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    lam = client(session, "lambda", region)
    recursos = []
    for pagina in lam.get_paginator("list_functions").paginate():
        for f in pagina["Functions"]:
            # list_functions trae "Environment" con las variables de entorno (suelen tener
            # credenciales y URLs internas). Se descarta sin leerlo: CONTRATO.md, regla 2.
            f.pop("Environment", None)
            vpc = f.get("VpcConfig") or {}
            recursos.append(recurso(
                account_id, region, NAME, "function", f["FunctionName"],
                arn=f["FunctionArn"],
                name=f["FunctionName"],
                state=f.get("State"),  # list_functions a veces no lo trae
                tags=_tags(lam, f["FunctionArn"]),
                attrs={
                    "runtime": f.get("Runtime"),  # None en funciones de imagen de contenedor
                    "package_type": f.get("PackageType"),
                    "memory_mb": f.get("MemorySize"),
                    "timeout_s": f.get("Timeout"),
                    "architectures": f.get("Architectures", []),
                    "ephemeral_storage_mb": (f.get("EphemeralStorage") or {}).get("Size"),
                    "last_modified": f.get("LastModified"),
                    "role": f.get("Role"),
                    "layers": [l["Arn"] for l in f.get("Layers", [])],
                },
                relations={
                    "vpc_id": vpc.get("VpcId") or None,
                    "subnet_ids": vpc.get("SubnetIds", []),
                    "security_group_ids": vpc.get("SecurityGroupIds", []),
                },
            ))
    return recursos


def _tags(lam, arn):
    # Sin permiso de tags no se pierde la función: solo sale sin tags.
    try:
        return lam.list_tags(Resource=arn).get("Tags", {})
    except Exception:  # noqa: BLE001
        return {}
