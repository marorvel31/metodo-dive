"""Fase 2 · Inventariar: escanea solo donde la fase 1 encontró costo.

Lee discovery.json, decide qué colectores correr en cada cuenta × región y los ejecuta
en paralelo. Escribe inventory.json en la misma carpeta.

Si existe cobertura.json (fase 3) en la misma carpeta, suma sus `tareas_extra`: colectores en
cuentas/regiones donde el oráculo vio recursos que el billing no mostró (free tier).

Uso: python 2-inventariar/inventariar.py [--discovery salida/<fecha>/discovery.json] [--solo ec2,rds] [--sin-cobertura]
"""

import argparse
import importlib
import pkgutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI))

import boto3  # noqa: E402

from common import ahora, cargar_config, carpeta_salida, error_corto, escribir_json, leer_json  # noqa: E402
from common.aws import regiones_habilitadas, sesion_cuenta, sesion_management  # noqa: E402

import colectores  # noqa: E402

REGION_GLOBAL = "us-east-1"


def cargar_colectores(solo=None):
    mods = []
    for info in pkgutil.iter_modules(colectores.__path__):
        mod = importlib.import_module(f"colectores.{info.name}")
        if hasattr(mod, "collect") and (not solo or mod.NAME in solo):
            mods.append(mod)
    return mods


def planear(discovery, mods):
    """Lista de (account_id, region, colector) a ejecutar según el billing."""
    tareas = []
    for account_id, cuenta in discovery["accounts"].items():
        servicios_cuenta = set()
        for region, datos in cuenta["regions"].items():
            servicios_cuenta |= set(datos["services"])
        for mod in mods:
            if mod.GLOBAL:
                if mod.ALWAYS or servicios_cuenta & set(mod.TRIGGERS):
                    tareas.append((account_id, REGION_GLOBAL, mod))
                continue
            regiones = cuenta["regions"]
            for region, datos in regiones.items():
                if region == "global":
                    continue
                if mod.ALWAYS or set(datos["services"]) & set(mod.TRIGGERS):
                    tareas.append((account_id, region, mod))
            # Costo global (CloudFront) que activa un colector regional: sus recursos viven en us-east-1
            # (ej. los certificados ACM de CloudFront), aunque la cuenta no tenga otro costo ahí.
            servicios_globales = set(regiones.get("global", {}).get("services", {}))
            ya = any(a == account_id and r == REGION_GLOBAL and m is mod for a, r, m in tareas)
            if not ya and servicios_globales & set(mod.TRIGGERS):
                tareas.append((account_id, REGION_GLOBAL, mod))
    return tareas


def credenciales_por_cuenta(cfg, cuentas):
    """Resuelve credenciales una vez por cuenta; cada hilo crea su propia Session con ellas."""
    mgmt = None if cfg["access_mode"] == "sso" else sesion_management(cfg)
    creds, errores = {}, []
    for account_id in cuentas:
        try:
            c = sesion_cuenta(cfg, account_id, mgmt).get_credentials().get_frozen_credentials()
            creds[account_id] = c
        except Exception as e:  # noqa: BLE001 — una cuenta sin acceso no detiene la corrida
            errores.append({"account_id": account_id, "region": None, "collector": "acceso", "error": error_corto(e)})
    return creds, errores


def _sesion(creds):
    return boto3.Session(aws_access_key_id=creds.access_key, aws_secret_access_key=creds.secret_key,
                         aws_session_token=creds.token)


def habilitadas_por_cuenta(creds):
    """{account_id: [regiones habilitadas]}. Si la consulta falla, la cuenta no aparece y no se filtra."""
    habilitadas, errores = {}, []
    for account_id, c in creds.items():
        try:
            habilitadas[account_id] = regiones_habilitadas(_sesion(c))
        except Exception as e:  # noqa: BLE001 — sin la lista se escanea todo, como antes
            errores.append({"account_id": account_id, "region": None, "collector": "regiones", "error": error_corto(e)})
    return habilitadas, errores


def filtrar_habilitadas(tareas, habilitadas):
    """Separa las tareas en regiones no habilitadas (opt-in apagadas): ahí toda llamada falla o se cuelga."""
    validas, omitidas = [], []
    for t in tareas:
        account_id, region, mod = t
        if account_id in habilitadas and region not in habilitadas[account_id]:
            omitidas.append(t)
        else:
            validas.append(t)
    return validas, omitidas


def ejecutar(tarea, creds):
    account_id, region, mod = tarea
    inicio = time.monotonic()
    recursos = mod.collect(_sesion(creds), region, account_id)
    return recursos, round(time.monotonic() - inicio, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--discovery", help="Ruta a discovery.json (por defecto, el de hoy)")
    parser.add_argument("--solo", help="Colectores a correr, separados por coma")
    parser.add_argument("--sin-cobertura", action="store_true", help="Ignorar cobertura.json")
    args = parser.parse_args()

    cfg = cargar_config(args.config)
    ruta_disc = Path(args.discovery) if args.discovery else carpeta_salida(cfg) / "discovery.json"
    discovery = leer_json(ruta_disc)
    salida = ruta_disc.parent

    mods = cargar_colectores(set(args.solo.split(",")) if args.solo else None)
    tareas = planear(discovery, mods)
    ruta_cob = salida / "cobertura.json"
    if ruta_cob.exists() and not args.sin_cobertura:
        por_nombre = {m.NAME: m for m in mods}
        ya = {(a, r, m.NAME) for a, r, m in tareas}
        extra = [(t["account_id"], t["region"], por_nombre[t["collector"]])
                 for t in leer_json(ruta_cob).get("tareas_extra", [])
                 if t["collector"] in por_nombre and (t["account_id"], t["region"], t["collector"]) not in ya]
        tareas += extra
        print(f"+{len(extra)} tareas desde cobertura.json (recursos fuera del mapa del billing)")
    cuentas = sorted({t[0] for t in tareas})
    print(f"{len(mods)} colectores · {len(cuentas)} cuentas · {len(tareas)} tareas")

    creds, errores = credenciales_por_cuenta(cfg, cuentas)
    tareas = [t for t in tareas if t[0] in creds]
    habilitadas, errores_reg = habilitadas_por_cuenta(creds)
    errores += errores_reg
    tareas, omitidas = filtrar_habilitadas(tareas, habilitadas)
    omitidas = sorted({(a, r) for a, r, _ in omitidas})
    if omitidas:
        print(f"Omito {len(omitidas)} cuenta×región no habilitadas: "
              + ", ".join(f"{a}/{r}" for a, r in omitidas))

    recursos, escaneado = [], []
    with ThreadPoolExecutor(max_workers=cfg["max_workers"]) as pool:
        futuros = {pool.submit(ejecutar, t, creds[t[0]]): t for t in tareas}
        for i, fut in enumerate(as_completed(futuros), 1):
            account_id, region, mod = futuros[fut]
            try:
                encontrados, segundos = fut.result()
                recursos.extend(encontrados)
                escaneado.append({"account_id": account_id, "region": region, "collector": mod.NAME,
                                  "count": len(encontrados), "seconds": segundos})
            except Exception as e:  # noqa: BLE001
                errores.append({"account_id": account_id, "region": region, "collector": mod.NAME,
                                "error": error_corto(e)})
            if i % 25 == 0 or i == len(futuros):
                print(f"  {i}/{len(futuros)} tareas · {len(recursos)} recursos · {len(errores)} errores")

    inventario = {
        "generated_at": ahora(),
        "accounts": {a: {"name": discovery["accounts"][a]["name"],
                         **({"enabled_regions": habilitadas[a]} if a in habilitadas else {})}
                     for a in discovery["accounts"]},
        "scanned": sorted(escaneado, key=lambda x: (x["account_id"], x["region"], x["collector"])),
        "skipped": [{"account_id": a, "region": r, "reason": "region_not_enabled"} for a, r in omitidas],
        "resources": recursos,
        "errors": errores,
    }
    escribir_json(salida / "inventory.json", inventario)
    print(f"→ {salida / 'inventory.json'}: {len(recursos)} recursos, {len(errores)} errores")


if __name__ == "__main__":
    main()
