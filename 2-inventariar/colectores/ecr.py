"""Repositorios ECR privados."""

from common import recurso
from common.aws import client

NAME = "ecr"
TRIGGERS = ["Amazon EC2 Container Registry (ECR)"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ecr = client(session, "ecr", region)
    recursos = []
    for pagina in ecr.get_paginator("describe_repositories").paginate():
        for r in pagina["repositories"]:
            n_imagenes, bytes_total = _tamano(ecr, r["repositoryName"])
            recursos.append(recurso(
                account_id, region, NAME, "repository", r["repositoryName"],
                arn=r["repositoryArn"],
                name=r["repositoryName"],
                tags=_tags(ecr, r["repositoryArn"]),
                attrs={
                    "uri": r.get("repositoryUri"),
                    "created_at": r.get("createdAt"),
                    "tag_mutability": r.get("imageTagMutability"),
                    "scan_on_push": (r.get("imageScanningConfiguration") or {}).get("scanOnPush", False),
                    "encryption": (r.get("encryptionConfiguration") or {}).get("encryptionType"),
                    "image_count": n_imagenes,
                    # Aproximado: suma de tamaños comprimidos; ECR cobra por capas deduplicadas.
                    "approx_size_gb": round(bytes_total / 1024 ** 3, 3) if bytes_total is not None else None,
                },
            ))
    return recursos


def _tamano(ecr, repo):
    # Metadatos de imágenes (1 llamada por cada 1000 imágenes); nunca se bajan capas.
    try:
        n, total = 0, 0
        for p in ecr.get_paginator("describe_images").paginate(repositoryName=repo,
                                                                 PaginationConfig={"PageSize": 1000}):
            for img in p["imageDetails"]:
                n += 1
                total += img.get("imageSizeInBytes", 0)
        return n, total
    except Exception:  # noqa: BLE001
        return None, None


def _tags(ecr, arn):
    try:
        return ecr.list_tags_for_resource(resourceArn=arn).get("tags", [])
    except Exception:  # noqa: BLE001
        return {}
