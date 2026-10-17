"""Entornos de Elastic Beanstalk. Nunca se llama describe_configuration_settings:
las option settings traen las variables de entorno de la aplicación."""

from common import recurso
from common.aws import client

NAME = "elasticbeanstalk"
# Beanstalk no factura aparte: se cobra como EC2 (y ELB) debajo.
TRIGGERS = ["Amazon Elastic Compute Cloud - Compute"]
ALWAYS = False
GLOBAL = False
TAGS = False  # el colector no lee tags: no cuenta para la regla de recursos sin tag


def collect(session, region, account_id):
    eb = client(session, "elasticbeanstalk", region)
    recursos = []
    for pagina in eb.get_paginator("describe_environments").paginate(IncludeDeleted=False):
        for e in pagina["Environments"]:
            recursos.append(recurso(
                account_id, region, NAME, "environment", e["EnvironmentId"],
                arn=e.get("EnvironmentArn"),
                name=e.get("EnvironmentName"),
                state=e.get("Status"),
                attrs={
                    "application": e.get("ApplicationName"),
                    "platform_arn": e.get("PlatformArn"),
                    "solution_stack": e.get("SolutionStackName"),
                    "tier": (e.get("Tier") or {}).get("Name"),
                    "health": e.get("Health"),
                    "health_status": e.get("HealthStatus"),
                    "cname": e.get("CNAME"),
                    "endpoint_url": e.get("EndpointURL"),
                    "version_label": e.get("VersionLabel"),
                    "date_updated": e.get("DateUpdated"),
                },
            ))
    return recursos
