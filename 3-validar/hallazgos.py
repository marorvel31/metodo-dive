"""Helpers comunes para construir findings con la forma de CONTRATO.md.

Uso: from hallazgos import hallazgo, severidad_por_costo
"""

from datetime import datetime, timezone

SEVERIDADES = ["alta", "media", "baja"]


def hallazgo(category, severity, account_id, region, resource_id, title, detail, monthly_usd, source):
    """Un finding con las claves exactas del contrato (el id se asigna al final, al ordenar)."""
    return {
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
