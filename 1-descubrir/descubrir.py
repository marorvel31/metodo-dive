"""Fase 1 · Descubrir: usa Cost Explorer para saber qué cuentas, regiones y servicios tienen costo.

Lo que no factura casi nunca existe, así que el billing es el mapa para no escanear
todas las regiones de todas las cuentas. Escribe discovery.json (ver CONTRATO.md).

Llamadas a Cost Explorer (cada una cuesta USD 0.01), sin importar cuántas cuentas haya:
  1 llamada   GroupBy LINKED_ACCOUNT + REGION         → qué regiones tienen costo
  R llamadas  GroupBy LINKED_ACCOUNT + SERVICE, Filter REGION = r (una por región con costo)
  R llamadas  GroupBy LINKED_ACCOUNT + USAGE_TYPE, Filter REGION = r (Cost Explorer admite solo 2 GroupBy)
  1 llamada   GroupBy LINKED_ACCOUNT + USAGE_TYPE de los servicios globales (CloudFront)
  Total = 2 + 2R, más una llamada extra por cada página (NextPageToken). R suele ser 3-8.
  El número de cuentas solo influye en la paginación, no en el número de llamadas base.

Uso: python 1-descubrir/descubrir.py [--config dive.config.yaml] [--meses 6]
"""

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

from common import (SERVICIOS_GOBIERNO, ahora, cargar_config, error_corto, escribir_json,  # noqa: E402
                    marcar_ultima, nueva_corrida, run_de)
from common.aws import client, listar_cuentas, sesion_management  # noqa: E402

# Solo cuenta el USO de recursos. Se dejan fuera impuestos, créditos y sobre todo las cuotas de
# RIs / Savings Plans (Recurring, Upfront...): las paga la cuenta que compró la reserva aunque
# ahí no exista ningún recurso, y confunden el cruce billing ↔ inventario.
RECORD_TYPES_USO = ["Usage", "DiscountedUsage", "SavingsPlanCoveredUsage"]

# "Servicios" de Cost Explorer que no corresponden a recursos inventariables.
SERVICIOS_NO_RECURSOS = [
    "Tax",
    "Refund",
    "Savings Plans for AWS Compute usage",
    "Savings Plans for AWS Database Usage",
    "Savings Plans for AWS Machine Learning",
    "AWS Support (Basic)",
    "AWS Support (Developer)",
    "AWS Support (Business)",
    "AWS Support (Enterprise)",
    "AWS Support (Enterprise On-Ramp)",
    "AWS Business Support+",
]

# Servicios globales que Cost Explorer reparte por la región del edge que sirvió el tráfico
# (CloudFront factura en sa-east-1, me-south-1, af-south-1...). Su costo va a "global": si no,
# aparecen como "regiones sorpresa" y la fase 2 escanea regiones donde no hay nada (a veces ni habilitadas).
SERVICIOS_GLOBALES = {"Amazon CloudFront"}

# Valores de la dimensión REGION que se agrupan como "global".
REGIONES_GLOBALES = {"", "NoRegion", "global", "Global"}

UMBRAL_SORPRESA = 0.05  # regiones con menos del 5% del costo de la cuenta



# ---------- Periodo ----------

def periodo(meses, hoy=None):
    """Últimos `meses` meses completos: [día 1 de hace N meses, día 1 del mes actual)."""
    hoy = hoy or date.today()
    fin = hoy.replace(day=1)
    anio, mes = fin.year, fin.month - meses
    while mes <= 0:
        mes += 12
        anio -= 1
    return date(anio, mes, 1), fin


def normalizar_region(region):
    return "global" if (region or "") in REGIONES_GLOBALES else region


# ---------- Cost Explorer ----------

def filtro_base(incluir=None, region=None, sin_servicios=(), solo_servicios=()):
    """Solo registros de uso, sin servicios que no son recursos."""
    condiciones = [
        {"Dimensions": {"Key": "RECORD_TYPE", "Values": RECORD_TYPES_USO}},
        {"Not": {"Dimensions": {"Key": "SERVICE", "Values": SERVICIOS_NO_RECURSOS + sorted(sin_servicios)}}},
    ]
    if solo_servicios:
        condiciones.append({"Dimensions": {"Key": "SERVICE", "Values": sorted(solo_servicios)}})
    if incluir:
        condiciones.append({"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": sorted(incluir)}})
    if region is not None:
        condiciones.append({"Dimensions": {"Key": "REGION", "Values": [region]}})
    return {"And": condiciones}


def consultar(ce, inicio, fin, group_by, filtro, contador):
    """Recorre todas las páginas de get_cost_and_usage. Devuelve (mes, clave1, clave2, usd)."""
    filas, token = [], None
    while True:
        params = {
            "TimePeriod": {"Start": inicio.isoformat(), "End": fin.isoformat()},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": k} for k in group_by],
            "Filter": filtro,
        }
        if token:
            params["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**params)
        contador["llamadas"] += 1
        for resultado in resp.get("ResultsByTime", []):
            mes = resultado["TimePeriod"]["Start"][:7]
            for g in resultado.get("Groups", []):
                contador.setdefault("monedas", set()).add(g["Metrics"]["UnblendedCost"].get("Unit") or "USD")
                usd = float(g["Metrics"]["UnblendedCost"]["Amount"])
                filas.append((mes, g["Keys"][0], g["Keys"][1], usd))
        token = resp.get("NextPageToken")
        if not token:
            return filas


def descubrir(ce, cuentas, cfg, hoy=None):
    """Arma discovery.json a partir de Cost Explorer. `cuentas` = {account_id: nombre}."""
    meses = int(cfg["billing_months"])
    umbral = float(cfg["threshold_usd"])
    inicio, fin = periodo(meses, hoy)
    incluir = {str(x) for x in cfg.get("include_accounts") or []}
    contador, errores = {"llamadas": 0}, []

    # 1) Regiones con costo (valor crudo de CE, para poder filtrar por él después).
    por_region = defaultdict(float)
    for _, cuenta, region, usd in consultar(ce, inicio, fin, ["LINKED_ACCOUNT", "REGION"],
                                            filtro_base(incluir), contador):
        if cuenta in cuentas:
            por_region[region] += usd
    regiones_crudas = sorted(r for r, usd in por_region.items() if usd >= umbral)

    # 2) Servicios por cuenta dentro de cada región. costos[cuenta][region][servicio][mes] = usd
    costos = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(float))))
    for cruda in regiones_crudas:
        try:
            filas = consultar(ce, inicio, fin, ["LINKED_ACCOUNT", "SERVICE"],
                              filtro_base(incluir, cruda), contador)
        except Exception as e:  # noqa: BLE001 — una región fallida no detiene la corrida
            errores.append({"region": cruda, "error": error_corto(e)})
            continue
        region = normalizar_region(cruda)
        for mes, cuenta, servicio, usd in filas:
            if cuenta in cuentas and servicio not in SERVICIOS_NO_RECURSOS:
                destino = "global" if servicio in SERVICIOS_GLOBALES else region
                costos[cuenta][destino][servicio][mes] += usd

    # 2b) Tipos de uso (USAGE_TYPE) por cuenta y región: dicen QUÉ se paga dentro de cada servicio (NAT,
    #     transferencia, almacenamiento, métricas...). Cost Explorer admite solo 2 GroupBy, así que es otra
    #     consulta por región, sin servicio. Los servicios globales (CloudFront) se consultan una sola vez
    #     y van a "global", igual que arriba; si no, sus regiones de edge volverían a aparecer.
    usos = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(float))))
    consultas_uso = [(cruda, filtro_base(incluir, cruda, sin_servicios=SERVICIOS_GLOBALES)) for cruda in regiones_crudas]
    consultas_uso.append(("global", filtro_base(incluir, solo_servicios=SERVICIOS_GLOBALES)))
    for cruda, filtro in consultas_uso:
        try:
            filas = consultar(ce, inicio, fin, ["LINKED_ACCOUNT", "USAGE_TYPE"], filtro, contador)
        except Exception as e:  # noqa: BLE001 — sin tipos de uso el discovery sigue siendo válido
            errores.append({"region": cruda, "query": "usage_type", "error": error_corto(e)})
            continue
        region = normalizar_region(cruda)
        for mes, cuenta, tipo, usd in filas:
            if cuenta in cuentas:
                usos[cuenta][region][tipo][mes] += usd

    # 3) Umbral por servicio (sobre el total del periodo) y forma del CONTRATO.
    salida_cuentas = {}
    for cuenta in sorted(costos):
        regiones = {}
        for region in sorted(costos[cuenta]):
            servicios, mensual, servicios_mes = {}, defaultdict(float), {}
            for servicio, por_mes in costos[cuenta][region].items():
                total = sum(por_mes.values())
                if total < umbral:
                    continue
                servicios[servicio] = round(total, 2)
                servicios_mes[servicio] = {m: round(v, 2) for m, v in sorted(por_mes.items())}
                for mes, usd in por_mes.items():
                    mensual[mes] += usd
            if servicios:
                tipos = {t: {m: round(v, 2) for m, v in sorted(por_mes.items())}
                         for t, por_mes in usos[cuenta][region].items() if sum(por_mes.values()) >= umbral}
                regiones[region] = {
                    "total_usd": round(sum(servicios.values()), 2),
                    "services": dict(sorted(servicios.items(), key=lambda x: -x[1])),
                    "monthly_usd": {m: round(v, 2) for m, v in sorted(mensual.items())},
                    "services_monthly_usd": servicios_mes,
                    "usage_types_monthly_usd": dict(sorted(tipos.items(), key=lambda x: -sum(x[1].values()))),
                }
        if regiones:
            salida_cuentas[cuenta] = {
                "name": cuentas[cuenta],
                "total_usd": round(sum(r["total_usd"] for r in regiones.values()), 2),
                "regions": regiones,
            }

    monedas = sorted(contador.get("monedas") or {"USD"})
    if len(monedas) > 1:  # todo se suma como si fuera una sola moneda: que se vea, y sin elegir una al azar
        errores.append({"region": None, "error": f"Cost Explorer devolvió varias monedas: {', '.join(monedas)}"})
    discovery = {
        "currency": monedas[0] if len(monedas) == 1 else None,
        "generated_at": ahora(),
        "period": {"start": inicio.isoformat(), "end": fin.isoformat(), "months": meses},
        "threshold_usd": umbral,
        "accounts": salida_cuentas,
        "errors": errores,
    }
    return discovery, contador["llamadas"], errores


# ---------- Resumen ----------

def regiones_sorpresa(discovery, umbral=UMBRAL_SORPRESA):
    """(cuenta, nombre, region, usd, %) de regiones con <5% del costo de su cuenta."""
    sorpresas = []
    for cuenta, datos in discovery["accounts"].items():
        total = datos["total_usd"]
        for region, r in datos["regions"].items():
            if region == "global" or not total or len(datos["regions"]) < 2:
                continue
            if set(r["services"]) <= SERVICIOS_GOBIERNO:
                continue
            pct = r["total_usd"] / total
            if pct < umbral:
                sorpresas.append((cuenta, datos["name"], region, r["total_usd"], pct))
    return sorted(sorpresas, key=lambda x: x[4])


def imprimir_resumen(discovery, llamadas, errores, top=10):
    cuentas = discovery["accounts"]
    p = discovery["period"]
    total = sum(c["total_usd"] for c in cuentas.values())
    print(f"\nPeriodo {p['start']} → {p['end']} ({p['months']} meses) · umbral USD {discovery['threshold_usd']}")
    print(f"{len(cuentas)} cuentas con costo · USD {total:,.2f} en total · {llamadas} llamadas a Cost Explorer")

    print("\nCuentas y regiones:")
    for cuenta, datos in sorted(cuentas.items(), key=lambda x: -x[1]["total_usd"]):
        regiones = sorted(datos["regions"].items(), key=lambda x: -x[1]["total_usd"])
        detalle = ", ".join(f"{r} ({v['total_usd']:,.0f})" for r, v in regiones)
        print(f"  {cuenta} {datos['name'][:30]:<30} USD {datos['total_usd']:>11,.2f}  {detalle}")

    servicios = defaultdict(float)
    for datos in cuentas.values():
        for r in datos["regions"].values():
            for s, usd in r["services"].items():
                servicios[s] += usd
    print(f"\nTop {top} servicios:")
    for s, usd in sorted(servicios.items(), key=lambda x: -x[1])[:top]:
        print(f"  USD {usd:>11,.2f}  {s}")

    sorpresas = regiones_sorpresa(discovery)
    print(f"\nRegiones sorpresa (<{UMBRAL_SORPRESA:.0%} del costo de la cuenta): {len(sorpresas)}")
    for cuenta, nombre, region, usd, pct in sorpresas:
        servicios_r = ", ".join(cuentas[cuenta]["regions"][region]["services"])
        print(f"  {cuenta} {nombre[:25]:<25} {region:<15} USD {usd:>9,.2f} ({pct:.1%})  {servicios_r}")

    for e in errores:
        print(f"  ⚠ región {e['region']!r}: {e['error']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--meses", type=int, help="Sobrescribe billing_months del config")
    args = parser.parse_args()

    cfg = cargar_config(args.config)
    if args.meses:
        cfg["billing_months"] = args.meses

    session = sesion_management(cfg)
    org = client(session, "organizations", "us-east-1").describe_organization()["Organization"]
    cuentas = listar_cuentas(cfg, session)
    print(f"{len(cuentas)} cuentas ACTIVE después de include/exclude")
    ce = client(session, "ce", "us-east-1")

    discovery, llamadas, errores = descubrir(ce, cuentas, cfg)
    carpeta = nueva_corrida(cfg)
    discovery = {"run": run_de(carpeta, org["Id"], org["MasterAccountId"], cfg, discovery.pop("currency")),
                 **discovery}
    ruta = escribir_json(carpeta / "discovery.json", discovery)
    marcar_ultima(carpeta)
    imprimir_resumen(discovery, llamadas, errores)
    print(f"\n→ {ruta}")


if __name__ == "__main__":
    main()
