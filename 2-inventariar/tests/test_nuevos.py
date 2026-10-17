"""Pruebas de los colectores agregados tras medir cobertura (alarmas, logs, stacks, SSM, EventBridge, flow logs...)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

AQUI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from colectores import appautoscaling, cloudformation, cloudwatch, eventbridge, flowlogs, logs, sns, ssm  # noqa: E402
from test_datos import CLAVES, CUENTA, REGION, SesionFalsa  # noqa: E402

FECHA = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _contrato(recursos):
    for r in recursos:
        assert set(r) == CLAVES
        json.dumps(r, default=str)
    return recursos


def test_cloudwatch_alarmas_ligadas_al_recurso():
    s = SesionFalsa()
    s.stub("cloudwatch").add_response("describe_alarms", {
        "MetricAlarms": [{"AlarmName": "cpu-alta", "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{CUENTA}:alarm:cpu-alta",
                          "StateValue": "OK", "Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
                          "Dimensions": [{"Name": "InstanceId", "Value": "i-0abc"}],
                          "AlarmActions": [f"arn:aws:sns:{REGION}:{CUENTA}:alertas"], "ActionsEnabled": True}],
        "CompositeAlarms": [],
    }, {"AlarmTypes": ["MetricAlarm", "CompositeAlarm"]})
    (r,) = _contrato(cloudwatch.collect(s, REGION, CUENTA))
    assert r["relations"]["attached_to"] == ["i-0abc"]
    assert r["attrs"]["has_actions"] is True


def test_logs_sin_retencion_es_none():
    s = SesionFalsa()
    s.stub("logs").add_response("describe_log_groups", {"logGroups": [
        {"logGroupName": "/ecs/app", "arn": f"arn:aws:logs:{REGION}:{CUENTA}:log-group:/ecs/app:*", "storedBytes": 1024 ** 3},
    ]}, {})
    (r,) = _contrato(logs.collect(s, REGION, CUENTA))
    assert r["arn"].endswith("log-group:/ecs/app")  # sin ":*", igual que la Tagging API
    assert r["attrs"]["retention_days"] is None and r["attrs"]["stored_gb"] == 1.0


def test_cloudformation_no_lee_parametros():
    s = SesionFalsa()
    st = s.stub("cloudformation")
    st.add_response("list_stacks", {"StackSummaries": [
        {"StackName": "red", "StackId": f"arn:aws:cloudformation:{REGION}:{CUENTA}:stack/red/abc",
         "StackStatus": "UPDATE_COMPLETE", "CreationTime": FECHA}]}, {"StackStatusFilter": cloudformation._ESTADOS_VIVOS})
    st.add_client_error("list_stack_sets", "AccessDenied")
    (r,) = _contrato(cloudformation.collect(s, REGION, CUENTA))
    assert r["state"] == "UPDATE_COMPLETE"
    assert "describe_stacks" not in Path(cloudformation.__file__).read_text().split('"""', 2)[2]


def test_ssm_nunca_lee_valores():
    s = SesionFalsa()
    st = s.stub("ssm")
    st.add_response("describe_parameters", {"Parameters": [
        {"Name": "/app/db/password", "Type": "SecureString", "KeyId": "alias/aws/ssm", "Version": 3}]}, {})
    st.add_response("list_documents", {"DocumentIdentifiers": [{"Name": "parche", "DocumentType": "Command"}]},
                    {"Filters": [{"Key": "Owner", "Values": ["Self"]}]})
    recursos = _contrato(ssm.collect(s, REGION, CUENTA))
    assert recursos[0]["arn"] == f"arn:aws:ssm:{REGION}:{CUENTA}:parameter/app/db/password"
    assert "value" not in json.dumps(recursos).lower()
    codigo = Path(ssm.__file__).read_text().split('"""', 2)[2]
    assert "get_parameter" not in codigo


def test_eventbridge_solo_arn_de_targets():
    s = SesionFalsa()
    st = s.stub("events")
    st.add_response("list_event_buses", {"EventBuses": [{"Name": "default", "Arn": "arn:bus"}]}, {})
    st.add_response("list_rules", {"Rules": [
        {"Name": "nocturno", "Arn": f"arn:aws:events:{REGION}:{CUENTA}:rule/nocturno", "State": "ENABLED",
         "ScheduleExpression": "cron(0 2 * * ? *)"}]}, {"EventBusName": "default"})
    st.add_response("list_targets_by_rule", {"Targets": [
        {"Id": "1", "Arn": f"arn:aws:lambda:{REGION}:{CUENTA}:function:job", "Input": '{"token":"secreto"}'}]},
        {"Rule": "nocturno", "EventBusName": "default"})
    (r,) = _contrato(eventbridge.collect(s, REGION, CUENTA))
    assert r["relations"]["target_ids"] == [f"arn:aws:lambda:{REGION}:{CUENTA}:function:job"]
    assert "secreto" not in json.dumps(r)


def test_flowlogs_ligado_a_la_vpc():
    s = SesionFalsa()
    s.stub("ec2").add_response("describe_flow_logs", {"FlowLogs": [
        {"FlowLogId": "fl-1", "ResourceId": "vpc-1", "TrafficType": "ALL", "LogDestinationType": "s3",
         "LogDestination": "arn:aws:s3:::logs", "FlowLogStatus": "ACTIVE"}]}, {})
    (r,) = _contrato(flowlogs.collect(s, REGION, CUENTA))
    assert r["relations"]["attached_to"] == ["vpc-1"] and r["attrs"]["traffic_type"] == "ALL"


def test_appautoscaling_recorre_namespaces():
    s = SesionFalsa()
    st = s.stub("application-autoscaling")
    for ns in appautoscaling._NAMESPACES:
        objetivos = [{"ServiceNamespace": "ecs", "ResourceId": "service/c/api", "ScalableDimension": "ecs:service:DesiredCount",
                      "MinCapacity": 1, "MaxCapacity": 4, "RoleARN": "arn:rol", "CreationTime": FECHA}] if ns == "ecs" else []
        st.add_response("describe_scalable_targets", {"ScalableTargets": objetivos}, {"ServiceNamespace": ns})
    (r,) = _contrato(appautoscaling.collect(s, REGION, CUENTA))
    assert r["relations"]["attached_to"] == ["service/c/api"] and r["attrs"]["max"] == 4


def test_sns_topico_con_acceso_denegado_igual_se_registra():
    s = SesionFalsa()
    st = s.stub("sns")
    arn = f"arn:aws:sns:{REGION}:{CUENTA}:privado"
    st.add_response("list_topics", {"Topics": [{"TopicArn": arn}]}, {})
    st.add_client_error("get_topic_attributes", "AuthorizationError")
    st.add_response("list_tags_for_resource", {"Tags": []}, {"ResourceArn": arn})
    (r,) = _contrato(sns.collect(s, REGION, CUENTA))
    assert r["id"] == "privado" and r["attrs"]["attributes_error"] == "AuthorizationError"


assert ClientError  # usado indirectamente por add_client_error
