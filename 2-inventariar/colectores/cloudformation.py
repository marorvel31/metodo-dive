"""Stacks y StackSets de CloudFormation.

Solo list_stacks / list_stack_sets: describe_stacks devuelve los Parameters del stack, que
pueden traer passwords si la plantilla no usa NoEcho. Tampoco se leen plantillas ni outputs.
"""

from common import recurso
from common.aws import client

NAME = "cloudformation"
TRIGGERS = ["AWS CloudFormation"]
ALWAYS = True  # CloudFormation no factura por stack: el billing nunca lo muestra
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag

_ESTADOS_VIVOS = [
    "CREATE_IN_PROGRESS", "CREATE_FAILED", "CREATE_COMPLETE", "ROLLBACK_IN_PROGRESS", "ROLLBACK_FAILED",
    "ROLLBACK_COMPLETE", "DELETE_IN_PROGRESS", "DELETE_FAILED", "UPDATE_IN_PROGRESS",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", "UPDATE_COMPLETE", "UPDATE_FAILED", "UPDATE_ROLLBACK_IN_PROGRESS",
    "UPDATE_ROLLBACK_FAILED", "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS", "UPDATE_ROLLBACK_COMPLETE",
    "REVIEW_IN_PROGRESS", "IMPORT_IN_PROGRESS", "IMPORT_COMPLETE", "IMPORT_ROLLBACK_IN_PROGRESS",
    "IMPORT_ROLLBACK_FAILED", "IMPORT_ROLLBACK_COMPLETE",
]


def collect(session, region, account_id):
    cfn = client(session, "cloudformation", region)
    recursos = []
    for pagina in cfn.get_paginator("list_stacks").paginate(StackStatusFilter=_ESTADOS_VIVOS):
        for s in pagina["StackSummaries"]:
            recursos.append(recurso(
                account_id, region, NAME, "stack", s["StackName"],
                arn=s["StackId"], name=s["StackName"], state=s["StackStatus"],
                attrs={
                    "created": s.get("CreationTime"),
                    "updated": s.get("LastUpdatedTime"),
                    "drift": (s.get("DriftInformation") or {}).get("StackDriftStatus"),
                    "nested": bool(s.get("ParentId")),
                },
                relations={"attached_to": [s["ParentId"]] if s.get("ParentId") else []},
            ))
    # Los StackSets solo existen en la cuenta administradora; en las demás la lista viene vacía.
    try:
        for pagina in cfn.get_paginator("list_stack_sets").paginate(Status="ACTIVE"):
            for s in pagina["Summaries"]:
                recursos.append(recurso(
                    account_id, region, NAME, "stack_set", s["StackSetName"],
                    arn=f"arn:aws:cloudformation:{region}:{account_id}:stackset/{s['StackSetName']}:{s['StackSetId'].split(':')[-1]}",
                    name=s["StackSetName"], state=s.get("Status"),
                    attrs={"permission_model": s.get("PermissionModel"),
                           "drift": s.get("DriftStatus"),
                           "auto_deployment": (s.get("AutoDeployment") or {}).get("Enabled")},
                ))
    except Exception:  # noqa: BLE001 — sin permisos de StackSets en cuentas miembro no es un error del inventario
        pass
    return recursos
