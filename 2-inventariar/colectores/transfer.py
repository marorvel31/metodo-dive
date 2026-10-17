"""Servidores de AWS Transfer Family (SFTP/FTPS/FTP/AS2).

No se listan usuarios ni sus llaves SSH: UserCount viene en list_servers.
"""

from common import recurso
from common.aws import client

NAME = "transfer"
TRIGGERS = ["AWS Transfer Family"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    tr = client(session, "transfer", region)
    recursos = []
    for pagina in tr.get_paginator("list_servers").paginate():
        for s in pagina.get("Servers", []):
            # describe_server trae protocolos y red; de él NO se copian Certificate, HostKeyFingerprint
            # ni IdentityProviderDetails (URL/función del proveedor de identidad).
            d = tr.describe_server(ServerId=s["ServerId"])["Server"]
            red = d.get("EndpointDetails") or {}
            recursos.append(recurso(
                account_id, region, NAME, "server", s["ServerId"], arn=d.get("Arn") or s.get("Arn"),
                state=d.get("State") or s.get("State"), tags=d.get("Tags"),
                attrs={
                    "protocols": d.get("Protocols", []),
                    "endpoint_type": d.get("EndpointType"),
                    "domain": d.get("Domain"),
                    "identity_provider": d.get("IdentityProviderType"),
                    "user_count": s.get("UserCount"),
                    "elastic_ip_allocations": red.get("AddressAllocationIds", []),
                    "logging_role": bool(d.get("LoggingRole")),
                },
                relations={
                    "vpc_id": red.get("VpcId"),
                    "subnet_ids": red.get("SubnetIds", []),
                    "security_group_ids": red.get("SecurityGroupIds", []),
                    "attached_to": [red["VpcEndpointId"]] if red.get("VpcEndpointId") else [],
                },
            ))
    return recursos
