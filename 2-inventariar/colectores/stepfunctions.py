"""State machines de Step Functions. Solo list: describe traería la definición completa."""

from common import recurso
from common.aws import client

NAME = "stepfunctions"
TRIGGERS = ["AWS Step Functions"]
ALWAYS = False
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def collect(session, region, account_id):
    sfn = client(session, "stepfunctions", region)
    recursos = []
    for pagina in sfn.get_paginator("list_state_machines").paginate():
        for m in pagina["stateMachines"]:
            recursos.append(recurso(
                account_id, region, NAME, "state_machine", m["name"],
                arn=m.get("stateMachineArn"),
                name=m["name"],
                attrs={"type": m.get("type"), "created": m.get("creationDate")},
            ))
    return recursos
