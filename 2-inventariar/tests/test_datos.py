"""Pruebas de colectores de datos, mensajería y seguridad con Stubber (sin llamadas reales)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

AQUI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI))

from colectores import (
    backup,
    dms,
    dynamodb,
    efs,
    elasticache,
    glue,
    kinesis,
    kms,
    opensearch,
    rds,
    s3,
    secretsmanager,
    sns,
    sqs,
    stepfunctions,
)

CUENTA = "123456789012"
REGION = "us-east-1"
FECHA = datetime(2026, 1, 1, tzinfo=timezone.utc)
CLAVES = {"account_id", "region", "service", "type", "id", "arn", "name", "state", "tags", "attrs", "relations"}


class SesionFalsa:
    """Devuelve un cliente con Stubber por servicio, sin importar la región."""

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
        self.stub(servicio)
        return self.clientes[servicio]

    def verificar(self):
        for s in self.stubs.values():
            s.assert_no_pending_responses()


@pytest.fixture
def ses():
    s = SesionFalsa()
    yield s
    s.verificar()


def _forma(recursos, servicio):
    for r in recursos:
        assert set(r) == CLAVES
        assert r["service"] == servicio and r["account_id"] == CUENTA
    json.dumps(recursos, default=str)  # serializable


def test_interfaz():
    for m in (backup, dms, dynamodb, efs, elasticache, glue, kinesis, kms, opensearch, rds, s3,
              secretsmanager, sns, sqs, stepfunctions):
        assert m.NAME == m.__name__.rsplit(".", 1)[-1]
        assert m.ALWAYS is False and isinstance(m.TRIGGERS, list) and m.TRIGGERS
        assert m.GLOBAL is (m is s3)
    nombres = set(json.loads((AQUI / "servicios_cost_explorer.json").read_text()))
    for m in (backup, dms, dynamodb, efs, elasticache, glue, kms, opensearch, rds, s3,
              secretsmanager, sns, sqs, stepfunctions):
        assert set(m.TRIGGERS) <= nombres, m.NAME
    assert "Amazon Kinesis" in kinesis.TRIGGERS


def test_rds(ses):
    st = ses.stub("rds")
    st.add_response("describe_db_subnet_groups", {"DBSubnetGroups": [
        {"DBSubnetGroupName": "grp", "VpcId": "vpc-1", "Subnets": [{"SubnetIdentifier": "subnet-a"}, {"SubnetIdentifier": "subnet-b"}]}]})
    st.add_response("describe_db_instances", {"DBInstances": [{
        "DBInstanceIdentifier": "db1", "DBInstanceArn": "arn:db1", "DBInstanceClass": "db.t3.micro",
        "Engine": "postgres", "EngineVersion": "15.4", "DBInstanceStatus": "available", "MasterUsername": "admin",
        "Endpoint": {"Address": "db1.x.rds.amazonaws.com", "Port": 5432}, "AllocatedStorage": 20,
        "StorageType": "gp3", "MultiAZ": False, "PubliclyAccessible": True, "StorageEncrypted": True,
        "BackupRetentionPeriod": 7, "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-1", "Status": "active"}],
        "DBSubnetGroup": {"DBSubnetGroupName": "grp", "VpcId": "vpc-1", "Subnets": [{"SubnetIdentifier": "subnet-a"}]},
        "TagList": [{"Key": "Environment", "Value": "prod"}]}]})
    st.add_response("describe_db_clusters", {"DBClusters": [{
        "DBClusterIdentifier": "aur1", "DBClusterArn": "arn:aur1", "Engine": "aurora-mysql", "Status": "available",
        "MasterUsername": "admin", "DBSubnetGroup": "grp", "Endpoint": "aur1.cluster-x.rds.amazonaws.com",
        "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-2"}], "DBClusterMembers": [{"DBInstanceIdentifier": "aur1-a"}],
        "StorageEncrypted": True, "BackupRetentionPeriod": 1}]})
    r = rds.collect(ses, REGION, CUENTA)
    _forma(r, "rds")
    inst, clus = r
    assert inst["attrs"]["endpoint_host"] == "db1.x.rds.amazonaws.com"
    assert inst["attrs"]["publicly_accessible"] is True and inst["tags"] == {"Environment": "prod"}
    assert inst["relations"] == {"vpc_id": "vpc-1", "subnet_ids": ["subnet-a"], "security_group_ids": ["sg-1"]}
    assert "admin" not in json.dumps(r, default=str)
    assert clus["type"] == "cluster" and clus["relations"]["subnet_ids"] == ["subnet-a", "subnet-b"]


def test_dynamodb(ses):
    st = ses.stub("dynamodb")
    st.add_response("list_tables", {"TableNames": ["tab1", "tab2"]})
    st.add_response("describe_table", {"Table": {"TableName": "tab1", "TableArn": "arn:tab1", "TableStatus": "ACTIVE",
        "TableSizeBytes": 100, "ItemCount": 3, "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
        "ProvisionedThroughput": {"ReadCapacityUnits": 0, "WriteCapacityUnits": 0}}}, {"TableName": "tab1"})
    st.add_response("list_tags_of_resource", {"Tags": [{"Key": "Name", "Value": "tabla-uno"}]})
    st.add_response("describe_table", {"Table": {"TableName": "tab2", "TableArn": "arn:tab2", "TableStatus": "ACTIVE",
        "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}}}, {"TableName": "tab2"})
    st.add_client_error("list_tags_of_resource", "AccessDeniedException")
    r = dynamodb.collect(ses, REGION, CUENTA)
    _forma(r, "dynamodb")
    assert r[0]["attrs"]["billing_mode"] == "PAY_PER_REQUEST" and r[0]["tags"] == {"Name": "tabla-uno"}
    assert r[1]["attrs"]["billing_mode"] == "PROVISIONED" and r[1]["attrs"]["rcu"] == 5 and r[1]["tags"] == {}


def test_s3(ses):
    st = ses.stub("s3")
    st.add_response("list_buckets", {"Buckets": [{"Name": "b-eu"}, {"Name": "b-use1"}]})
    # b-eu: sin BucketRegion → get_bucket_location; tags AccessDenied; sin cifrado configurado.
    st.add_response("get_bucket_location", {"LocationConstraint": "EU"}, {"Bucket": "b-eu"})
    st.add_client_error("get_bucket_tagging", "AccessDenied")
    st.add_response("get_bucket_versioning", {"Status": "Enabled"})
    st.add_client_error("get_bucket_encryption", "ServerSideEncryptionConfigurationNotFoundError")
    st.add_client_error("get_public_access_block", "NoSuchPublicAccessBlockConfiguration")
    # b-use1: LocationConstraint nulo; NoSuchTagSet no es error.
    st.add_response("get_bucket_location", {}, {"Bucket": "b-use1"})
    st.add_client_error("get_bucket_tagging", "NoSuchTagSet")
    st.add_response("get_bucket_versioning", {})
    st.add_response("get_bucket_encryption", {"ServerSideEncryptionConfiguration": {"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "k1"}, "BucketKeyEnabled": True}]}})
    st.add_response("get_public_access_block", {"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}})
    r = s3.collect(ses, REGION, CUENTA)
    _forma(r, "s3")
    eu, use1 = r
    assert eu["region"] == "eu-west-1" and eu["attrs"]["errors"] == {"tags": "AccessDenied"}
    assert eu["attrs"]["versioning"] == "Enabled" and eu["attrs"]["encryption"] is None
    assert eu["attrs"]["public_access_fully_blocked"] is False
    assert use1["region"] == "us-east-1" and use1["attrs"]["errors"] == {}
    assert use1["attrs"]["versioning"] == "Disabled" and use1["attrs"]["encryption"] == "aws:kms"
    assert use1["attrs"]["public_access_fully_blocked"] is True


def test_elasticache(ses):
    st = ses.stub("elasticache")
    st.add_response("describe_replication_groups", {"ReplicationGroups": [{"ReplicationGroupId": "rg1", "Status": "available",
        "MemberClusters": ["rg1-001"], "CacheNodeType": "cache.t3.micro", "TransitEncryptionEnabled": True,
        "AtRestEncryptionEnabled": False, "ClusterEnabled": False}]})
    st.add_response("describe_cache_clusters", {"CacheClusters": [{"CacheClusterId": "rg1-001", "Engine": "redis",
        "EngineVersion": "7.1", "CacheClusterStatus": "available", "SecurityGroups": [{"SecurityGroupId": "sg-9"}]}]})
    r = elasticache.collect(ses, REGION, CUENTA)
    _forma(r, "elasticache")
    assert r[0]["attrs"]["cluster_mode"] == "disabled" and r[0]["attrs"]["transit_encryption"] is True
    assert r[1]["relations"]["security_group_ids"] == ["sg-9"]


def _dominio(nombre, policy, vpc=None):
    d = {"DomainId": f"{CUENTA}/{nombre}", "DomainName": nombre, "ARN": f"arn:aws:es:us-east-1:{CUENTA}:domain/{nombre}", "Created": True, "Deleted": False,
         "Processing": False, "EngineVersion": "OpenSearch_2.11", "ClusterConfig": {"InstanceType": "t3.small.search", "InstanceCount": 1},
         "AccessPolicies": json.dumps(policy), "EncryptionAtRestOptions": {"Enabled": True}}
    if vpc:
        d["VPCOptions"] = {"VPCId": vpc, "SubnetIds": ["subnet-1"], "SecurityGroupIds": ["sg-1"]}
    return d


def test_opensearch(ses):
    st = ses.stub("opensearch")
    nombres = [f"dom{i}" for i in range(6)]
    st.add_response("list_domain_names", {"DomainNames": [{"DomainName": n} for n in nombres]})
    abierta = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "es:*"}]}
    cerrada = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{CUENTA}:root"}, "Action": "es:*"}]}
    st.add_response("describe_domains", {"DomainStatusList": [_dominio("dom0", abierta)] +
                                         [_dominio(n, cerrada, "vpc-1") for n in nombres[1:5]]}, {"DomainNames": nombres[:5]})
    st.add_response("describe_domains", {"DomainStatusList": [_dominio("dom5", {"Statement": {"Effect": "Allow", "Principal": "*"}})]},
                    {"DomainNames": ["dom5"]})
    r = opensearch.collect(ses, REGION, CUENTA)
    _forma(r, "opensearch")
    assert len(r) == 6
    assert r[0]["attrs"]["access_policy_principal_star"] is True and r[0]["attrs"]["in_vpc"] is False
    assert r[1]["attrs"]["access_policy_principal_star"] is False and r[1]["relations"]["vpc_id"] == "vpc-1"
    assert r[5]["attrs"]["access_policy_principal_star"] is True
    assert "es:*" not in json.dumps(r, default=str)  # la policy no se copia


def test_kinesis(ses):
    k, f = ses.stub("kinesis"), ses.stub("firehose")
    k.add_response("list_streams", {"StreamNames": ["s1"], "HasMoreStreams": False})
    k.add_response("describe_stream_summary", {"StreamDescriptionSummary": {"StreamName": "s1", "StreamARN": "arn:s1",
        "StreamStatus": "ACTIVE", "StreamModeDetails": {"StreamMode": "ON_DEMAND"}, "RetentionPeriodHours": 24,
        "StreamCreationTimestamp": FECHA, "EnhancedMonitoring": [], "OpenShardCount": 4}})
    f.add_response("list_delivery_streams", {"DeliveryStreamNames": ["f1"], "HasMoreDeliveryStreams": True}, {"Limit": 100})
    f.add_response("describe_delivery_stream", {"DeliveryStreamDescription": {"DeliveryStreamName": "f1",
        "DeliveryStreamARN": "arn:f1", "DeliveryStreamStatus": "ACTIVE", "DeliveryStreamType": "DirectPut",
        "VersionId": "1", "HasMoreDestinations": False, "Destinations": [{"DestinationId": "d1",
            "HttpEndpointDestinationDescription": {"EndpointConfiguration": {"Url": "https://secreto.example.com", "Name": "x"}}}]}})
    f.add_response("list_delivery_streams", {"DeliveryStreamNames": [], "HasMoreDeliveryStreams": False},
                   {"Limit": 100, "ExclusiveStartDeliveryStreamName": "f1"})
    r = kinesis.collect(ses, REGION, CUENTA)
    _forma(r, "kinesis")
    assert r[0]["attrs"]["mode"] == "ON_DEMAND" and r[0]["attrs"]["open_shards"] == 4
    assert r[1]["type"] == "firehose" and r[1]["attrs"]["destinations"] == ["HttpEndpoint"]
    assert "secreto" not in json.dumps(r, default=str)


def test_efs(ses):
    st = ses.stub("efs")
    st.add_response("describe_file_systems", {"FileSystems": [{"FileSystemId": "fs-1", "OwnerId": CUENTA,
        "CreationToken": "t", "CreationTime": FECHA, "LifeCycleState": "available", "NumberOfMountTargets": 1,
        "SizeInBytes": {"Value": 2048}, "PerformanceMode": "generalPurpose", "Encrypted": True, "Tags": [{"Key": "Name", "Value": "compartido"}]}]})
    st.add_response("describe_mount_targets", {"MountTargets": [{"MountTargetId": "fsmt-0123456789", "FileSystemId": "fs-1",
        "SubnetId": "subnet-0123456789", "LifeCycleState": "available", "VpcId": "vpc-1"}]}, {"FileSystemId": "fs-1"})
    st.add_response("describe_mount_target_security_groups", {"SecurityGroups": ["sg-0123456789"]}, {"MountTargetId": "fsmt-0123456789"})
    r = efs.collect(ses, REGION, CUENTA)
    _forma(r, "efs")
    mt, fs = r
    assert mt["relations"]["security_group_ids"] == ["sg-0123456789"] and mt["relations"]["attached_to"] == ["fs-1"]
    assert fs["name"] == "compartido" and fs["attrs"]["size_bytes"] == 2048 and fs["relations"]["vpc_id"] == "vpc-1"


def test_sqs(ses):
    st = ses.stub("sqs")
    url = f"https://sqs.us-east-1.amazonaws.com/{CUENTA}/cola-1"
    st.add_response("list_queues", {"QueueUrls": [url]})
    st.add_response("get_queue_attributes", {"Attributes": {"QueueArn": "arn:cola-1", "VisibilityTimeout": "30",
        "MessageRetentionPeriod": "345600", "RedrivePolicy": json.dumps({"deadLetterTargetArn": "arn:dlq", "maxReceiveCount": 5})}},
        {"QueueUrl": url, "AttributeNames": sqs._ATRIBUTOS})
    st.add_response("list_queue_tags", {"Tags": {"Environment": "dev"}})
    r = sqs.collect(ses, REGION, CUENTA)
    _forma(r, "sqs")
    assert "All" not in sqs._ATRIBUTOS and "Policy" not in sqs._ATRIBUTOS
    assert r[0]["id"] == "cola-1" and r[0]["attrs"]["dead_letter_target"] == "arn:dlq"
    assert r[0]["relations"]["target_ids"] == ["arn:dlq"] and r[0]["tags"] == {"Environment": "dev"}


def test_sns(ses):
    st = ses.stub("sns")
    arn = f"arn:aws:sns:us-east-1:{CUENTA}:alertas"
    st.add_response("list_topics", {"Topics": [{"TopicArn": arn}]})
    st.add_response("get_topic_attributes", {"Attributes": {"SubscriptionsConfirmed": "2", "SubscriptionsPending": "1",
        "Policy": '{"Statement":"persona@example.com"}'}})
    st.add_response("list_tags_for_resource", {"Tags": []})
    r = sns.collect(ses, REGION, CUENTA)
    _forma(r, "sns")
    assert r[0]["attrs"]["subscriptions_confirmed"] == 2 and r[0]["attrs"]["subscriptions_pending"] == 1
    assert "@" not in json.dumps(r, default=str)


def test_secretsmanager(ses, monkeypatch):
    st = ses.stub("secretsmanager")
    st.add_response("list_secrets", {"SecretList": [{"Name": "db/prod", "ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db/prod-AbC123", "Description": "password: hunter2",
        "RotationEnabled": True, "RotationRules": {"AutomaticallyAfterDays": 30}, "KmsKeyId": "k1", "LastAccessedDate": FECHA}]})
    cliente = ses.client("secretsmanager")
    monkeypatch.setattr(cliente, "get_secret_value", lambda **kw: pytest.fail("get_secret_value llamado"))
    r = secretsmanager.collect(ses, REGION, CUENTA)
    _forma(r, "secretsmanager")
    assert r[0]["attrs"]["rotation_enabled"] is True and r[0]["attrs"]["rotation_days"] == 30
    assert "hunter2" not in json.dumps(r, default=str)
    fuente = (AQUI / "colectores" / "secretsmanager.py").read_text()
    assert "get_secret_value(" not in fuente and ".get_secret_value" not in fuente


def test_kms(ses):
    st = ses.stub("kms")
    st.add_response("list_aliases", {"Aliases": [{"AliasName": "alias/app", "TargetKeyId": "k-c"},
                                                  {"AliasName": "alias/aws/s3", "TargetKeyId": "k-a"}]})
    st.add_response("list_keys", {"Keys": [{"KeyId": "k-a"}, {"KeyId": "k-c"}]})
    st.add_response("describe_key", {"KeyMetadata": {"KeyId": "k-a", "KeyManager": "AWS"}}, {"KeyId": "k-a"})
    st.add_response("describe_key", {"KeyMetadata": {"KeyId": "k-c", "Arn": "arn:aws:kms:us-east-1:123456789012:key/k-c", "KeyManager": "CUSTOMER",
        "KeyState": "Enabled", "KeyUsage": "ENCRYPT_DECRYPT"}}, {"KeyId": "k-c"})
    st.add_response("get_key_rotation_status", {"KeyRotationEnabled": True}, {"KeyId": "k-c"})
    st.add_response("list_resource_tags", {"Tags": [{"TagKey": "Owner", "TagValue": "infra"}]}, {"KeyId": "k-c"})
    r = kms.collect(ses, REGION, CUENTA)
    _forma(r, "kms")
    assert len(r) == 1 and r[0]["id"] == "k-c" and r[0]["name"] == "alias/app"
    assert r[0]["attrs"]["rotation_enabled"] is True and r[0]["tags"] == {"Owner": "infra"}


def test_backup(ses):
    st = ses.stub("backup")
    st.add_response("list_backup_vaults", {"BackupVaultList": [{"BackupVaultName": "Default", "NumberOfRecoveryPoints": 1500}]})
    st.add_response("list_backup_plans", {"BackupPlansList": [{"BackupPlanId": "p1", "BackupPlanName": "diario"}]})
    st.add_response("get_backup_plan", {"BackupPlan": {"BackupPlanName": "diario", "Rules": [{"RuleName": "r1",
        "TargetBackupVaultName": "Default", "ScheduleExpression": "cron(0 5 * * ? *)", "Lifecycle": {"DeleteAfterDays": 35}}]}},
        {"BackupPlanId": "p1"})
    r = backup.collect(ses, REGION, CUENTA)
    _forma(r, "backup")
    assert r[0]["attrs"]["recovery_points"] == 1500
    assert r[1]["attrs"]["rules"][0]["delete_after_days"] == 35 and r[1]["relations"]["target_ids"] == ["Default"]


def test_dms(ses):
    st = ses.stub("dms")
    st.add_response("describe_replication_instances", {"ReplicationInstances": [{"ReplicationInstanceIdentifier": "ri1",
        "ReplicationInstanceClass": "dms.t3.small", "ReplicationInstanceStatus": "available",
        "ReplicationSubnetGroup": {"VpcId": "vpc-1", "Subnets": [{"SubnetIdentifier": "subnet-1"}]},
        "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-1"}]}]})
    st.add_response("describe_replication_tasks", {"ReplicationTasks": [{"ReplicationTaskIdentifier": "tab1",
        "Status": "stopped", "MigrationType": "cdc", "ReplicationInstanceArn": "arn:ri1"}]}, {"WithoutSettings": True})
    r = dms.collect(ses, REGION, CUENTA)
    _forma(r, "dms")
    assert r[0]["relations"]["vpc_id"] == "vpc-1" and r[1]["relations"]["attached_to"] == ["arn:ri1"]


def test_glue(ses):
    st = ses.stub("glue")
    st.add_response("get_jobs", {"Jobs": [{"Name": "etl", "Role": "r", "Command": {"Name": "glueetl"},
        "DefaultArguments": {"--password": "hunter2"}}]})
    st.add_response("get_crawlers", {"Crawlers": [{"Name": "c1", "State": "READY"}]})
    st.add_response("get_databases", {"DatabaseList": [{"Name": "db1"}]})
    r = glue.collect(ses, REGION, CUENTA)
    _forma(r, "glue")
    assert [x["type"] for x in r] == ["job", "crawler", "database"]
    assert "hunter2" not in json.dumps(r, default=str)


def test_stepfunctions(ses):
    st = ses.stub("stepfunctions")
    st.add_response("list_state_machines", {"stateMachines": [{"stateMachineArn": "arn:sm", "name": "flujo",
        "type": "EXPRESS", "creationDate": FECHA}]})
    r = stepfunctions.collect(ses, REGION, CUENTA)
    _forma(r, "stepfunctions")
    assert r[0]["attrs"]["type"] == "EXPRESS"


def test_error_propaga(ses):
    # Un AccessDenied global del colector sube para que inventariar.py lo registre en errors.
    ses.stub("rds").add_client_error("describe_db_subnet_groups", "AccessDenied")
    with pytest.raises(ClientError):
        rds.collect(ses, REGION, CUENTA)

