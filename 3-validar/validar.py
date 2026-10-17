"""Fase 3 · Validar: cruza billing, inventario y aws-doctor para producir findings.

Lee discovery.json + inventory.json de la carpeta de salida y escribe ahí mismo
findings.json (forma de CONTRATO.md) y findings.md (resumen ordenado por severidad y $).

Tres fuentes:
  a) aws-doctor  → doctor.py  (salida cruda en salida/<fecha>/aws-doctor/)
  b) cruce-billing → este archivo: lo que se cobra y no está inventariado, servicios sin
     colector y regiones sorpresa
  c) regla       → reglas.py  (desperdicio y riesgo sobre los recursos inventariados)

Uso: python 3-validar/validar.py [--inventory salida/<fecha>/inventory.json] [--discovery ...]
                                 [--sin-doctor] [--timeout-doctor 900]
"""

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "2-inventariar"))
sys.path.insert(0, str(AQUI))

from common import SERVICIOS_GOBIERNO, ahora, cargar_config, carpeta_salida, escribir_json, leer_json  # noqa: E402

import doctor  # noqa: E402
import reglas  # noqa: E402
from hallazgos import SEVERIDADES, hallazgo, severidad_por_costo  # noqa: E402

FUENTE_CRUCE = "cruce-billing"
UMBRAL_REGION_SORPRESA = 0.05   # región con menos de este % del costo de la cuenta
MIN_USD_MES_COBRO = 1.0         # por debajo, "se cobra pero no está inventariado" es ruido
# Servicios que son cargos de facturación, no recursos: no se esperan colectores para ellos.
SIN_RECURSOS = ("Tax", "AWS Support", "AWS Business Support", "AWS Enterprise Support", "AWS Developer Support",
                "AWS Cost Explorer", "Savings Plans", "AWS Billing Conductor", "Refund", "Credit")


# ---------- colectores (solo sus metadatos NAME/TRIGGERS/GLOBAL) ----------

def cargar_colectores():
    """Importa los módulos de 2-inventariar/colectores igual que inventariar.py."""
    import colectores
    mods = []
    for info in pkgutil.iter_modules(colectores.__path__):
        try:
            mod = importlib.import_module(f"colectores.{info.name}")
        except Exception as e:  # noqa: BLE001 — un colector roto no impide validar
            print(f"⚠ no pude importar colectores.{info.name}: {e}")
            continue
        if hasattr(mod, "collect") and hasattr(mod, "TRIGGERS"):
            mods.append(mod)
    return mods


# ---------- b) cruce billing ↔ inventario ----------

def _meses(discovery):
    return (discovery.get("period") or {}).get("months") or 1


def _ultimo_mes(discovery):
    """'YYYY-MM' del último mes completo del periodo (period.end es exclusivo)."""
    fin = (discovery.get("period") or {}).get("end")
    if not fin:
        return None
    anio, mes = int(fin[:4]), int(fin[5:7])
    anio, mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
    return f"{anio:04d}-{mes:02d}"


def _sumar_series(*series):
    total = {}
    for serie in series:
        for mes, usd in (serie or {}).items():
            total[mes] = total.get(mes, 0.0) + usd
    return total


def _es_cargo_sin_recurso(servicio):
    return any(servicio.startswith(p) for p in SIN_RECURSOS)


def cruce_billing(discovery, inventory, mods):
    meses = _meses(discovery)
    ultimo = _ultimo_mes(discovery)
    findings = []

    # Índices del inventario: (cuenta, región, colector) y (cuenta, colector) con al menos un recurso.
    con_recursos_region, con_recursos_cuenta = set(), set()
    for r in inventory.get("resources", []):
        con_recursos_region.add((r["account_id"], r["region"], r["service"]))
        con_recursos_cuenta.add((r["account_id"], r["service"]))
    escaneado = {(s["account_id"], s["region"], s["collector"]) for s in inventory.get("scanned", [])}
    omitidas = {(s["account_id"], s["region"]) for s in inventory.get("skipped", [])}
    errores = {}
    for e in inventory.get("errors", []):
        errores.setdefault((e.get("account_id"), e.get("collector")), []).append(e)

    por_servicio = {}
    for m in mods:
        for s in m.TRIGGERS:
            por_servicio.setdefault(s, []).append(m)

    sin_colector = {}  # servicio → {cuenta: usd total}
    for account_id, cuenta in discovery["accounts"].items():
        regiones = cuenta.get("regions", {})

        # (1) se cobra pero no está inventariado. Colectores GLOBAL se revisan una vez por cuenta.
        costo_global = {}  # (servicio) → usd total en la cuenta, para servicios con colector GLOBAL
        serie_global = {}  # (servicio) → serie mensual sumada de todas las regiones
        for region, datos in regiones.items():
            series = datos.get("services_monthly_usd") or {}
            for servicio, usd in datos.get("services", {}).items():
                mods_s = por_servicio.get(servicio)
                if not mods_s:
                    if not _es_cargo_sin_recurso(servicio):
                        sin_colector.setdefault(servicio, {}).setdefault(account_id, 0.0)
                        sin_colector[servicio][account_id] += usd
                    continue
                if any(m.GLOBAL for m in mods_s):
                    costo_global[servicio] = costo_global.get(servicio, 0.0) + usd
                    serie_global[servicio] = _sumar_series(serie_global.get(servicio), series.get(servicio))
                    continue
                if region == "global":
                    continue  # colector regional con costo sin región: no hay dónde buscar
                hay = any((account_id, region, m.NAME) in con_recursos_region for m in mods_s)
                if not hay and (account_id, region) in omitidas:
                    continue  # región no habilitada hoy: lo cobrado fue antes de deshabilitarla
                if not hay and usd / meses >= MIN_USD_MES_COBRO:
                    findings.append(_no_inventariado(account_id, region, servicio, usd / meses, mods_s,
                                                     escaneado, errores, series.get(servicio), ultimo))

        for servicio, usd in costo_global.items():
            mods_s = por_servicio[servicio]
            hay = any((account_id, m.NAME) in con_recursos_cuenta for m in mods_s)
            if not hay and usd / meses >= MIN_USD_MES_COBRO:
                findings.append(_no_inventariado(account_id, None, servicio, usd / meses, mods_s, escaneado, errores,
                                                 serie_global.get(servicio), ultimo))

        # (3) regiones sorpresa: poco costo comparado con el total de la cuenta.
        total = cuenta.get("total_usd") or sum(d.get("total_usd", 0) for d in regiones.values())
        reales = [r for r in regiones if r != "global"]
        if total and len(reales) > 1:
            for region in reales:
                datos = regiones[region]
                usd = datos.get("total_usd", 0)
                if set(datos.get("services", {})) <= SERVICIOS_GOBIERNO:
                    continue
                if 0 < usd < total * UMBRAL_REGION_SORPRESA:
                    servicios = ", ".join(f"{s} ${v:.2f}" for s, v in
                                          sorted(datos.get("services", {}).items(), key=lambda x: -x[1]))
                    inventariados = sorted({s for (a, reg, s) in con_recursos_region if a == account_id and reg == region})
                    findings.append(hallazgo(
                        "riesgo", "baja", account_id, region, None, "Región sorpresa (poco costo, posible olvido)",
                        f"{usd / total:.1%} del costo de la cuenta en el periodo. Servicios: {servicios}. "
                        f"Colectores con recursos aquí: {', '.join(inventariados) or 'ninguno'}.",
                        usd / meses, FUENTE_CRUCE))

    # (2) servicios con costo sin colector: uno por servicio, con las cuentas afectadas.
    for servicio, cuentas in sorted(sin_colector.items()):
        total = sum(cuentas.values())
        detalle = ", ".join(f"{a} ${v / meses:.2f}/mes" for a, v in sorted(cuentas.items(), key=lambda x: -x[1]))
        findings.append(hallazgo("inventario", "baja", None, None, None, f"Servicio sin colector: {servicio}",
                                 f"Se cobra en {len(cuentas)} cuenta(s): {detalle}.", total / meses, FUENTE_CRUCE))
    return findings


def _no_inventariado(account_id, region, servicio, usd_mes, mods_s, escaneado, errores, serie=None, ultimo=None):
    nombres = [m.NAME for m in mods_s]
    # Con la serie mensual se distingue "se sigue cobrando" de "ya se borró dentro del periodo".
    if serie and ultimo:
        meses_con_cobro = sorted(m for m, v in serie.items() if v >= MIN_USD_MES_COBRO)
        if meses_con_cobro and ultimo not in meses_con_cobro:
            return hallazgo("inventario", "baja", account_id, region, None,
                            f"Dejó de cobrarse (recurso ya borrado): {servicio}",
                            f"Último mes con cobro: {meses_con_cobro[-1]} (~${serie[meses_con_cobro[-1]]:.2f}). "
                            f"Nada que limpiar; confirma que el borrado fue intencional.", 0.0, FUENTE_CRUCE)
        usd_mes = serie.get(ultimo, usd_mes)  # el último mes es más fiel que el promedio del periodo
    fallos = [e for n in nombres for e in errores.get((account_id, n), [])
              if region is None or e.get("region") in (region, None)]
    corrio = any((account_id, region, n) in escaneado for n in nombres) if region else \
        any(a == account_id and c in nombres for (a, _, c) in escaneado)
    if fallos:
        estado = "el colector falló: " + "; ".join(f"{e.get('collector')}: {e.get('error', '')[:120]}" for e in fallos[:3])
    elif corrio:
        estado = "el colector corrió y no encontró recursos (¿recursos que el colector no cubre, o ya borrados?)"
    else:
        estado = "el colector no se ejecutó aquí (¿corrida con --solo?)"
    return hallazgo("inventario", severidad_por_costo(usd_mes, "baja"), account_id, region, None,
                    f"Se cobra pero no está inventariado: {servicio}",
                    f"~${usd_mes:.2f}/mes; colectores {', '.join(nombres)}: {estado}.", usd_mes, FUENTE_CRUCE)


# ---------- consolidar y escribir ----------

def deduplicar(findings):
    """Si aws-doctor y una regla marcan el mismo recurso con la misma categoría, queda el primero.

    Además, si un recurso es desperdicio (ej. volumen sin adjuntar), se descarta su finding de costo
    (ej. migrar gp2→gp3): lo que se recomienda es borrarlo, y sumar los dos duplicaría el ahorro.
    """
    vistos, salida = set(), []
    desperdicio = {(f["account_id"], f["region"], f["resource_id"]) for f in findings
                   if f["resource_id"] and f["category"] == "desperdicio"}
    for f in findings:
        recurso = (f["account_id"], f["region"], f["resource_id"])
        if f["resource_id"] and f["category"] == "costo" and recurso in desperdicio:
            continue
        llave = (*recurso, f["category"])
        if f["resource_id"] and llave in vistos:
            continue
        vistos.add(llave)
        salida.append(f)
    return salida


def ordenar_y_numerar(findings):
    findings.sort(key=lambda f: (SEVERIDADES.index(f["severity"]), -(f["monthly_usd"] or 0),
                                 f["account_id"] or "", f["region"] or "", f["title"]))
    ancho = max(3, len(str(len(findings))))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i:0{ancho}d}"
    return findings


def _celda(texto):
    return str(texto if texto is not None else "—").replace("|", "\\|").replace("\n", " ")


def generar_md(findings, discovery, errores):
    nombres = {a: c.get("name", a) for a, c in discovery["accounts"].items()}
    cuenta = lambda a: f"{nombres.get(a, a)} ({a})" if a else "varias"  # noqa: E731
    total = sum(f["monthly_usd"] or 0 for f in findings if f["category"] in ("desperdicio", "costo"))

    lineas = ["# Findings · Método DIVE", "",
              f"Generado: {ahora()} · {len(findings)} findings · {len(discovery['accounts'])} cuentas", "",
              f"Ahorro potencial estimado (desperdicio + costo): **${total:,.2f}/mes**. "
              "Es un tope: son estimaciones, y un recurso marcado como desperdicio no suma además su optimización.", "",
              "| Severidad | Findings | $/mes |", "|---|---:|---:|"]
    for sev in SEVERIDADES:
        grupo = [f for f in findings if f["severity"] == sev]
        lineas.append(f"| {sev} | {len(grupo)} | ${sum(f['monthly_usd'] or 0 for f in grupo):,.2f} |")

    fuentes = {}
    for f in findings:
        fuentes[f["source"]] = fuentes.get(f["source"], 0) + 1
    lineas += ["", "Por fuente: " + " · ".join(f"{s}: {n}" for s, n in sorted(fuentes.items())), ""]

    for sev in SEVERIDADES:
        grupo = [f for f in findings if f["severity"] == sev]
        if not grupo:
            continue
        lineas += [f"## Severidad {sev} ({len(grupo)})", "",
                   "| ID | Categoría | Cuenta | Región | Recurso | Hallazgo | $/mes | Fuente |",
                   "|---|---|---|---|---|---|---:|---|"]
        for f in grupo:
            usd = f"${f['monthly_usd']:,.2f}" if f["monthly_usd"] is not None else "—"
            lineas.append(f"| {f['id']} | {f['category']} | {_celda(cuenta(f['account_id']))} | {_celda(f['region'])} | "
                          f"{_celda(f['resource_id'])} | {_celda(f['title'])} | {usd} | {f['source']} |")
        lineas.append("")
        lineas.append("<details><summary>Detalle</summary>\n")
        for f in grupo:
            lineas.append(f"- **{f['id']}** {_celda(f['title'])}: {_celda(f['detail'])}")
        lineas += ["", "</details>", ""]

    if errores:
        lineas += ["## Errores de la validación", ""]
        lineas += [f"- {cuenta(e.get('account_id'))} {e.get('region') or ''} · {e.get('collector')}: {e.get('error')}"
                   for e in errores]
        lineas.append("")
    return "\n".join(lineas)


def validar(cfg, discovery, inventory, salida, sin_doctor=False, timeout_doctor=900):
    """Corre las tres fuentes y escribe findings.json + findings.md en `salida`."""
    salida = Path(salida)
    findings, errores = [], []

    if sin_doctor:
        print("aws-doctor: omitido (--sin-doctor)")
    else:
        creds_fn = None
        if cfg["access_mode"] != "sso":
            from common.aws import sesion_cuenta, sesion_management
            mgmt = sesion_management(cfg)
            creds_fn = lambda a: sesion_cuenta(cfg, a, mgmt).get_credentials().get_frozen_credentials()  # noqa: E731
        habilitadas = {a: c["enabled_regions"] for a, c in inventory.get("accounts", {}).items()
                       if c.get("enabled_regions")}
        f_doc, e_doc = doctor.ejecutar(cfg, discovery, salida, timeout=timeout_doctor, creds_fn=creds_fn,
                                       habilitadas=habilitadas)
        findings += f_doc
        errores += e_doc

    findings += cruce_billing(discovery, inventory, cargar_colectores())
    findings += reglas.aplicar(inventory)
    findings = ordenar_y_numerar(deduplicar(findings))

    # "errors" es adicional al contrato: corridas de aws-doctor que fallaron.
    escribir_json(salida / "findings.json", {"generated_at": ahora(), "findings": findings, "errors": errores})
    (salida / "findings.md").write_text(generar_md(findings, discovery, errores))
    return findings, errores


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--inventory", help="Ruta a inventory.json (por defecto, el de hoy)")
    parser.add_argument("--discovery", help="Ruta a discovery.json (por defecto, junto a inventory.json)")
    parser.add_argument("--sin-doctor", action="store_true", help="No ejecutar aws-doctor")
    parser.add_argument("--timeout-doctor", type=int, default=900, help="Segundos máximos por corrida de aws-doctor")
    args = parser.parse_args()

    cfg = cargar_config(args.config)
    ruta_inv = Path(args.inventory) if args.inventory else carpeta_salida(cfg) / "inventory.json"
    ruta_disc = Path(args.discovery) if args.discovery else ruta_inv.parent / "discovery.json"
    findings, errores = validar(cfg, leer_json(ruta_disc), leer_json(ruta_inv), ruta_inv.parent,
                                args.sin_doctor, args.timeout_doctor)

    conteo = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERIDADES}
    print(f"→ {ruta_inv.parent / 'findings.json'}: {len(findings)} findings "
          f"({', '.join(f'{k}={v}' for k, v in conteo.items())}), {len(errores)} errores")


if __name__ == "__main__":
    main()
