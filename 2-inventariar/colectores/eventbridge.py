"""Reglas de EventBridge (programadas y por patrón) en todos los event buses.

De los targets solo se copia el ARN: el campo Input puede traer payloads con datos sensibles.
"""

from common import recurso
from common.aws import client

NAME = "eventbridge"
TRIGGERS = ["CloudWatch Events", "Amazon EventBridge"]
ALWAYS = True  # las reglas sobre eventos de AWS son gratis
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def _buses(events):
    buses, token = [], None
    while True:
        resp = events.list_event_buses(**({"NextToken": token} if token else {}))
        buses.extend(resp.get("EventBuses", []))
        token = resp.get("NextToken")
        if not token:
            return buses


def collect(session, region, account_id):
    events = client(session, "events", region)
    recursos = []
    for bus in _buses(events):
        if bus["Name"] != "default":
            recursos.append(recurso(account_id, region, NAME, "event_bus", bus["Name"],
                                    arn=bus["Arn"], name=bus["Name"]))
        for pagina in events.get_paginator("list_rules").paginate(EventBusName=bus["Name"]):
            for r in pagina["Rules"]:
                targets = [t["Arn"] for pt in events.get_paginator("list_targets_by_rule").paginate(
                    Rule=r["Name"], EventBusName=bus["Name"]) for t in pt["Targets"]]
                recursos.append(recurso(
                    account_id, region, NAME, "rule", r["Arn"],
                    arn=r["Arn"], name=r["Name"], state=r.get("State"),
                    attrs={
                        "event_bus": bus["Name"],
                        "schedule": r.get("ScheduleExpression"),
                        "event_pattern": bool(r.get("EventPattern")),
                        "managed_by": r.get("ManagedBy"),
                    },
                    relations={"target_ids": targets},
                ))
    return recursos
