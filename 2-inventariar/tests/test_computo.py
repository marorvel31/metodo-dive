"""Pruebas de los colectores de cómputo con botocore Stubber: ninguna llamada real a AWS.

Uso: .venv/bin/python -m unittest discover -s 2-inventariar/tests   (o pytest)
"""

import datetime as dt
import importlib
import json
import sys
import unittest
from pathlib import Path

import boto3
from botocore.stub import ANY, Stubber

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent.parent))
sys.path.insert(0, str(AQUI.parent))

CUENTA, REGION = "111122223333", "us-east-1"
FECHA = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
CLAVES = {"account_id", "region", "service", "type", "id", "arn", "name", "state", "tags", "attrs", "relations"}
RELACIONES = {"vpc_id", "subnet_ids", "security_group_ids", "target_ids", "attached_to"}
SERVICIOS_CE = set(json.loads((AQUI.parent / "servicios_cost_explorer.json").read_text()))
SERVICIOS_CE.add("Amazon Elastic Container Service for Kubernetes")  # no aparece en el JSON de ejemplo


class SesionFalsa:
    """Imita boto3.Session.client devolviendo clientes con Stubber ya activado."""

    def __init__(self):
        self._base = boto3.Session(aws_access_key_id="x", aws_secret_access_key="x", region_name=REGION)
        self.clientes, self.stubs = {}, {}

    def stub(self, servicio):
        if servicio not in self.stubs:
            c = self._base.client(servicio, region_name=REGION)
            self.clientes[servicio] = c
            self.stubs[servicio] = Stubber(c)
            self.stubs[servicio].activate()
        return self.stubs[servicio]

    def client(self, servicio, region_name=None, config=None):
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
            self.assertLessEqual(set(r["relations"]), RELACIONES)
            json.dumps(r, default=str)
        self.assertTrue(set(mod.TRIGGERS) <= SERVICIOS_CE, mod.TRIGGERS)
        self.assertFalse(mod.GLOBAL)
        return recursos


class TestEBS(Base):
    def test_volumenes_y_snapshots(self):
        st = self.s.stub("ec2")
        st.add_response("describe_volumes", {"Volumes": [
            {"VolumeId": "vol-1", "Size": 100, "VolumeType": "gp2", "State": "available", "Encrypted": False,
             "AvailabilityZone": "us-east-1a", "CreateTime": FECHA, "SnapshotId": "", "Attachments": []},
            {"VolumeId": "vol-2", "Size": 8, "VolumeType": "gp3", "State": "in-use", "Encrypted": True,
             "AvailabilityZone": "us-east-1a", "CreateTime": FECHA,
             "Attachments": [{"InstanceId": "i-1", "VolumeId": "vol-2", "State": "attached"}],
             "Tags": [{"Key": "Name", "Value": "raiz"}]},
        ]}, {})
        st.add_response("describe_snapshots", {"Snapshots": [
            {"SnapshotId": "snap-1", "VolumeId": "vol-2", "VolumeSize": 8, "StartTime": FECHA, "State": "completed"},
            {"SnapshotId": "snap-2", "VolumeId": "vol-ffffffff", "VolumeSize": 8, "StartTime": FECHA, "State": "completed"},
        ]}, {"OwnerIds": ["self"]})
        r = {x["id"]: x for x in self.correr("ebs")}
        self.assertEqual(r["vol-1"]["state"], "available")
        self.assertEqual(r["vol-1"]["relations"], {})
        self.assertEqual(r["vol-2"]["relations"]["attached_to"], ["i-1"])
        self.assertEqual(r["vol-2"]["name"], "raiz")
        self.assertEqual(r["vol-2"]["attrs"]["volume_type"], "gp3")
        self.assertEqual(r["snap-1"]["relations"]["attached_to"], ["vol-2"])
        self.assertEqual(r["snap-2"]["relations"], {})


class TestEIP(Base):
    def test_asociada_y_libre(self):
        self.s.stub("ec2").add_response("describe_addresses", {"Addresses": [
            {"AllocationId": "eipalloc-1", "PublicIp": "203.0.113.1", "Domain": "vpc",
             "AssociationId": "eipassoc-1", "InstanceId": "i-1"},
            {"AllocationId": "eipalloc-2", "PublicIp": "203.0.113.2", "Domain": "vpc"},
        ]}, {})
        r = {x["id"]: x for x in self.correr("eip")}
        self.assertEqual(r["eipalloc-1"]["state"], "associated")
        self.assertEqual(r["eipalloc-1"]["relations"]["attached_to"], ["i-1"])
        self.assertEqual(r["eipalloc-2"]["state"], "unassociated")
        self.assertFalse(r["eipalloc-2"]["attrs"]["associated"])


class TestAMI(Base):
    def test_ami_con_snapshots(self):
        self.s.stub("ec2").add_response("describe_images", {"Images": [
            {"ImageId": "ami-1", "Name": "base-2026", "State": "available", "CreationDate": "2026-01-01T00:00:00Z",
             "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": "snap-1"}},
                                     {"DeviceName": "/dev/sdb", "VirtualName": "ephemeral0"}]},
        ]}, {"Owners": ["self"]})
        (r,) = self.correr("ami")
        self.assertEqual(r["name"], "base-2026")
        self.assertEqual(r["attrs"]["snapshot_ids"], ["snap-1"])


class TestLambda(Base):
    def test_descarta_environment(self):
        st = self.s.stub("lambda")
        arn = f"arn:aws:lambda:{REGION}:{CUENTA}:function:fn"
        st.add_response("list_functions", {"Functions": [{
            "FunctionName": "fn", "FunctionArn": arn, "Runtime": "python3.12", "MemorySize": 256, "Timeout": 30,
            "LastModified": "2026-01-01T00:00:00.000+0000", "Architectures": ["arm64"],
            "Environment": {"Variables": {"DB_PASSWORD": "NO-DEBE-SALIR"}},
            "VpcConfig": {"VpcId": "vpc-1", "SubnetIds": ["subnet-1"], "SecurityGroupIds": ["sg-1"]},
        }]}, {})
        st.add_response("list_tags", {"Tags": {"env": "prod"}}, {"Resource": arn})
        (r,) = self.correr("lambda_")
        self.assertNotIn("NO-DEBE-SALIR", json.dumps(r, default=str))
        self.assertNotIn("DB_PASSWORD", json.dumps(r, default=str))
        self.assertEqual(r["attrs"]["memory_mb"], 256)
        self.assertEqual(r["tags"], {"env": "prod"})
        self.assertEqual(r["relations"], {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"], "security_group_ids": ["sg-1"]})

    def test_sin_vpc_y_tags_denegados(self):
        st = self.s.stub("lambda")
        st.add_response("list_functions", {"Functions": [{"FunctionName": "f2", "FunctionArn": "arn:aws:lambda:x:1:function:f2",
                                                          "VpcConfig": {"SubnetIds": [], "SecurityGroupIds": [], "VpcId": ""}}]}, {})
        st.add_client_error("list_tags", "AccessDeniedException")
        (r,) = self.correr("lambda_")
        self.assertEqual(r["relations"], {})
        self.assertEqual(r["tags"], {})


class TestECS(Base):
    def test_cluster_y_servicio(self):
        st = self.s.stub("ecs")
        c_arn = f"arn:aws:ecs:{REGION}:{CUENTA}:cluster/prod"
        s_arn = f"arn:aws:ecs:{REGION}:{CUENTA}:service/prod/api"
        st.add_response("list_clusters", {"clusterArns": [c_arn]}, {})
        st.add_response("describe_clusters", {"clusters": [{"clusterArn": c_arn, "clusterName": "prod", "status": "ACTIVE",
                                                            "runningTasksCount": 2, "activeServicesCount": 1}]},
                        {"clusters": [c_arn], "include": ["TAGS"]})
        st.add_response("list_services", {"serviceArns": [s_arn]}, {"cluster": c_arn})
        st.add_response("describe_services", {"services": [{
            "serviceArn": s_arn, "serviceName": "api", "status": "ACTIVE", "launchType": "FARGATE",
            "desiredCount": 2, "runningCount": 2,
            "taskDefinition": f"arn:aws:ecs:{REGION}:{CUENTA}:task-definition/api:7",
            "loadBalancers": [{"targetGroupArn": "arn:tg/1", "containerName": "api", "containerPort": 8080}],
            "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"],
                                                             "assignPublicIp": "DISABLED"}},
        }]}, {"cluster": c_arn, "services": [s_arn], "include": ["TAGS"]})
        r = {x["type"]: x for x in self.correr("ecs")}
        self.assertEqual(r["cluster"]["attrs"]["running_tasks"], 2)
        svc = r["service"]
        self.assertEqual(svc["attrs"]["task_definition"].split("/")[-1], "api:7")
        self.assertEqual(svc["relations"]["target_ids"], ["arn:tg/1"])
        self.assertEqual(svc["relations"]["security_group_ids"], ["sg-1"])


class TestECR(Base):
    def test_repo_con_tamano(self):
        st = self.s.stub("ecr")
        arn = f"arn:aws:ecr:{REGION}:{CUENTA}:repository/api"
        st.add_response("describe_repositories", {"repositories": [{
            "repositoryArn": arn, "repositoryName": "api", "imageTagMutability": "MUTABLE",
            "imageScanningConfiguration": {"scanOnPush": True}}]}, {})
        st.add_response("describe_images", {"imageDetails": [{"imageSizeInBytes": 1024 ** 3}, {"imageSizeInBytes": 1024 ** 3}]},
                        {"repositoryName": "api", "maxResults": 1000})
        st.add_response("list_tags_for_resource", {"tags": [{"Key": "team", "Value": "core"}]}, {"resourceArn": arn})
        (r,) = self.correr("ecr")
        self.assertEqual(r["attrs"]["image_count"], 2)
        self.assertEqual(r["attrs"]["approx_size_gb"], 2.0)
        self.assertTrue(r["attrs"]["scan_on_push"])
        self.assertEqual(r["tags"], {"team": "core"})


class TestAutoScaling(Base):
    def test_asg_con_mixed_instances(self):
        self.s.stub("autoscaling").add_response("describe_auto_scaling_groups", {"AutoScalingGroups": [{
            "AutoScalingGroupName": "web", "AutoScalingGroupARN": "arn:asg/web", "MinSize": 1, "MaxSize": 4,
            "DesiredCapacity": 2, "DefaultCooldown": 300, "AvailabilityZones": ["us-east-1a"],
            "HealthCheckType": "ELB", "CreatedTime": FECHA, "VPCZoneIdentifier": "subnet-1,subnet-2",
            "MixedInstancesPolicy": {"LaunchTemplate": {"LaunchTemplateSpecification": {
                "LaunchTemplateId": "lt-1", "Version": "$Latest"}}},
            "TargetGroupARNs": ["arn:tg/1"],
            "Instances": [{"InstanceId": "i-1", "AvailabilityZone": "us-east-1a", "LifecycleState": "InService",
                           "HealthStatus": "Healthy", "ProtectedFromScaleIn": False}],
        }]}, {})
        (r,) = self.correr("autoscaling")
        self.assertEqual(r["attrs"]["launch_template_id"], "lt-1")
        self.assertEqual(r["relations"]["subnet_ids"], ["subnet-1", "subnet-2"])
        self.assertEqual(r["relations"]["attached_to"], ["i-1"])


class TestBeanstalk(Base):
    def test_entorno(self):
        self.s.stub("elasticbeanstalk").add_response("describe_environments", {"Environments": [{
            "EnvironmentId": "e-1", "EnvironmentName": "api-prod", "ApplicationName": "api",
            "PlatformArn": "arn:aws:elasticbeanstalk:us-east-1::platform/Node.js 20", "Status": "Ready",
            "Health": "Green", "HealthStatus": "Ok"}]}, {"IncludeDeleted": False})
        (r,) = self.correr("elasticbeanstalk")
        self.assertEqual((r["name"], r["state"], r["attrs"]["health"]), ("api-prod", "Ready", "Green"))


class TestLightsail(Base):
    def test_instancia(self):
        self.s.stub("lightsail").add_response("get_instances", {"instances": [{
            "name": "wp", "arn": "arn:ls/wp", "bundleId": "small_3_0", "state": {"name": "running"},
            "hardware": {"cpuCount": 1, "ramSizeInGb": 2.0, "disks": [{"sizeInGb": 60}]},
            "location": {"availabilityZone": "us-east-1a"}}]}, {})
        (r,) = self.correr("lightsail")
        self.assertEqual((r["state"], r["attrs"]["disk_gb"]), ("running", 60))


class TestEKS(Base):
    def test_cluster_y_nodegroup(self):
        st = self.s.stub("eks")
        st.add_response("list_clusters", {"clusters": ["prod"]}, {})
        st.add_response("describe_cluster", {"cluster": {
            "name": "prod", "arn": "arn:eks/prod", "version": "1.30", "status": "ACTIVE",
            "certificateAuthority": {"data": "CERTIFICADO-NO-DEBE-SALIR"},
            "resourcesVpcConfig": {"vpcId": "vpc-1", "subnetIds": ["subnet-1"], "securityGroupIds": ["sg-1"],
                                   "clusterSecurityGroupId": "sg-2", "endpointPublicAccess": True}}}, {"name": "prod"})
        st.add_response("list_nodegroups", {"nodegroups": ["ng1"]}, {"clusterName": "prod"})
        st.add_response("describe_nodegroup", {"nodegroup": {
            "nodegroupName": "ng1", "nodegroupArn": "arn:eks/ng1", "status": "ACTIVE", "instanceTypes": ["m5.large"],
            "capacityType": "SPOT", "scalingConfig": {"minSize": 1, "maxSize": 3, "desiredSize": 2},
            "subnets": ["subnet-1"], "resources": {"autoScalingGroups": [{"name": "eks-ng1-asg"}]}}},
            {"clusterName": "prod", "nodegroupName": "ng1"})
        recursos = self.correr("eks")
        self.assertNotIn("CERTIFICADO", json.dumps(recursos, default=str))
        r = {x["type"]: x for x in recursos}
        self.assertEqual(r["cluster"]["relations"]["security_group_ids"], ["sg-1", "sg-2"])
        self.assertEqual(r["nodegroup"]["id"], "prod/ng1")
        self.assertEqual(r["nodegroup"]["relations"]["attached_to"], ["eks-ng1-asg"])


class TestPlanear(Base):
    def test_carga_y_planificacion(self):
        import inventariar
        mods = {m.NAME: m for m in inventariar.cargar_colectores()}
        for n in ["ebs", "eip", "ami", "lambda", "ecs", "ecr", "autoscaling", "elasticbeanstalk", "lightsail", "eks"]:
            self.assertIn(n, mods)
        disc = {"accounts": {CUENTA: {"name": "x", "regions": {REGION: {"services": {"AWS Lambda": 1.0}}}}}}
        plan = [t[2].NAME for t in inventariar.planear(disc, [mods["lambda"], mods["ebs"]])]
        self.assertEqual(plan, ["lambda"])


if __name__ == "__main__":
    unittest.main()
