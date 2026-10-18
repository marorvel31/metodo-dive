"""Cobertura: ¿el inventario vio todo? Compara inventory.json contra un oráculo independiente.

El oráculo es la Resource Groups Tagging API (tag:GetResources), que lista por ARN los recursos
de casi todos los servicios sin pasar por nuestros colectores. Límite conocido: solo devuelve
recursos que tienen o tuvieron tags, así que un 100% aquí no prueba que no falte nada sin tags.

Escribe cobertura.json y cobertura.md junto a inventory.json.

Uso: python 3-validar/cobertura.py [--inventory salida/<corrida>/inventory.json]
"""

import argparse
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import ahora, cargar_config, corrida_actual, error_corto, escribir_json, heredar_run, leer_json  # noqa: E402
from common.aws import client, sesion_cuenta  # noqa: E402


def tipo_de_arn(arn):
    """arn:aws:ec2:us-east-1:123:security-group/sg-1 → 'ec2:security-group'."""
    partes = arn.split(":", 5)
    if len(partes) < 6:
        return "desconocido"
    servicio, recurso = partes[2], partes[5]
    if servicio in ("secretsmanager", "logs"):  # secret:<nombre-con-/>, log-group:<ruta>
        return f"{servicio}:{recurso.split(':')[0]}"
    subtipo = recurso.split("/")[0] if "/" in recurso else (recurso.split(":")[0] if ":" in recurso else "")
    return f"{servicio}:{subtipo}" if subtipo and subtipo != recurso else servicio


# Tipo de ARN → colector que lo cubre. Sirve para que la fase 2 vuelva a escanear lo que el billing
# no mostró (típicamente recursos dentro del free tier: el mapa del billing no los ve).
_EC2 = {"instance": "ec2", "volume": "ebs", "snapshot": "ebs", "elastic-ip": "eip", "image": "ami",
        "transit-gateway": "transitgateway", "transit-gateway-attachment": "transitgateway",
        "transit-gateway-route-table": "transitgateway", "vpn-connection": "vpn", "vpn-gateway": "vpn",
        "customer-gateway": "vpn", "client-vpn-endpoint": "vpn", "vpc-flow-log": "flowlogs",
        "vpc": "vpc", "subnet": "vpc", "security-group": "vpc", "route-table": "vpc", "internet-gateway": "vpc",
        "natgateway": "vpc", "vpc-endpoint": "vpc", "vpc-peering-connection": "vpc"}
_SERVICIO = {"lambda": "lambda", "rds": "rds", "elasticloadbalancing": "elb", "ecs": "ecs", "ecr": "ecr",
             "dynamodb": "dynamodb", "sqs": "sqs", "sns": "sns", "secretsmanager": "secretsmanager", "kms": "kms",
             "elasticache": "elasticache", "es": "opensearch", "kinesis": "kinesis", "firehose": "kinesis",
             "elasticfilesystem": "efs", "states": "stepfunctions", "glue": "glue", "dms": "dms",
             "backup": "backup", "apigateway": "apigateway", "transfer": "transfer", "wafv2": "waf",
             "acm": "acm", "autoscaling": "autoscaling", "elasticbeanstalk": "elasticbeanstalk", "eks": "eks",
             "lightsail": "lightsail", "s3": "s3", "cloudfront": "cloudfront", "route53": "route53",
             "cloudwatch": "cloudwatch", "logs": "logs", "cloudformation": "cloudformation", "ssm": "ssm",
             "events": "eventbridge", "application-autoscaling": "appautoscaling"}
# Sub-recursos o efímeros: el colector los resume a propósito (conteo, último estado) o no son arquitectura.
FUERA_DE_ALCANCE = {"backup:recovery-point", "ssm:session", "ssm:automation-execution", "ecs:task",
                    "ecs:task-definition", "rds:snapshot", "rds:cluster-snapshot", "elasticache:snapshot",
                    "lightsail:InstanceSnapshot", "ec2:network-interface", "elasticloadbalancing:listener",
                    "elasticloadbalancing:listener-rule", "payments:payment-instrument",
                    # configuración que cuelga de un recurso ya inventariado (no es un nodo de arquitectura)
                    "rds:subgrp", "rds:pg", "rds:cluster-pg", "rds:og", "elasticache:parametergroup",
                    "elasticache:subnetgroup", "elasticbeanstalk:configurationtemplate", "elasticbeanstalk:application",
                    "ec2:security-group-rule", "ec2:network-insights-path", "ec2:network-insights-access-scope",
                    "ec2:network-insights-analysis", "lambda:event-source-mapping", "lightsail:KeyPair"}


def colector_para(tipo):
    servicio, _, subtipo = tipo.partition(":")
    if servicio == "ec2":
        return _EC2.get(subtipo)
    return _SERVICIO.get(servicio)


def arns_del_oraculo(cfg, account_id, region):
    tagging = client(sesion_cuenta(cfg, account_id), "resourcegroupstaggingapi", region)
    arns = []
    for pagina in tagging.get_paginator("get_resources").paginate(ResourcesPerPage=100):
        arns.extend(m["ResourceARN"] for m in pagina["ResourceTagMappingList"])
    return arns


def cubierto(arn, arns_inv, ids_inv):
    if arn in arns_inv:
        return True
    # Muchos colectores guardan el id corto (sg-..., nombre del bucket...): basta con que sea el último segmento.
    ultimo = arn.replace(":", "/").split("/")[-1]
    return ultimo in ids_inv or arn.split(":::")[-1] in ids_inv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--inventory")
    args = parser.parse_args()
    cfg = cargar_config(args.config)
    ruta_inv = Path(args.inventory) if args.inventory else corrida_actual(cfg) / "inventory.json"
    inventory = leer_json(ruta_inv)
    discovery = leer_json(ruta_inv.parent / "discovery.json")
    heredar_run(inventory, cfg, "cobertura", {})  # aborta si la corrida es de otra organización

    arns_inv = {r["arn"] for r in inventory["resources"] if r.get("arn")}
    ids_inv = {str(r["id"]) for r in inventory["resources"]}

    # Mismas regiones que se inventariaron (las que tienen costo) + us-east-1 para lo global.
    habilitadas = {a: c["enabled_regions"] for a, c in inventory.get("accounts", {}).items()
                   if c.get("enabled_regions")}
    tareas = sorted({(a, r if r != "global" else "us-east-1")
                     for a, c in discovery["accounts"].items() for r in c["regions"]
                     if r == "global" or a not in habilitadas or r in habilitadas[a]})
    faltantes, total, errores = defaultdict(list), Counter(), []
    with ThreadPoolExecutor(max_workers=cfg["max_workers"]) as pool:
        futuros = {pool.submit(arns_del_oraculo, cfg, a, r): (a, r) for a, r in tareas}
        for fut in as_completed(futuros):
            account_id, region = futuros[fut]
            try:
                for arn in fut.result():
                    tipo = tipo_de_arn(arn)
                    total[tipo] += 1
                    if not cubierto(arn, arns_inv, ids_inv):
                        faltantes[tipo].append({"account_id": account_id, "region": region, "arn": arn})
            except Exception as e:  # noqa: BLE001
                errores.append({"account_id": account_id, "region": region, "error": error_corto(e)})

    # Tareas extra para la fase 2: colector × cuenta × región donde el oráculo vio algo que no inventariamos.
    tareas_extra = sorted({(f["account_id"], f["region"], colector_para(t))
                           for t, lista in faltantes.items() if t not in FUERA_DE_ALCANCE and colector_para(t)
                           for f in lista})
    fuera = {t: len(v) for t, v in faltantes.items() if t in FUERA_DE_ALCANCE}
    faltantes = {t: v for t, v in faltantes.items() if t not in FUERA_DE_ALCANCE}
    for t in fuera:
        total.pop(t, None)
    n_total, n_falta = sum(total.values()), sum(len(v) for v in faltantes.values())
    resumen = sorted(({"tipo": t, "oraculo": total[t], "faltan": len(faltantes.get(t, []))} for t in total),
                     key=lambda x: (-x["faltan"], x["tipo"]))
    escribir_json(ruta_inv.parent / "cobertura.json", {
        "generated_at": ahora(), "oraculo": "resourcegroupstaggingapi:GetResources",
        "total_oraculo": n_total, "faltan": n_falta,
        "cobertura_pct": round(100 * (n_total - n_falta) / n_total, 1) if n_total else None,
        "por_tipo": resumen, "faltantes": faltantes, "fuera_de_alcance": fuera,
        "tareas_extra": [{"account_id": a, "region": r, "collector": c} for a, r, c in tareas_extra],
        "errores": errores,
    })

    lineas = [f"# Cobertura del inventario\n",
              f"Oráculo: Tagging API · {n_total} recursos con tags · **{n_falta} no están en inventory.json** "
              f"({100 * (n_total - n_falta) / n_total:.1f}% de cobertura)\n" if n_total else "Oráculo vacío\n",
              "| Tipo | En el oráculo | Faltan | ¿Qué hacer? |", "|---|---:|---:|---|"]
    for x in resumen:
        if x["faltan"]:
            if colector_para(x["tipo"]):
                accion = f"re-escanear con `{colector_para(x['tipo'])}` (fuera del mapa del billing)"
            else:
                accion = "escribir colector" if x["faltan"] == x["oraculo"] else "revisar colector (cubre parcial)"
            lineas.append(f"| `{x['tipo']}` | {x['oraculo']} | {x['faltan']} | {accion} |")
    (ruta_inv.parent / "cobertura.md").write_text("\n".join(lineas) + "\n")
    print(f"{len(tareas)} cuenta×región · oráculo {n_total} recursos en alcance · faltan {n_falta} · "
          f"errores {len(errores)} · {len(tareas_extra)} tareas extra para la fase 2 "
          f"· fuera de alcance: {sum(fuera.values())}")
    for x in resumen[:25]:
        if x["faltan"]:
            print(f"  {x['faltan']:5}/{x['oraculo']:<5} {x['tipo']}")


if __name__ == "__main__":
    main()
