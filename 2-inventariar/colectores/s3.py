"""Buckets S3 (global: corre una vez por cuenta). Solo configuración, nunca objetos."""

from botocore.exceptions import ClientError

from common import recurso
from common.aws import client

NAME = "s3"
TRIGGERS = ["Amazon Simple Storage Service"]
ALWAYS = False
GLOBAL = True

# Códigos que significan "no configurado", no un error real.
_NO_CONFIGURADO = {"NoSuchTagSet", "ServerSideEncryptionConfigurationNotFoundError",
                   "NoSuchPublicAccessBlockConfiguration"}


def _campo(fn, **kw):
    """Ejecuta un Get* y devuelve (respuesta, error). Un fallo por campo no tumba el bucket."""
    try:
        return fn(**kw), None
    except ClientError as e:
        codigo = e.response.get("Error", {}).get("Code", "Error")
        return None, (None if codigo in _NO_CONFIGURADO else codigo)


def _region(s3, bucket):
    # LocationConstraint vacío = us-east-1; "EU" es el alias histórico de eu-west-1.
    resp, err = _campo(s3.get_bucket_location, Bucket=bucket["Name"])
    if err or resp is None:
        return None
    loc = resp.get("LocationConstraint")
    return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)


def collect(session, region, account_id):
    s3 = client(session, "s3", region)
    clientes = {region: s3}
    recursos = []
    for pagina in s3.get_paginator("list_buckets").paginate():
        for b in pagina["Buckets"]:
            nombre = b["Name"]
            # list_buckets ya trae BucketRegion en versiones nuevas; si no, get_bucket_location.
            reg = b.get("BucketRegion") or _region(s3, b)
            # Cliente en la región del bucket para evitar redirecciones.
            c = clientes.get(reg or region) or clientes.setdefault(reg, client(session, "s3", reg))
            errores = {}

            tags, err = _campo(c.get_bucket_tagging, Bucket=nombre)
            if err:
                errores["tags"] = err
            ver, err = _campo(c.get_bucket_versioning, Bucket=nombre)
            if err:
                errores["versioning"] = err
            enc, err = _campo(c.get_bucket_encryption, Bucket=nombre)
            if err:
                errores["encryption"] = err
            pab, err = _campo(c.get_public_access_block, Bucket=nombre)
            if err:
                errores["public_access_block"] = err

            reglas = ((enc or {}).get("ServerSideEncryptionConfiguration") or {}).get("Rules", [])
            sse = (reglas[0].get("ApplyServerSideEncryptionByDefault") or {}) if reglas else {}
            bloqueo = (pab or {}).get("PublicAccessBlockConfiguration")
            recursos.append(recurso(
                account_id, reg or region, NAME, "bucket", nombre,
                arn=f"arn:aws:s3:::{nombre}",
                name=nombre,
                tags=(tags or {}).get("TagSet"),
                attrs={
                    "creation_date": b.get("CreationDate"),
                    "region_unknown": reg is None,
                    "versioning": (ver or {}).get("Status", "Disabled") if ver is not None else None,
                    "mfa_delete": (ver or {}).get("MFADelete"),
                    "encryption": sse.get("SSEAlgorithm"),
                    "kms_key_id": sse.get("KMSMasterKeyID"),
                    "bucket_key_enabled": reglas[0].get("BucketKeyEnabled") if reglas else None,
                    # None = sin bloqueo configurado (o no se pudo leer, ver errors).
                    "public_access_block": bloqueo,
                    "public_access_fully_blocked": (None if "public_access_block" in errores
                                                    else bool(bloqueo) and all(bloqueo.values())),
                    "errors": errores,
                },
            ))
    return recursos
