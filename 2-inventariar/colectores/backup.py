"""AWS Backup: vaults (con conteo de recovery points) y planes con sus reglas."""

from common import recurso
from common.aws import client

NAME = "backup"
TRIGGERS = ["AWS Backup"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    bk = client(session, "backup", region)
    recursos = []

    for pagina in bk.get_paginator("list_backup_vaults").paginate():
        for v in pagina["BackupVaultList"]:
            # Solo el conteo: los recovery points no se listan uno por uno.
            recursos.append(recurso(
                account_id, region, NAME, "vault", v["BackupVaultName"],
                arn=v.get("BackupVaultArn"),
                name=v["BackupVaultName"],
                state=v.get("VaultState"),
                attrs={
                    "recovery_points": v.get("NumberOfRecoveryPoints"),
                    "vault_type": v.get("VaultType"),
                    "kms_key_id": v.get("EncryptionKeyArn"),
                    "locked": v.get("Locked"),
                    "min_retention_days": v.get("MinRetentionDays"),
                    "max_retention_days": v.get("MaxRetentionDays"),
                },
            ))

    for pagina in bk.get_paginator("list_backup_plans").paginate():
        for p in pagina["BackupPlansList"]:
            plan = bk.get_backup_plan(BackupPlanId=p["BackupPlanId"])["BackupPlan"]
            reglas = [{
                "name": r.get("RuleName"),
                "schedule": r.get("ScheduleExpression"),
                "vault": r.get("TargetBackupVaultName"),
                "delete_after_days": (r.get("Lifecycle") or {}).get("DeleteAfterDays"),
                "copies": len(r.get("CopyActions", [])),
            } for r in plan.get("Rules", [])]
            recursos.append(recurso(
                account_id, region, NAME, "plan", p["BackupPlanId"],
                arn=p.get("BackupPlanArn"),
                name=p.get("BackupPlanName"),
                attrs={
                    "version_id": p.get("VersionId"),
                    "last_execution": p.get("LastExecutionDate"),
                    "rules": reglas,
                },
                relations={"target_ids": sorted({r["vault"] for r in reglas if r["vault"]})},
            ))
    return recursos
