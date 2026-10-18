"""Helpers comunes para construir findings con la forma de CONTRATO.md.

Uso: from hallazgos import hallazgo, severidad_por_costo
"""

import hashlib
from datetime import datetime, timezone

SEVERIDADES = ["alta", "media", "baja"]
CATEGORIAS_AHORRO = {"desperdicio", "costo"}

# Problemas iguales detectados por fuentes distintas → un solo issue_id, así el fingerprint no cambia si
# una corrida se hace sin aws-doctor (o aws-doctor falla en una región) y la regla lo detecta igual.
ISSUE_CANONICO = {
    "regla.ebs_sin_adjuntar": "ebs.unattached", "aws-doctor.unused_ebs_volumes": "ebs.unattached",
    "regla.eip_sin_asociar": "eip.unassociated", "aws-doctor.unused_elastic_ips": "eip.unassociated",
    "regla.ec2_detenida": "ec2.stopped", "aws-doctor.stopped_instances": "ec2.stopped",
    # Un snapshot "huérfano" (volumen borrado) para aws-doctor suele ser "viejo" para la regla: es el mismo
    # snapshot a revisar, y así no cambia de identidad entre corridas con y sin aws-doctor.
    "regla.snapshots_viejos": "ebs_snapshot.old", "aws-doctor.stale_snapshots": "ebs_snapshot.old",
    "aws-doctor.orphaned_snapshots": "ebs_snapshot.old",
    "regla.root_sin_mfa": "root.no_mfa", "aws-doctor.root_user_waste": "root.no_mfa",
}
FUENTE_PRECIO = {"regla": "static_estimate", "aws-doctor": "aws_doctor", "cruce-billing": "ce_actual"}


def huella(issue_id, account_id, region, resource_id, discriminador=None):
    """Identidad estable de un finding entre corridas: no depende del título, del costo ni del orden."""
    clave = "|".join(str(x) if x is not None else "" for x in (issue_id, account_id, region, resource_id,
                                                                 discriminador))
    return hashlib.sha256(clave.encode()).hexdigest()[:16]


def completar_v2(f):
    """Agrega los campos v2 (CONTRATO.md) a un finding que ya tiene rule_id. Idempotente."""
    rule_id = f.get("rule_id") or f"{f['source']}.desconocida"
    issue = ISSUE_CANONICO.get(rule_id, rule_id)
    disc = f.pop("_discriminador", None)
    f.update({
        "rule_id": rule_id,
        "issue_id": issue,
        "fingerprint": f.get("fingerprint") or huella(issue, f["account_id"], f["region"], f["resource_id"], disc),
        "price_source": FUENTE_PRECIO.get(f["source"]) if f["monthly_usd"] is not None else None,
        "is_saving": f["category"] in CATEGORIAS_AHORRO,
    })
    return f


def hallazgo(category, severity, account_id, region, resource_id, title, detail, monthly_usd, source,
             rule_id=None, discriminador=None):
    """Un finding con las claves exactas del contrato (el id se asigna al final, al ordenar). Los campos v2
    (issue_id, fingerprint...) se completan en validar con completar_v2; rule_id puede venir después."""
    f = {
        "id": None,
        "category": category,
        "severity": severity,
        "account_id": account_id,
        "region": region,
        "resource_id": resource_id,
        "title": title,
        "detail": detail,
        "monthly_usd": round(monthly_usd, 2) if monthly_usd is not None else None,
        "source": source,
    }
    if rule_id:
        f["rule_id"] = rule_id
    if discriminador is not None:
        f["_discriminador"] = discriminador
    return f


def severidad_por_costo(usd, base="baja"):
    """La severidad más alta entre la base de la regla y la que sugiere el costo mensual."""
    por_costo = "baja"
    if usd is not None and usd >= 100:
        por_costo = "alta"
    elif usd is not None and usd >= 10:
        por_costo = "media"
    return min(base, por_costo, key=SEVERIDADES.index)


def parsear_fecha(valor):
    """Fecha ISO (o con espacio, como la serializa json default=str) → datetime UTC. None si no se puede."""
    if not valor:
        return None
    if isinstance(valor, datetime):
        fecha = valor
    else:
        try:
            fecha = datetime.fromisoformat(str(valor).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)
