"""Pruebas de los colectores de red y edge con botocore Stubber: ninguna llamada real a AWS.

Uso: .venv/bin/python -m pytest 2-inventariar/tests/test_red.py
"""

import datetime as dt
import importlib
import json
import sys
import unittest
from pathlib import Path

import boto3
from botocore.stub import Stubber

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent.parent))
sys.path.insert(0, str(AQUI.parent))

CUENTA, OTRA, REGION = "111122223333", "444455556666", "us-east-1"
FECHA = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
CLAVES = {"account_id", "region", "service", "type", "id", "arn", "name", "state", "tags", "attrs", "relations"}
RELACIONES = {"vpc_id", "subnet_ids", "security_group_ids", "target_ids", "attached_to"}
SERVICIOS_CE = set(json.loads((AQUI.parent / "servicios_cost_explorer.json").read_text()))
SERVICIOS_CE.add("Amazon CloudFront")  # nombre real de CE; el JSON de ejemplo no lo trae
MODULOS = ["vpc", "transitgateway", "vpn", "elb", "cloudfront", "route53", "apigateway", "transfer",
           "waf", "waf_cloudfront", "acm", "globalaccelerator"]


class SesionFalsa:
    """Imita boto3.Session.client con clientes stubbeados; verifica la región pedida."""

    def __init__(self):
        self._base = boto3.Session(aws_access_key_id="x", aws_secret_access_key="x", region_name=REGION)
        self.clientes, self.stubs, self.regiones = {}, {}, {}

    def stub(self, servicio, region=REGION):
        if servicio not in self.stubs:
            c = self._base.client(servicio, region_name=region)
            self.clientes[servicio], self.regiones[servicio] = c, region
            self.stubs[servicio] = Stubber(c)
            self.stubs[servicio].activate()
        return self.stubs[servicio]

    def client(self, servicio, region_name=None, config=None):
        assert region_name == self.regiones[servicio], f"{servicio}: región {region_name}"
        return self.clientes[servicio]

    def verificar(self):
        for s in self.stubs.values():
            s.assert_no_pending_responses()


class Base(unittest.TestCase):
    def setUp(self):
        self.s = SesionFalsa()

    def correr(self, modulo):
        mod = importlib.import_module(f"colectores.{modulo}")
        recursos = mod.collect(self.s, REGION, CUENTA)
        self.s.verificar()
        for r in recursos:
            self.assertEqual(set(r), CLAVES)
            self.assertEqual(r["service"], mod.NAME)
            self.assertEqual(r["account_id"], CUENTA)
            self.assertTrue(set(r["relations"]) <= RELACIONES, r["relations"])
            json.dumps(r, default=str)
        return recursos

    @staticmethod
    def por_id(recursos):
        return {r["id"]: r for r in recursos}


class TestInterfaz(unittest.TestCase):
    def test_constantes(self):
        for nombre in MODULOS:
            mod = importlib.import_module(f"colectores.{nombre}")
            self.assertEqual(mod.NAME, nombre)
            self.assertTrue(mod.TRIGGERS)
            self.assertTrue(set(mod.TRIGGERS) <= SERVICIOS_CE, (nombre, set(mod.TRIGGERS) - SERVICIOS_CE))
            self.assertIsInstance(mod.ALWAYS, bool)
            self.assertIsInstance(mod.GLOBAL, bool)
        globales = {n for n in MODULOS if importlib.import_module(f"colectores.{n}").GLOBAL}
        self.assertEqual(globales, {"cloudfront", "route53", "waf_cloudfront", "globalaccelerator"})
        self.assertTrue(importlib.import_module("colectores.vpc").ALWAYS)


class TestVpc(Base):
    def test_red_completa(self):
        ec2 = self.s.stub("ec2")
        ec2.add_response("describe_route_tables", {"RouteTables": [
            {"RouteTableId": "rtb-pub", "VpcId": "vpc-1", "OwnerId": CUENTA,
             "Associations": [{"SubnetId": "subnet-pub", "Main": False}],
             "Routes": [{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                        {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}]},
            {"RouteTableId": "rtb-main", "VpcId": "vpc-1", "OwnerId": CUENTA,
             "Associations": [{"Main": True}],
             "Routes": [{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local", "State": "active"},
                        {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"},
                        {"DestinationCidrBlock": "172.16.0.0/12", "TransitGatewayId": "tgw-1", "State": "active"},
                        {"DestinationPrefixListId": "pl-s3", "GatewayId": "vpce-s3", "State": "active"},
                        {"DestinationCidrBlock": "192.168.0.0/16", "VpcPeeringConnectionId": "pcx-1",
                         "State": "blackhole"}]},
        ]})
        ec2.add_response("describe_vpcs", {"Vpcs": [
            {"VpcId": "vpc-1", "OwnerId": CUENTA, "CidrBlock": "10.0.0.0/16", "IsDefault": False, "State": "available",
             "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}], "Tags": [{"Key": "Name", "Value": "prod"}]},
            {"VpcId": "vpc-def", "OwnerId": CUENTA, "CidrBlock": "172.31.0.0/16", "IsDefault": True, "State": "available"},
            {"VpcId": "vpc-compartida", "OwnerId": OTRA, "CidrBlock": "10.9.0.0/16", "IsDefault": False},
        ]})
        ec2.add_response("describe_subnets", {"Subnets": [
            {"SubnetId": "subnet-pub", "VpcId": "vpc-1", "OwnerId": CUENTA, "CidrBlock": "10.0.1.0/24",
             "AvailabilityZone": "us-east-1a", "SubnetArn": "arn:aws:ec2:us-east-1:111122223333:subnet/subnet-pub"},
            {"SubnetId": "subnet-priv", "VpcId": "vpc-1", "OwnerId": CUENTA, "CidrBlock": "10.0.2.0/24",
             "AvailabilityZone": "us-east-1b"},
            {"SubnetId": "subnet-ajena", "VpcId": "vpc-compartida", "OwnerId": OTRA, "CidrBlock": "10.9.1.0/24"},
        ]})
        ec2.add_response("describe_internet_gateways", {"InternetGateways": [
            {"InternetGatewayId": "igw-1", "OwnerId": CUENTA, "Attachments": [{"VpcId": "vpc-1", "State": "available"}]}]})
        ec2.add_response("describe_nat_gateways", {"NatGateways": [
            {"NatGatewayId": "nat-1", "VpcId": "vpc-1", "SubnetId": "subnet-pub", "State": "available",
             "ConnectivityType": "public",
             "NatGatewayAddresses": [{"PublicIp": "44.0.0.1", "PrivateIp": "10.0.1.5", "AllocationId": "eipalloc-1",
                                      "NetworkInterfaceId": "eni-nat"}]}]})
        ec2.add_response("describe_vpc_endpoints", {"VpcEndpoints": [
            {"VpcEndpointId": "vpce-s3", "VpcEndpointType": "Gateway", "VpcId": "vpc-1", "OwnerId": CUENTA,
             "ServiceName": "com.amazonaws.us-east-1.s3", "State": "available", "RouteTableIds": ["rtb-main"],
             "PolicyDocument": "{\"Statement\": []}"},
            {"VpcEndpointId": "vpce-ssm", "VpcEndpointType": "Interface", "VpcId": "vpc-1", "OwnerId": CUENTA,
             "ServiceName": "com.amazonaws.us-east-1.ssm", "State": "available", "SubnetIds": ["subnet-priv"],
             "Groups": [{"GroupId": "sg-app", "GroupName": "app"}]}]})
        ec2.add_response("describe_vpc_peering_connections", {"VpcPeeringConnections": [
            {"VpcPeeringConnectionId": "pcx-1", "Status": {"Code": "active"},
             "RequesterVpcInfo": {"OwnerId": CUENTA, "VpcId": "vpc-1", "Region": "us-east-1", "CidrBlock": "10.0.0.0/16"},
             "AccepterVpcInfo": {"OwnerId": OTRA, "VpcId": "vpc-9", "Region": "us-east-2", "CidrBlock": "192.168.0.0/16"}}]})
        ec2.add_response("describe_security_groups", {"SecurityGroups": [
            {"GroupId": "sg-web", "GroupName": "web", "OwnerId": CUENTA, "VpcId": "vpc-1", "Description": "web",
             "IpPermissions": [
                 {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                 {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}],
             "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
            {"GroupId": "sg-app", "GroupName": "app", "OwnerId": CUENTA, "VpcId": "vpc-1", "Description": "app",
             "Tags": [{"Key": "Name", "Value": "app-prod"}],
             "IpPermissions": [
                 {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8090, "UserIdGroupPairs": [{"GroupId": "sg-web",
                                                                                               "UserId": CUENTA}]},
                 {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "10.0.0.0/8"}], "PrefixListIds": [{"PrefixListId": "pl-1"}]}]},
            {"GroupId": "sg-def", "GroupName": "default", "OwnerId": CUENTA, "VpcId": "vpc-1", "Description": "default"},
        ]})

        r = self.por_id(self.correr("vpc"))
        self.assertNotIn("vpc-compartida", r)
        self.assertNotIn("subnet-ajena", r)
        self.assertEqual(r["vpc-1"]["name"], "prod")
        self.assertFalse(r["vpc-1"]["attrs"]["is_default"])
        self.assertTrue(r["vpc-def"]["attrs"]["is_default"])

        self.assertTrue(r["subnet-pub"]["attrs"]["public"])
        self.assertFalse(r["subnet-priv"]["attrs"]["public"])  # usa la main, que sale por NAT
        self.assertEqual(r["subnet-priv"]["attrs"]["route_table_id"], "rtb-main")
        self.assertEqual(r["subnet-pub"]["relations"], {"vpc_id": "vpc-1", "attached_to": ["rtb-pub"]})

        main = r["rtb-main"]
        self.assertTrue(main["attrs"]["main"])
        tipos = {x["dest"]: (x["target_type"], x["target"]) for x in main["attrs"]["routes"]}
        self.assertEqual(tipos["0.0.0.0/0"], ("nat", "nat-1"))
        self.assertEqual(tipos["172.16.0.0/12"], ("tgw", "tgw-1"))
        self.assertEqual(tipos["pl-s3"], ("vpce", "vpce-s3"))
        self.assertEqual(tipos["192.168.0.0/16"], ("peering", "pcx-1"))
        self.assertEqual(tipos["10.0.0.0/16"], ("local", "local"))
        self.assertEqual(main["relations"]["target_ids"], ["nat-1", "pcx-1", "tgw-1", "vpce-s3"])
        self.assertEqual(r["rtb-pub"]["relations"]["subnet_ids"], ["subnet-pub"])

        self.assertEqual(r["igw-1"]["relations"], {"vpc_id": "vpc-1", "attached_to": ["vpc-1"]})
        nat = r["nat-1"]
        self.assertEqual(nat["attrs"]["public_ips"], ["44.0.0.1"])
        self.assertEqual(nat["relations"]["subnet_ids"], ["subnet-pub"])
        self.assertEqual(r["vpce-s3"]["relations"]["attached_to"], ["rtb-main"])
        self.assertNotIn("policy", json.dumps(r["vpce-s3"]).lower())
        self.assertEqual(r["vpce-ssm"]["relations"]["security_group_ids"], ["sg-app"])

        pcx = r["pcx-1"]
        self.assertEqual(pcx["attrs"]["accepter"]["account_id"], OTRA)
        self.assertEqual(pcx["attrs"]["accepter"]["cidr"], "192.168.0.0/16")
        self.assertTrue(pcx["attrs"]["cross_account"])
        self.assertEqual(pcx["relations"]["vpc_id"], "vpc-1")
        self.assertEqual(pcx["relations"]["target_ids"], ["vpc-1", "vpc-9"])

        web, app = r["sg-web"], r["sg-app"]
        self.assertTrue(web["attrs"]["open_to_world"])
        self.assertEqual(web["attrs"]["open_to_world_ports"], ["tcp/22", "tcp/443"])
        self.assertFalse(app["attrs"]["open_to_world"])
        self.assertEqual(app["name"], "app-prod")
        self.assertIn({"protocol": "tcp", "ports": "8080-8090", "source": "sg-web"}, app["attrs"]["ingress"])
        self.assertIn({"protocol": "all", "ports": "all", "source": "pl-1"}, app["attrs"]["ingress"])
        self.assertEqual(app["attrs"]["ingress_source_sgs"], ["sg-web"])
        self.assertTrue(r["sg-def"]["attrs"]["is_default"])
        self.assertFalse(web["attrs"]["is_default"])
        self.assertEqual(web["relations"], {"vpc_id": "vpc-1"})

    def test_paginacion(self):
        ec2 = self.s.stub("ec2")
        ec2.add_response("describe_route_tables", {"RouteTables": []})
        ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-a", "OwnerId": CUENTA}], "NextToken": "t"})
        ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-b", "OwnerId": CUENTA}]}, {"NextToken": "t"})
        for op, clave in [("describe_subnets", "Subnets"), ("describe_internet_gateways", "InternetGateways"),
                          ("describe_nat_gateways", "NatGateways"), ("describe_vpc_endpoints", "VpcEndpoints"),
                          ("describe_vpc_peering_connections", "VpcPeeringConnections"),
                          ("describe_security_groups", "SecurityGroups")]:
            ec2.add_response(op, {clave: []})
        r = self.por_id(self.correr("vpc"))
        self.assertEqual(set(r), {"vpc-a", "vpc-b"})
        self.assertEqual(r["vpc-a"]["relations"], {})


class TestTransitGateway(Base):
    def test_tgw(self):
        ec2 = self.s.stub("ec2")
        ec2.add_response("describe_transit_gateways", {"TransitGateways": [
            {"TransitGatewayId": "tgw-1", "OwnerId": CUENTA, "State": "available",
             "TransitGatewayArn": "arn:aws:ec2:us-east-1:111122223333:transit-gateway/tgw-1",
             "Options": {"AmazonSideAsn": 64512, "AssociationDefaultRouteTableId": "tgw-rtb-1"}},
            {"TransitGatewayId": "tgw-ajeno", "OwnerId": OTRA, "State": "available"}]})
        ec2.add_response("describe_transit_gateway_vpc_attachments", {"TransitGatewayVpcAttachments": [
            {"TransitGatewayAttachmentId": "tgw-attach-vpc", "SubnetIds": ["subnet-a", "subnet-b"]}]})
        ec2.add_response("describe_transit_gateway_attachments", {"TransitGatewayAttachments": [
            {"TransitGatewayAttachmentId": "tgw-attach-vpc", "TransitGatewayId": "tgw-1",
             "TransitGatewayOwnerId": CUENTA, "ResourceOwnerId": OTRA, "ResourceType": "vpc", "ResourceId": "vpc-9",
             "State": "available", "Association": {"TransitGatewayRouteTableId": "tgw-rtb-1", "State": "associated"}},
            {"TransitGatewayAttachmentId": "tgw-attach-vpn", "TransitGatewayId": "tgw-1",
             "TransitGatewayOwnerId": CUENTA, "ResourceOwnerId": CUENTA, "ResourceType": "vpn",
             "ResourceId": "vpn-1", "State": "available"}]})
        ec2.add_response("describe_transit_gateway_route_tables", {"TransitGatewayRouteTables": [
            {"TransitGatewayRouteTableId": "tgw-rtb-1", "TransitGatewayId": "tgw-1", "State": "available",
             "DefaultAssociationRouteTable": True, "DefaultPropagationRouteTable": True}]})
        ec2.add_response("get_transit_gateway_route_table_associations",
                         {"Associations": [{"TransitGatewayAttachmentId": "tgw-attach-vpc"}]},
                         {"TransitGatewayRouteTableId": "tgw-rtb-1"})
        ec2.add_response("get_transit_gateway_route_table_propagations",
                         {"TransitGatewayRouteTablePropagations": [{"TransitGatewayAttachmentId": "tgw-attach-vpn"}]},
                         {"TransitGatewayRouteTableId": "tgw-rtb-1"})
        ec2.add_response("search_transit_gateway_routes", {"Routes": [
            {"DestinationCidrBlock": "10.9.0.0/16", "Type": "propagated", "State": "active",
             "TransitGatewayAttachments": [{"TransitGatewayAttachmentId": "tgw-attach-vpc", "ResourceId": "vpc-9",
                                            "ResourceType": "vpc"}]},
            {"DestinationCidrBlock": "0.0.0.0/0", "Type": "static", "State": "blackhole"}],
            "AdditionalRoutesAvailable": False},
            {"TransitGatewayRouteTableId": "tgw-rtb-1",
             "Filters": [{"Name": "state", "Values": ["active", "blackhole"]}], "MaxResults": 200})

        r = self.por_id(self.correr("transitgateway"))
        self.assertNotIn("tgw-ajeno", r)
        self.assertEqual(r["tgw-1"]["attrs"]["amazon_side_asn"], 64512)
        vpc = r["tgw-attach-vpc"]
        self.assertEqual(vpc["attrs"]["resource_owner_id"], OTRA)
        self.assertEqual(vpc["attrs"]["seen_from"], "tgw_owner")
        self.assertEqual(vpc["relations"], {"vpc_id": "vpc-9", "subnet_ids": ["subnet-a", "subnet-b"],
                                            "attached_to": ["tgw-1"], "target_ids": ["vpc-9"]})
        self.assertNotIn("vpc_id", r["tgw-attach-vpn"]["relations"])
        rt = r["tgw-rtb-1"]
        self.assertEqual(rt["relations"], {"attached_to": ["tgw-1"], "target_ids": ["tgw-attach-vpc"]})
        self.assertEqual(rt["attrs"]["propagating_attachments"], ["tgw-attach-vpn"])
        self.assertEqual(rt["attrs"]["routes"][0]["targets"][0]["resource_id"], "vpc-9")
        self.assertEqual(rt["attrs"]["routes"][1]["state"], "blackhole")


class TestVpn(Base):
    def test_vpn_sin_secretos(self):
        ec2 = self.s.stub("ec2")
        ec2.add_response("describe_customer_gateways", {"CustomerGateways": [
            {"CustomerGatewayId": "cgw-1", "IpAddress": "200.1.2.3", "BgpAsn": "65000", "State": "available",
             "Type": "ipsec.1", "DeviceName": "fortigate-sede", "CertificateArn": "arn:aws:acm-pca:x"}]})
        ec2.add_response("describe_vpn_connections", {"VpnConnections": [
            {"VpnConnectionId": "vpn-1", "State": "available", "Type": "ipsec.1", "CustomerGatewayId": "cgw-1",
             "TransitGatewayId": "tgw-1", "Category": "VPN",
             "CustomerGatewayConfiguration": "<xml><pre_shared_key>SECRETO-XML</pre_shared_key></xml>",
             "Options": {"StaticRoutesOnly": False, "TunnelOptions": [
                 {"OutsideIpAddress": "3.3.3.3", "PreSharedKey": "SECRETO-PSK"}]},
             "VgwTelemetry": [{"OutsideIpAddress": "3.3.3.3", "Status": "UP", "AcceptedRouteCount": 4,
                               "LastStatusChange": FECHA},
                              {"OutsideIpAddress": "3.3.3.4", "Status": "DOWN", "AcceptedRouteCount": 0}]},
            {"VpnConnectionId": "vpn-2", "State": "available", "Type": "ipsec.1", "CustomerGatewayId": "cgw-1",
             "VpnGatewayId": "vgw-1", "Options": {"StaticRoutesOnly": True},
             "Routes": [{"DestinationCidrBlock": "192.168.10.0/24", "State": "available"}]}]})
        ec2.add_response("describe_vpn_gateways", {"VpnGateways": [
            {"VpnGatewayId": "vgw-1", "State": "available", "AmazonSideAsn": 64512,
             "VpcAttachments": [{"VpcId": "vpc-1", "State": "attached"}, {"VpcId": "vpc-old", "State": "detached"}]}]})
        ec2.add_response("describe_client_vpn_endpoints", {"ClientVpnEndpoints": [
            {"ClientVpnEndpointId": "cvpn-endpoint-1", "Status": {"Code": "available"}, "ClientCidrBlock": "10.200.0.0/22",
             "VpcId": "vpc-1", "SecurityGroupIds": ["sg-vpn"], "SplitTunnel": True, "TransportProtocol": "udp",
             "ServerCertificateArn": "arn:aws:acm:us-east-1:111122223333:certificate/srv",
             "AuthenticationOptions": [{"Type": "certificate-authentication", "MutualAuthentication": {
                 "ClientRootCertificateChain": "arn:aws:acm:us-east-1:111122223333:certificate/root"}}]}]})
        ec2.add_response("describe_client_vpn_target_networks", {"ClientVpnTargetNetworks": [
            {"TargetNetworkId": "subnet-a"}, {"TargetNetworkId": "subnet-b"}]}, {"ClientVpnEndpointId": "cvpn-endpoint-1"})

        recursos = self.correr("vpn")
        texto = json.dumps(recursos, default=str)
        for secreto in ("SECRETO", "fortigate-sede", "acm-pca", "certificate/srv", "certificate/root"):
            self.assertNotIn(secreto, texto)
        r = self.por_id(recursos)
        v1 = r["vpn-1"]
        self.assertEqual(v1["attrs"]["routing"], "bgp")
        self.assertEqual(v1["attrs"]["customer_gateway_ip"], "200.1.2.3")
        self.assertEqual(v1["attrs"]["tunnels_up"], 1)
        self.assertEqual([t["status"] for t in v1["attrs"]["tunnels"]], ["UP", "DOWN"])
        self.assertEqual(v1["relations"], {"attached_to": ["cgw-1", "tgw-1"]})
        self.assertEqual(r["vpn-2"]["attrs"]["routing"], "static")
        self.assertEqual(r["vpn-2"]["attrs"]["static_routes"], ["192.168.10.0/24"])
        self.assertEqual(r["vpn-2"]["relations"]["attached_to"], ["cgw-1", "vgw-1"])
        self.assertEqual(r["vgw-1"]["relations"], {"vpc_id": "vpc-1", "attached_to": ["vpc-1"]})
        self.assertEqual(r["cgw-1"]["attrs"]["ip_address"], "200.1.2.3")
        cv = r["cvpn-endpoint-1"]
        self.assertEqual(cv["attrs"]["client_cidr"], "10.200.0.0/22")
        self.assertEqual(cv["state"], "available")
        self.assertEqual(cv["relations"], {"vpc_id": "vpc-1", "subnet_ids": ["subnet-a", "subnet-b"],
                                           "security_group_ids": ["sg-vpn"]})


ALB = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/api/abc"
TG = "arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/api/def"
CERT = "arn:aws:acm:us-east-1:111122223333:certificate/c1"


class TestElb(Base):
    def test_alb_tg_clasico(self):
        v2, v1 = self.s.stub("elbv2"), self.s.stub("elb")
        v2.add_response("describe_load_balancers", {"LoadBalancers": [
            {"LoadBalancerArn": ALB, "LoadBalancerName": "api", "Type": "application", "Scheme": "internet-facing",
             "DNSName": "api-123.us-east-1.elb.amazonaws.com", "VpcId": "vpc-1", "State": {"Code": "active"},
             "AvailabilityZones": [{"ZoneName": "us-east-1a", "SubnetId": "subnet-a"},
                                   {"ZoneName": "us-east-1b", "SubnetId": "subnet-b"}],
             "SecurityGroups": ["sg-web"]}]})
        v2.add_response("describe_target_groups", {"TargetGroups": [
            {"TargetGroupArn": TG, "TargetGroupName": "api", "Protocol": "HTTP", "Port": 8080, "VpcId": "vpc-1",
             "TargetType": "instance", "HealthCheckPath": "/health", "LoadBalancerArns": [ALB]}]})
        v2.add_response("describe_tags", {"TagDescriptions": [
            {"ResourceArn": ALB, "Tags": [{"Key": "Environment", "Value": "prod"}]}, {"ResourceArn": TG}]},
            {"ResourceArns": [ALB, TG]})
        v2.add_response("describe_listeners", {"Listeners": [
            {"Port": 443, "Protocol": "HTTPS", "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
             "Certificates": [{"CertificateArn": CERT}],
             "DefaultActions": [{"Type": "forward", "TargetGroupArn": TG}]},
            {"Port": 80, "Protocol": "HTTP", "DefaultActions": [{"Type": "redirect"}]}]}, {"LoadBalancerArn": ALB})
        v2.add_response("describe_target_health", {"TargetHealthDescriptions": [
            {"Target": {"Id": "i-1", "Port": 8080}, "TargetHealth": {"State": "healthy"}},
            {"Target": {"Id": "i-2", "Port": 8080}, "TargetHealth": {"State": "unhealthy"}}]}, {"TargetGroupArn": TG})
        v1.add_response("describe_load_balancers", {"LoadBalancerDescriptions": [
            {"LoadBalancerName": "legacy", "DNSName": "legacy.elb.amazonaws.com", "Scheme": "internal",
             "VPCId": "vpc-1", "Subnets": ["subnet-a"], "SecurityGroups": ["sg-old"],
             "Instances": [{"InstanceId": "i-9"}],
             "ListenerDescriptions": [{"Listener": {"Protocol": "TCP", "LoadBalancerPort": 22, "InstancePort": 22}}]}]})
        v1.add_response("describe_tags", {"TagDescriptions": [{"LoadBalancerName": "legacy",
                                                               "Tags": [{"Key": "Name", "Value": "legado"}]}]},
                        {"LoadBalancerNames": ["legacy"]})

        r = self.por_id(self.correr("elb"))
        lb = r[ALB]
        self.assertEqual(lb["name"], "api")
        self.assertEqual(lb["tags"], {"Environment": "prod"})
        self.assertEqual(lb["attrs"]["scheme"], "internet-facing")
        self.assertEqual([(x["port"], x["protocol"]) for x in lb["attrs"]["listeners"]], [(443, "HTTPS"), (80, "HTTP")])
        self.assertEqual(lb["attrs"]["listeners"][0]["certificate_arns"], [CERT])
        self.assertEqual(lb["relations"], {"vpc_id": "vpc-1", "subnet_ids": ["subnet-a", "subnet-b"],
                                           "security_group_ids": ["sg-web"], "target_ids": [TG]})
        tg = r[TG]
        self.assertEqual(tg["relations"], {"vpc_id": "vpc-1", "target_ids": ["i-1", "i-2"], "attached_to": [ALB]})
        self.assertEqual(tg["attrs"]["healthy"], 1)
        cl = r["legacy"]
        self.assertEqual(cl["type"], "classic_load_balancer")
        self.assertEqual(cl["name"], "legacy")
        self.assertEqual(cl["relations"]["target_ids"], ["i-9"])
        self.assertEqual(cl["relations"]["security_group_ids"], ["sg-old"])

    def test_vacio_no_llama_tags(self):
        v2, v1 = self.s.stub("elbv2"), self.s.stub("elb")
        v2.add_response("describe_load_balancers", {"LoadBalancers": []})
        v2.add_response("describe_target_groups", {"TargetGroups": []})
        v1.add_response("describe_load_balancers", {"LoadBalancerDescriptions": []})
        self.assertEqual(self.correr("elb"), [])


class TestCloudFront(Base):
    def test_distribucion(self):
        cf = self.s.stub("cloudfront")
        acl = "arn:aws:wafv2:us-east-1:111122223333:global/webacl/cf/1"
        cf.add_response("list_distributions", {"DistributionList": {
            "Marker": "", "MaxItems": 100, "IsTruncated": False, "Quantity": 1, "Items": [{
                "Id": "E1", "ARN": "arn:aws:cloudfront::111122223333:distribution/E1", "Status": "Deployed",
                "LastModifiedTime": FECHA, "DomainName": "d1.cloudfront.net",
                "Aliases": {"Quantity": 1, "Items": ["www.ejemplo.com"]},
                "Origins": {"Quantity": 2, "Items": [
                    {"Id": "s3", "DomainName": "web.s3.us-east-1.amazonaws.com", "OriginPath": "",
                     "S3OriginConfig": {"OriginAccessIdentity": ""}},
                    {"Id": "alb", "DomainName": "api-123.us-east-1.elb.amazonaws.com",
                     "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": "X-Secret", "HeaderValue": "SECRETO"}]},
                     "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only"}}]},
                "DefaultCacheBehavior": {"TargetOriginId": "s3", "ViewerProtocolPolicy": "redirect-to-https"},
                "CacheBehaviors": {"Quantity": 0}, "CustomErrorResponses": {"Quantity": 0}, "Comment": "",
                "PriceClass": "PriceClass_100", "Enabled": True,
                "ViewerCertificate": {"ACMCertificateArn": CERT}, "Restrictions": {
                    "GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
                "WebACLId": acl, "HttpVersion": "http2", "IsIPV6Enabled": True, "Staging": False}]}})
        r = self.correr("cloudfront")[0]
        self.assertEqual(r["region"], "global")
        self.assertEqual(r["name"], "www.ejemplo.com")
        self.assertEqual([o["type"] for o in r["attrs"]["origins"]], ["s3", "custom"])
        self.assertEqual(r["relations"]["target_ids"], ["web.s3.us-east-1.amazonaws.com",
                                                        "api-123.us-east-1.elb.amazonaws.com"])
        self.assertEqual(r["relations"]["attached_to"], [acl, CERT])
        self.assertNotIn("SECRETO", json.dumps(r, default=str))

    def test_sin_distribuciones(self):
        self.s.stub("cloudfront").add_response("list_distributions", {"DistributionList": {
            "Marker": "", "MaxItems": 100, "IsTruncated": False, "Quantity": 0}})
        self.assertEqual(self.correr("cloudfront"), [])


class TestRoute53(Base):
    def test_zonas(self):
        r53 = self.s.stub("route53")
        r53.add_response("list_hosted_zones", {"HostedZones": [
            {"Id": "/hostedzone/ZPUB", "Name": "ejemplo.com.", "CallerReference": "a", "ResourceRecordSetCount": 12,
             "Config": {"PrivateZone": False}},
            {"Id": "/hostedzone/ZPRIV", "Name": "interno.local.", "CallerReference": "b", "ResourceRecordSetCount": 3,
             "Config": {"PrivateZone": True}}],
            "Marker": "", "IsTruncated": False, "MaxItems": "100"})
        r53.add_response("get_hosted_zone", {
            "HostedZone": {"Id": "/hostedzone/ZPRIV", "Name": "interno.local.", "CallerReference": "b"},
            "VPCs": [{"VPCRegion": "us-east-1", "VPCId": "vpc-1"}]}, {"Id": "ZPRIV"})
        r = self.por_id(self.correr("route53"))
        self.assertEqual(r["ZPUB"]["name"], "ejemplo.com")
        self.assertFalse(r["ZPUB"]["attrs"]["private"])
        self.assertEqual(r["ZPUB"]["attrs"]["record_count"], 12)
        self.assertEqual(r["ZPUB"]["relations"], {})
        self.assertEqual(r["ZPRIV"]["relations"], {"attached_to": ["vpc-1"]})
        self.assertEqual(r["ZPRIV"]["region"], "global")


class TestApiGateway(Base):
    def test_rest_y_v2(self):
        v1, v2 = self.s.stub("apigateway"), self.s.stub("apigatewayv2")
        v1.add_response("get_rest_apis", {"items": [
            {"id": "abc123", "name": "pagos", "endpointConfiguration": {"types": ["PRIVATE"],
                                                                        "vpcEndpointIds": ["vpce-api"]}}]})
        v1.add_response("get_stages", {"item": [{"stageName": "prod", "variables": {"db_pass": "SECRETO"}}]},
                        {"restApiId": "abc123"})
        v1.add_response("get_domain_names", {"items": [
            {"domainName": "api.ejemplo.com", "regionalDomainName": "d-1.execute-api.us-east-1.amazonaws.com",
             "regionalCertificateArn": CERT, "endpointConfiguration": {"types": ["REGIONAL"]},
             "domainNameStatus": "AVAILABLE"}]})
        v1.add_response("get_base_path_mappings", {"items": [{"basePath": "(none)", "restApiId": "abc123",
                                                              "stage": "prod"}]},
                        {"domainName": "api.ejemplo.com"})
        v1.add_response("get_vpc_links", {"items": [{"id": "vl1", "name": "nlb", "status": "AVAILABLE",
                                                     "targetArns": ["arn:nlb"]}]})
        v2.add_response("get_apis", {"Items": [
            {"ApiId": "h1", "Name": "webhooks", "ProtocolType": "HTTP", "RouteSelectionExpression": "x",
             "ApiEndpoint": "https://h1.execute-api.us-east-1.amazonaws.com"},
            {"ApiId": "w1", "Name": "chat", "ProtocolType": "WEBSOCKET", "RouteSelectionExpression": "x"}]})
        v2.add_response("get_stages", {"Items": [{"StageName": "$default"}]}, {"ApiId": "h1"})
        v2.add_response("get_stages", {"Items": [{"StageName": "prod", "StageVariables": {"k": "SECRETO"}}]},
                        {"ApiId": "w1"})
        v2.add_response("get_domain_names", {"Items": [
            {"DomainName": "hooks.ejemplo.com", "DomainNameConfigurations": [
                {"EndpointType": "REGIONAL", "CertificateArn": CERT, "DomainNameStatus": "AVAILABLE",
                 "ApiGatewayDomainName": "d-2.execute-api.us-east-1.amazonaws.com"}]}]})
        v2.add_response("get_api_mappings", {"Items": [{"ApiId": "h1", "Stage": "$default", "ApiMappingKey": ""}],
                                             "NextToken": "n"}, {"DomainName": "hooks.ejemplo.com"})
        v2.add_response("get_api_mappings", {"Items": [{"ApiId": "h1", "Stage": "v2", "ApiMappingKey": "v2"}]},
                        {"DomainName": "hooks.ejemplo.com", "NextToken": "n"})
        v2.add_response("get_vpc_links", {"Items": [{"VpcLinkId": "vl2", "Name": "priv", "SubnetIds": ["subnet-a"],
                                                     "SecurityGroupIds": ["sg-vl"], "VpcLinkStatus": "AVAILABLE"}]})

        recursos = self.correr("apigateway")
        self.assertNotIn("SECRETO", json.dumps(recursos, default=str))
        r = self.por_id(recursos)
        self.assertEqual(r["abc123"]["attrs"]["endpoint_types"], ["PRIVATE"])
        self.assertEqual(r["abc123"]["attrs"]["stages"], ["prod"])
        self.assertEqual(r["abc123"]["relations"], {"attached_to": ["vpce-api"]})
        self.assertEqual(r["api.ejemplo.com"]["relations"], {"target_ids": ["abc123"]})
        self.assertEqual(r["vl1"]["relations"], {"target_ids": ["arn:nlb"]})
        self.assertEqual(r["h1"]["type"], "http_api")
        self.assertEqual(r["w1"]["type"], "websocket_api")
        self.assertEqual(len(r["hooks.ejemplo.com"]["attrs"]["mappings"]), 2)
        self.assertEqual(r["hooks.ejemplo.com"]["relations"], {"target_ids": ["h1"]})
        self.assertEqual(r["vl2"]["relations"], {"subnet_ids": ["subnet-a"], "security_group_ids": ["sg-vl"]})


class TestTransfer(Base):
    def test_servidor(self):
        tr = self.s.stub("transfer")
        tr.add_response("list_servers", {"Servers": [
            {"Arn": "arn:aws:transfer:us-east-1:111122223333:server/s-0123456789abcdef0", "ServerId": "s-0123456789abcdef0", "State": "ONLINE",
             "UserCount": 7, "EndpointType": "VPC"}]})
        tr.add_response("describe_server", {"Server": {
            "Arn": "arn:aws:transfer:us-east-1:111122223333:server/s-0123456789abcdef0", "ServerId": "s-0123456789abcdef0", "State": "ONLINE",
            "Protocols": ["SFTP"], "EndpointType": "VPC", "Domain": "S3", "IdentityProviderType": "API_GATEWAY",
            "IdentityProviderDetails": {"Url": "https://SECRETO-idp"}, "HostKeyFingerprint": "SHA256:SECRETO",
            "Certificate": "arn:aws:acm:SECRETO",
            "EndpointDetails": {"VpcId": "vpc-1", "SubnetIds": ["subnet-a"], "SecurityGroupIds": ["sg-0sftp0000"],
                                "AddressAllocationIds": ["eipalloc-1"], "VpcEndpointId": "vpce-0123456789abcdef0"},
            "Tags": [{"Key": "Name", "Value": "sftp-prod"}]}}, {"ServerId": "s-0123456789abcdef0"})
        r = self.correr("transfer")[0]
        self.assertNotIn("SECRETO", json.dumps(r, default=str))
        self.assertEqual(r["name"], "sftp-prod")
        self.assertEqual(r["attrs"]["protocols"], ["SFTP"])
        self.assertEqual(r["attrs"]["user_count"], 7)
        self.assertEqual(r["relations"], {"vpc_id": "vpc-1", "subnet_ids": ["subnet-a"],
                                          "security_group_ids": ["sg-0sftp0000"], "attached_to": ["vpce-0123456789abcdef0"]})


def _acl_detalle(nombre, acl_id, arn):
    return {"WebACL": {
        "Name": nombre, "Id": acl_id, "ARN": arn, "DefaultAction": {"Allow": {}},
        "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "m"},
        "Capacity": 700,
        "Rules": [
            {"Name": "aws-common", "Priority": 0, "OverrideAction": {"None": {}},
             "Statement": {"ManagedRuleGroupStatement": {"VendorName": "AWS", "Name": "AWSManagedRulesCommonRuleSet"}},
             "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "c"}},
            {"Name": "bloquear-pais", "Priority": 1, "Action": {"Block": {}},
             "Statement": {"GeoMatchStatement": {"CountryCodes": ["KP"]}},
             "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "g"}}]}}


class TestWaf(Base):
    def test_regional(self):
        waf = self.s.stub("wafv2")
        arn = "arn:aws:wafv2:us-east-1:111122223333:regional/webacl/api/1"
        waf.add_response("list_web_acls", {"WebACLs": [{"Name": "api", "Id": "1", "ARN": arn}], "NextMarker": "m"},
                         {"Scope": "REGIONAL", "Limit": 100})
        waf.add_response("list_web_acls", {"WebACLs": []}, {"Scope": "REGIONAL", "Limit": 100, "NextMarker": "m"})
        for tipo in ["APPLICATION_LOAD_BALANCER", "API_GATEWAY", "COGNITO_USER_POOL", "APPSYNC"]:
            waf.add_response("list_resources_for_web_acl",
                             {"ResourceArns": [ALB] if tipo == "APPLICATION_LOAD_BALANCER" else []},
                             {"WebACLArn": arn, "ResourceType": tipo})
        waf.add_response("get_web_acl", _acl_detalle("api", "1", arn), {"Name": "api", "Scope": "REGIONAL", "Id": "1"})
        r = self.correr("waf")[0]
        self.assertEqual(r["relations"], {"attached_to": [ALB]})
        self.assertEqual(r["attrs"]["default_action"], "Allow")
        self.assertEqual(r["attrs"]["rules"][0]["managed"], "AWS/AWSManagedRulesCommonRuleSet")
        self.assertEqual(r["attrs"]["rules"][1]["action"], "Block")
        self.assertNotIn("KP", json.dumps(r))  # no se copia el contenido de las reglas

    def test_cloudfront(self):
        waf, cf = self.s.stub("wafv2"), self.s.stub("cloudfront")
        arn = "arn:aws:wafv2:us-east-1:111122223333:global/webacl/cf/2"
        waf.add_response("list_web_acls", {"WebACLs": [{"Name": "cf", "Id": "2", "ARN": arn}]},
                         {"Scope": "CLOUDFRONT", "Limit": 100})
        waf.add_response("get_web_acl", _acl_detalle("cf", "2", arn), {"Name": "cf", "Scope": "CLOUDFRONT", "Id": "2"})
        cf.add_response("list_distributions_by_web_acl_id", {"DistributionList": {
            "Marker": "", "MaxItems": 100, "IsTruncated": False, "Quantity": 0, "Items": []}}, {"WebACLId": arn})
        r = self.correr("waf_cloudfront")[0]
        self.assertEqual(r["region"], "global")
        self.assertEqual(r["attrs"]["scope"], "CLOUDFRONT")


class TestAcm(Base):
    def test_certificados(self):
        acm = self.s.stub("acm")
        from colectores.acm import TIPOS_LLAVE
        acm.add_response("list_certificates", {"CertificateSummaryList": [{"CertificateArn": CERT,
                                                                           "DomainName": "ejemplo.com"}]},
                         {"Includes": {"keyTypes": TIPOS_LLAVE}})
        acm.add_response("describe_certificate", {"Certificate": {
            "CertificateArn": CERT, "DomainName": "ejemplo.com", "SubjectAlternativeNames": ["*.ejemplo.com"],
            "Status": "ISSUED", "Type": "AMAZON_ISSUED", "NotAfter": FECHA, "KeyAlgorithm": "RSA-2048",
            "InUseBy": [ALB], "DomainValidationOptions": [{"DomainName": "ejemplo.com", "ValidationMethod": "DNS"}]}},
            {"CertificateArn": CERT})
        r = self.correr("acm")[0]
        self.assertEqual(r["state"], "ISSUED")
        self.assertTrue(r["attrs"]["in_use"])
        self.assertEqual(r["attrs"]["not_after"], FECHA)
        self.assertEqual(r["relations"], {"attached_to": [ALB]})


class TestGlobalAccelerator(Base):
    def test_acelerador_en_us_west_2(self):
        ga = self.s.stub("globalaccelerator", region="us-west-2")
        acc = "arn:aws:globalaccelerator::111122223333:accelerator/a1"
        lis = acc + "/listener/l1"
        ga.add_response("list_accelerators", {"Accelerators": [
            {"AcceleratorArn": acc, "Name": "edge", "Enabled": True, "Status": "DEPLOYED", "DnsName": "a1.awsglobalaccelerator.com",
             "IpAddressType": "IPV4", "IpSets": [{"IpFamily": "IPv4", "IpAddresses": ["75.2.0.1", "99.83.0.1"]}]}]})
        ga.add_response("list_listeners", {"Listeners": [
            {"ListenerArn": lis, "Protocol": "TCP", "PortRanges": [{"FromPort": 443, "ToPort": 443}]}]},
            {"AcceleratorArn": acc})
        ga.add_response("list_endpoint_groups", {"EndpointGroups": [
            {"EndpointGroupArn": lis + "/endpoint-group/g1", "EndpointGroupRegion": "us-east-1",
             "EndpointDescriptions": [{"EndpointId": ALB, "Weight": 128, "HealthState": "HEALTHY"}]}]},
            {"ListenerArn": lis})
        r = self.correr("globalaccelerator")[0]
        self.assertEqual(r["region"], "global")
        self.assertEqual(r["attrs"]["ips"], ["75.2.0.1", "99.83.0.1"])
        self.assertEqual(r["attrs"]["listeners"][0]["ports"], ["443"])
        self.assertEqual(r["relations"], {"target_ids": [ALB]})


if __name__ == "__main__":
    unittest.main()
