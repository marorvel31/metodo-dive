"""Glue: jobs, crawlers y databases del catálogo. Nunca DefaultArguments (pueden traer secretos)."""

from common import recurso
from common.aws import client

NAME = "glue"
TRIGGERS = ["AWS Glue"]
ALWAYS = False
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def _arn(region, account_id, tipo, nombre):
    return f"arn:aws:glue:{region}:{account_id}:{tipo}/{nombre}"


def collect(session, region, account_id):
    glue = client(session, "glue", region)
    recursos = []
    for pagina in glue.get_paginator("get_jobs").paginate():
        for j in pagina["Jobs"]:
            recursos.append(recurso(
                account_id, region, NAME, "job", j["Name"],
                arn=_arn(region, account_id, "job", j["Name"]),
                name=j["Name"],
                attrs={
                    "job_type": (j.get("Command") or {}).get("Name"),
                    "glue_version": j.get("GlueVersion"),
                    "worker_type": j.get("WorkerType"),
                    "workers": j.get("NumberOfWorkers"),
                    "max_capacity": j.get("MaxCapacity"),
                    "role": j.get("Role"),
                    "last_modified": j.get("LastModifiedOn"),
                },
            ))
    for pagina in glue.get_paginator("get_crawlers").paginate():
        for c in pagina["Crawlers"]:
            recursos.append(recurso(
                account_id, region, NAME, "crawler", c["Name"],
                arn=_arn(region, account_id, "crawler", c["Name"]),
                name=c["Name"],
                state=c.get("State"),
                attrs={
                    "database": c.get("DatabaseName"),
                    "schedule": (c.get("Schedule") or {}).get("ScheduleExpression"),
                    "last_crawl_status": (c.get("LastCrawl") or {}).get("Status"),
                },
            ))
    for pagina in glue.get_paginator("get_databases").paginate():
        for d in pagina["DatabaseList"]:
            recursos.append(recurso(
                account_id, region, NAME, "database", d["Name"],
                arn=_arn(region, account_id, "database", d["Name"]),
                name=d["Name"],
                attrs={"created": d.get("CreateTime")},
            ))
    return recursos
