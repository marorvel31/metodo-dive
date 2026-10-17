"""Elastic IPs. Una EIP sin asociar se cobra igual: es desperdicio."""

from common import recurso
from common.aws import client

NAME = "eip"
# Las IPv4 públicas (asociadas o no) se cobran bajo VPC desde 2024; antes, bajo "EC2 - Other".
TRIGGERS = ["EC2 - Other", "Amazon Virtual Private Cloud"]
ALWAYS = False
GLOBAL = False


def collect(session, region, account_id):
    ec2 = client(session, "ec2", region)
    recursos = []
    # describe_addresses no tiene paginador: devuelve todas en una sola respuesta.
    for a in ec2.describe_addresses().get("Addresses", []):
        asociada = bool(a.get("AssociationId"))
        destino = a.get("InstanceId") or a.get("NetworkInterfaceId")
        recursos.append(recurso(
            account_id, region, NAME, "elastic_ip", a.get("AllocationId") or a["PublicIp"],
            arn=f"arn:aws:ec2:{region}:{account_id}:elastic-ip/{a['AllocationId']}" if a.get("AllocationId") else None,
            state="associated" if asociada else "unassociated",
            tags=a.get("Tags"),
            attrs={
                "public_ip": a.get("PublicIp"),
                "private_ip": a.get("PrivateIpAddress"),
                "domain": a.get("Domain"),
                "associated": asociada,
                "instance_id": a.get("InstanceId"),
                "network_interface_id": a.get("NetworkInterfaceId"),
                "public_ipv4_pool": a.get("PublicIpv4Pool"),
            },
            relations={"attached_to": [destino] if destino else []},
        ))
    return recursos
