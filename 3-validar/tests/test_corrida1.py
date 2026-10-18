"""Regresiones de la primera corrida real (org de 6 cuentas): regiones de edge de CloudFront,
regiones opt-in no habilitadas, categorías nuevas de aws-doctor, totales duplicados y reglas que
dependían de campos que los colectores no emitían. Sin llamadas a AWS.

Uso: .venv/bin/python -m pytest 3-validar/tests -q
"""

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.stub import Stubber

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent.parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(RAIZ / "1-descubrir"))

import validar  # noqa: E402  (agrega RAIZ y 2-inventariar al path)
import doctor  # noqa: E402
import reglas  # noqa: E402
import descubrir  # noqa: E402
import inventariar  # noqa: E402
from colectores import ec2 as col_ec2, sqs as col_sqs  # noqa: E402
from common import error_corto  # noqa: E402
from common.aws import regiones_habilitadas  # noqa: E402

CUENTA, REGION = "111122223333", "eu-west-1"
CFG = {"access_mode": "sso", "max_workers": 2}


class SesionFalsa:
    """boto3.Session.client con Stubber ya activado (como en 2-inventariar/tests)."""

    def __init__(self):
        self._base = boto3.Session(aws_access_key_id="x", aws_secret_access_key="x", region_name=REGION)
        self.clientes, self.stubs = {}, {}

    def stub(self, servicio):
        c = self._base.client(servicio, region_name=REGION)
        self.clientes[servicio] = c
        self.stubs[servicio] = Stubber(c)
        self.stubs[servicio].activate()
        return self.stubs[servicio]

    def client(self, servicio, region_name=None, config=None):
        return self.clientes[servicio]


# ---------- A1 · CloudFront va a "global" ----------

class CEFalso:
    """get_cost_and_usage mínimo: filtros REGION / SERVICE (incl. Not) y GroupBy de 2 claves como máximo."""

    def __init__(self, filas):
        # [(cuenta, region, servicio, usage_type, usd[, record_type])]; sin record_type = "Usage"
        self.filas = [f if len(f) == 6 else (*f, "Usage") for f in filas]
        self.llamadas = []

    def get_cost_and_usage(self, **p):
        claves = [g["Key"] for g in p["GroupBy"]]
        assert len(claves) <= 2, "Cost Explorer admite como máximo 2 GroupBy"
        self.llamadas.append(claves)
        region, excluidos, solo, rt_si, rt_no = None, set(), None, None, set()
        for c in p["Filter"].get("And", [p["Filter"]]):   # And necesita 2+ operandos: una sola condición va suelta
            d, n = c.get("Dimensions", {}), c.get("Not", {}).get("Dimensions", {})
            if d.get("Key") == "REGION":
                region = d["Values"][0]
            elif d.get("Key") == "SERVICE":
                solo = set(d["Values"])
            elif d.get("Key") == "RECORD_TYPE":
                rt_si = set(d["Values"])
            elif n.get("Key") == "SERVICE":
                excluidos = set(n["Values"])
            elif n.get("Key") == "RECORD_TYPE":
                rt_no = set(n["Values"])
        dim = {"LINKED_ACCOUNT": 0, "REGION": 1, "SERVICE": 2, "USAGE_TYPE": 3, "RECORD_TYPE": 5}
        grupos = {}
        for fila in self.filas:
            if (region and fila[1] != region) or fila[2] in excluidos or (solo and fila[2] not in solo):
                continue
            if (rt_si is not None and fila[5] not in rt_si) or fila[5] in rt_no:
                continue
            k = tuple(fila[dim[c]] for c in claves)
            grupos[k] = grupos.get(k, 0.0) + fila[4]
        return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-01"}, "Groups": [
            {"Keys": list(k), "Metrics": {"UnblendedCost": {"Amount": str(v)}}} for k, v in grupos.items()]}]}


def test_cloudfront_por_region_de_edge_va_a_global():
    ce = CEFalso([(CUENTA, "eu-west-1", "Amazon Elastic Compute Cloud - Compute", "EU-BoxUsage:t3.micro", 900.0),
                  (CUENTA, "eu-west-1", "Amazon CloudFront", "EU-DataTransfer-Out-Bytes", 50.0),
                  (CUENTA, "ap-south-1", "Amazon CloudFront", "IN-DataTransfer-Out-Bytes", 3.0),
                  (CUENTA, "me-south-1", "Amazon CloudFront", "ME-Requests-Tier1", 2.0)])
    cfg = {"billing_months": 1, "threshold_usd": 0.01, "include_accounts": []}
    disc, _, errores = descubrir.descubrir(ce, {CUENTA: "Prod"}, cfg, hoy=dt.date(2026, 10, 17))

    regiones = disc["accounts"][CUENTA]["regions"]
    assert set(regiones) == {"eu-west-1", "global"}                      # sin regiones de edge
    assert regiones["global"]["services"] == {"Amazon CloudFront": 55.0}
    assert "Amazon CloudFront" not in regiones["eu-west-1"]["services"]
    assert disc["errors"] == errores == []                               # E1: siempre presente
    assert descubrir.regiones_sorpresa(disc) == []

    # USAGE_TYPE: consulta aparte (máx. 2 GroupBy); los de CloudFront también van a "global".
    assert regiones["eu-west-1"]["usage_types_monthly_usd"] == {"EU-BoxUsage:t3.micro": {"2026-09": 900.0}}
    assert set(regiones["global"]["usage_types_monthly_usd"]) == {
        "EU-DataTransfer-Out-Bytes", "IN-DataTransfer-Out-Bytes", "ME-Requests-Tier1"}
    assert ["LINKED_ACCOUNT", "USAGE_TYPE"] in ce.llamadas


# ---------- A2 · regiones no habilitadas ----------

def test_regiones_habilitadas_con_stubber():
    s = SesionFalsa()
    s.stub("ec2").add_response("describe_regions", {"Regions": [{"RegionName": "us-east-1"},
                                                                 {"RegionName": "eu-west-1"}]}, {})
    assert regiones_habilitadas(s) == ["eu-west-1", "us-east-1"]


def test_filtrar_habilitadas_omite_opt_in_y_respeta_cuentas_sin_lista():
    mod = object()
    tareas = [(CUENTA, "eu-west-1", mod), (CUENTA, "me-south-1", mod), ("999", "me-south-1", mod)]
    validas, omitidas = inventariar.filtrar_habilitadas(tareas, {CUENTA: ["eu-west-1", "us-east-1"]})
    assert [t[:2] for t in validas] == [(CUENTA, "eu-west-1"), ("999", "me-south-1")]  # sin lista: no filtra
    assert [t[:2] for t in omitidas] == [(CUENTA, "me-south-1")]


def test_doctor_no_corre_en_regiones_no_habilitadas(tmp_path, monkeypatch):
    perfiles = tmp_path / "dive-config"
    perfiles.write_text("")
    import common.aws
    monkeypatch.setattr(common.aws, "DIVE_AWS_CONFIG", perfiles)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/aws-doctor")
    regiones = []
    monkeypatch.setattr(doctor.subprocess, "run", lambda cmd, **k: (
        regiones.append(cmd[cmd.index("--region") + 1]) or subprocess.CompletedProcess(cmd, 0, "{}", "")))
    disc = {"accounts": {CUENTA: {"regions": {"eu-west-1": {}, "me-south-1": {}, "global": {}}}}}
    doctor.ejecutar(CFG, disc, tmp_path, habilitadas={CUENTA: ["eu-west-1", "us-east-1"]})
    assert regiones == ["eu-west-1"]


# ---------- B · aws-doctor 2.x ----------

WASTE_V2 = {
    "account_id": CUENTA, "generated_at": "2026-10-17T00:00:00Z", "has_waste": True,
    "total_estimated_monthly_cost": 60.0,
    "summary": [{"category": "Idle Load Balancers", "count": 1, "estimated_monthly_cost": 18.4}],
    "errors": {"Lambda": "AccessDeniedException: StartQuery"},
    "idle_load_balancers": [{"name": "alb-x", "arn": "arn:aws:elasticloadbalancing:eu-west-1:1:loadbalancer/app/alb-x/1",
                             "type": "application", "days_checked": 7, "estimated_monthly_cost": 18.4}],
    "idle_nat_gateways": [{"nat_gateway_id": "nat-1", "vpc_id": "vpc-1", "estimated_monthly_cost": 32.4}],
    "idle_ec2_instances": [{"instance_id": "i-1", "instance_type": "t3.small", "estimated_monthly_cost": 15.0}],
    "over_provisioned_lambdas": [{"function_name": "fn-a", "configured_memory_mb": 1024, "recommended_memory_mb": 256}],
    "ecr_untagged_images": [{"repository_name": "repo-a", "untagged_image_count": 4, "estimated_monthly_cost": 1.5}],
    "ecr_no_lifecycle_policies": [{"repository_name": "repo-a"}],
    "ecr_empty_repositories": [{"repository_name": "repo-b"}],
    "unused_secrets": [{"name": "s-1", "estimated_monthly_cost": 0.4}],
    "unused_iam_users": [{"user_name": "u-1", "password_last_used": "Never"},
                         {"user_name": "u-2", "access_keys_status": "All 1 keys unused > 90 days"}],
    "root_user_waste": [{"has_mfa": False}],
    "categoria_del_futuro": [{"x": 1}],
}


def test_doctor_v2_categorias_nuevas_y_globales():
    items = doctor.items_desde_json(WASTE_V2)
    # El mismo JSON en dos regiones: IAM y root son globales y no se duplican; lo regional sí se repite.
    findings = doctor.items_a_findings({(CUENTA, "eu-west-1"): items, (CUENTA, "us-east-1"): items})
    titulos = [f["title"] for f in findings]

    root = [f for f in findings if f["title"] == "Usuario root sin MFA"]
    assert len(root) == 1 and root[0]["severity"] == "alta" and root[0]["region"] is None
    iam = [f for f in findings if f["title"].startswith("Usuarios IAM sin uso")]
    assert len(iam) == 1 and iam[0]["title"].endswith("(2)") and iam[0]["region"] is None
    assert titulos.count("NAT Gateway sin tráfico") == 2                 # regional: una por región
    for t in ["Load balancer sin tráfico", "Instancia EC2 ociosa (CPU y red bajas)",
              "Lambdas con memoria sobredimensionada (1)", "Repositorios ECR con imágenes sin tag (1)",
              "Repositorios ECR sin lifecycle policy (1)", "Repositorios ECR vacíos (1)",
              "Secretos sin acceso reciente (1)"]:
        assert t in titulos, t
    nat = next(f for f in findings if f["title"] == "NAT Gateway sin tráfico")
    assert nat["resource_id"] == "nat-1" and nat["monthly_usd"] == 32.4


def test_doctor_root_con_mfa_no_es_finding():
    items = doctor.items_desde_json({"root_user_waste": [{"has_mfa": True}]})
    assert items == []


def test_doctor_avisa_errores_y_categorias_desconocidas():
    avisos = doctor.avisos_desde_json(WASTE_V2)
    assert ("aws-doctor/Lambda", "AccessDeniedException: StartQuery") in avisos
    desconocidas = [t for c, t in avisos if "categoría desconocida" in t]
    assert desconocidas == ["categoría desconocida de aws-doctor: categoria_del_futuro (1); "
                            "actualiza CATEGORIAS en doctor.py"]        # summary/errors/etc. no cuentan


def test_doctor_ejecutar_registra_avisos_una_vez(tmp_path, monkeypatch):
    perfiles = tmp_path / "dive-config"
    perfiles.write_text("")
    import common.aws
    monkeypatch.setattr(common.aws, "DIVE_AWS_CONFIG", perfiles)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/aws-doctor")
    salida = json.dumps(WASTE_V2)
    monkeypatch.setattr(doctor.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, salida, ""))
    disc = {"accounts": {CUENTA: {"regions": {"eu-west-1": {}, "us-east-1": {}}}}}
    _, errores = doctor.ejecutar(CFG, disc, tmp_path)
    assert sum("categoría desconocida" in e["error"] for e in errores) == 1
    assert sum(e["collector"] == "aws-doctor/Lambda" for e in errores) == 2   # uno por región


# ---------- C2 · deduplicar con región ----------

def _f(cat, region, rid, usd, rule=None):
    return {"id": None, "category": cat, "severity": "baja", "account_id": CUENTA, "region": region,
            "resource_id": rid, "title": cat, "detail": "", "monthly_usd": usd, "source": "regla",
            "rule_id": rule or f"regla.{cat}"}


def test_deduplicar_usa_region_y_descarta_costo_de_un_desperdicio():
    findings = [_f("desperdicio", "eu-west-1", "vol-1", 10.0), _f("costo", "eu-west-1", "vol-1", 2.0),
                _f("costo", "us-east-1", "vol-1", 2.0),            # mismo id en otra región: es otro recurso
                _f("riesgo", "eu-west-1", "vol-1", None),
                _f("costo", None, None, 1.0, "regla.a"), _f("costo", None, None, 1.0, "regla.b")]  # agregados distintos
    salida = validar.deduplicar(findings)
    assert [(f["category"], f["region"]) for f in salida] == [
        ("desperdicio", "eu-west-1"), ("costo", "us-east-1"), ("riesgo", "eu-west-1"), ("costo", None), ("costo", None)]


# ---------- D · campos que las reglas esperaban ----------

def test_ec2_detenida_de_punta_a_punta_desde_el_colector():
    s = SesionFalsa()
    s.stub("ec2").add_response("describe_instances", {"Reservations": [{"Instances": [{
        "InstanceId": "i-0stop", "State": {"Name": "stopped"}, "InstanceType": "t3.micro",
        "StateTransitionReason": "User initiated (2026-01-31 10:00:00 GMT)",
        "LaunchTime": dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)}]}]}, {})
    recursos = col_ec2.collect(s, REGION, CUENTA)
    inv = json.loads(json.dumps({"generated_at": "2026-10-17T00:00:00Z", "resources": recursos}, default=str))
    detenidas = [f for f in reglas.aplicar(inv) if "detenida" in f["title"]]
    assert [f["resource_id"] for f in detenidas] == ["i-0stop"]


def test_snapshot_de_una_ami_registrada_no_es_viejo():
    viejo = {"start_time": "2024-01-01T00:00:00Z", "size_gb": 8}
    inv = {"generated_at": "2026-10-17T00:00:00Z", "resources": [
        {"account_id": CUENTA, "region": REGION, "service": "ebs", "type": "snapshot", "id": "snap-ami",
         "attrs": viejo, "relations": {}},
        {"account_id": CUENTA, "region": REGION, "service": "ebs", "type": "snapshot", "id": "snap-suelto",
         "attrs": viejo, "relations": {}},
        {"account_id": CUENTA, "region": REGION, "service": "ami", "type": "image", "id": "ami-1",
         "attrs": {"snapshot_ids": ["snap-ami"]}, "relations": {}}]}
    viejos = [f["resource_id"] for f in reglas.aplicar(inv) if f["title"].startswith("Snapshot EBS de más")]
    assert viejos == ["snap-suelto"]


def test_sqs_cola_borrada_a_mitad_no_tumba_el_colector():
    s = SesionFalsa()
    st = s.stub("sqs")
    base = "https://sqs.eu-west-1.amazonaws.com/111122223333/"
    st.add_response("list_queues", {"QueueUrls": [base + "borrada", base + "viva"]}, {})
    st.add_client_error("get_queue_attributes", service_error_code="AWS.SimpleQueueService.NonExistentQueue",
                        http_status_code=400)
    st.add_response("get_queue_attributes", {"Attributes": {"QueueArn": "arn:aws:sqs:eu-west-1:1:viva"}}, None)
    st.add_response("list_queue_tags", {"Tags": {}}, None)
    recursos = col_sqs.collect(s, REGION, CUENTA)
    assert [r["id"] for r in recursos] == ["viva"]


# ---------- G2 · errores sin datos del operador ----------

def test_error_corto_redacta_sesion_y_email():
    e = Exception("User: arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_DIVE_ReadOnly_abc/ana@empresa.pt "
                  "is not authorized to perform: ec2:DescribeX")
    texto = error_corto(e)
    assert "ana@" not in texto
    assert texto.startswith("Exception: User: arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_DIVE_ReadOnly_abc/<sesion> ")


# ---------- Regresión de la corrida 2: CloudFront en "global" dejó sin escanear us-east-1 ----------

class _Mod:
    def __init__(self, nombre, triggers, always=False, global_=False):
        self.NAME, self.TRIGGERS, self.ALWAYS, self.GLOBAL = nombre, triggers, always, global_


def test_costo_global_activa_colector_regional_en_us_east_1():
    acm = _Mod("acm", ["AWS Certificate Manager", "Amazon CloudFront"])
    vpc = _Mod("vpc", [], always=True)
    disc = {"accounts": {CUENTA: {"regions": {
        "eu-west-1": {"services": {"Amazon Elastic Compute Cloud - Compute": 10.0}},
        "global": {"services": {"Amazon CloudFront": 5.0}}}}}}
    tareas = {(a, r, m.NAME) for a, r, m in inventariar.planear(disc, [acm, vpc])}
    assert (CUENTA, "us-east-1", "acm") in tareas            # certificados de CloudFront
    assert (CUENTA, "us-east-1", "vpc") not in tareas        # ALWAYS no se extiende a us-east-1 por esto
    assert (CUENTA, "eu-west-1", "vpc") in tareas


def test_validar_pasa_regiones_habilitadas_al_doctor(tmp_path, monkeypatch):
    recibido = {}

    def falso_doctor(cfg, discovery, salida, timeout, creds_fn, habilitadas):
        recibido.update(habilitadas)
        return [], []

    monkeypatch.setattr(doctor, "ejecutar", falso_doctor)
    monkeypatch.setattr(doctor, "version", lambda: None)
    disc = {"generated_at": "x", "period": {"start": "2026-09-01", "end": "2026-10-01", "months": 1},
            "threshold_usd": 0.01, "accounts": {CUENTA: {"name": "P", "total_usd": 0, "regions": {}}}}
    inv = {"generated_at": "2026-10-17T00:00:00Z", "resources": [], "scanned": [], "errors": [],
           "accounts": {CUENTA: {"name": "P", "enabled_regions": ["eu-west-1"]}, "999": {"name": "Q"}}}
    validar.validar(CFG, disc, inv, tmp_path)
    assert recibido == {CUENTA: ["eu-west-1"]}


def test_region_omitida_no_genera_no_inventariado():
    rds = _Mod("rds", ["Amazon Relational Database Service"])
    disc = {"period": {"start": "2026-09-01", "end": "2026-10-01", "months": 1}, "accounts": {CUENTA: {
        "name": "P", "total_usd": 100.0, "regions": {
            "me-south-1": {"total_usd": 50.0, "services": {"Amazon Relational Database Service": 50.0}},
            "eu-west-1": {"total_usd": 50.0, "services": {"Amazon Relational Database Service": 50.0}}}}}}
    inv = {"resources": [], "scanned": [], "errors": [],
           "skipped": [{"account_id": CUENTA, "region": "me-south-1", "reason": "region_not_enabled"}]}
    regiones = [f["region"] for f in validar.cruce_billing(disc, inv, [rds]) if "no está inventariado" in f["title"]]
    assert regiones == ["eu-west-1"]


# ---------- Scan de seguridad del diff ----------

def test_redactar_usuario_iam_email_codificado_y_saltos():
    from common import redactar
    t = redactar("User: arn:aws:iam::111122223333:user/ana.silva is not authorized\nmail ana%40empresa.pt")
    assert "ana" not in t and ":user/<usuario>" in t and "\n" not in t


def test_avisos_de_aws_doctor_se_redactan():
    datos = {"errors": {"EC2": "AccessDenied: assumed-role/AWSReservedSSO_X/ana@empresa.pt not authorized"}}
    (colector, texto), = doctor.avisos_desde_json(datos)
    assert colector == "aws-doctor/EC2" and "ana@" not in texto and "<sesion>" in texto


def test_doctor_formas_inesperadas_no_rompen():
    datos = {"unused_ebs_volumes": {"no": "es lista"}, "root_user_waste": [{"has_mfa": None}], "errors": ["x"]}
    assert doctor.items_desde_json(datos) == []
    textos = [t for _, t in doctor.avisos_desde_json(datos)]
    assert any("forma inesperada en unused_ebs_volumes" in t for t in textos)
    assert any("has_mfa" in t for t in textos)


# ---------- P1 · tags, root ----------

def _r(service, type_, id_, region=REGION, tags=None, attrs=None):
    return {"account_id": CUENTA, "region": region, "service": service, "type": type_, "id": id_,
            "tags": tags or {}, "attrs": attrs or {}, "relations": {}}


def test_sin_tags_ignora_colectores_sin_lectura_y_no_colisiona_ids():
    recursos = [_r("logs", "log_group", "/app"),                         # logs: TAGS = False
                _r("sqs", "queue", "cola", region="eu-west-1"),
                _r("sqs", "queue", "cola", region="us-east-1"),          # mismo id, otra región
                _r("sqs", "queue", "ok", tags={"owner": "a", "env": "prod"})]
    f, = reglas.regla_sin_tags(recursos, {})
    assert f["title"].startswith("2 de 3") and "sqs=2" in f["detail"]
    assert "1 recursos de servicios cuyos tags no se leen" in f["detail"]
    assert "logs" in reglas.servicios_sin_lectura_de_tags() and "iam" in reglas.servicios_sin_lectura_de_tags()


def test_colector_iam_y_reglas_de_root_de_punta_a_punta():
    from colectores import iam as col_iam
    s = SesionFalsa()
    s.stub("iam").add_response("get_account_summary", {"SummaryMap": {
        "AccountMFAEnabled": 0, "AccountAccessKeysPresent": 1, "AccountPasswordPresent": 1, "Users": 3}}, {})
    recursos = col_iam.collect(s, "us-east-1", CUENTA)
    assert recursos[0]["attrs"] == {"mfa_enabled": False, "access_keys_present": True,
                                    "password_present": True, "users": 3}
    root = {f["resource_id"]: f for f in reglas.regla_root(recursos, {})}
    assert set(root) == {"root", "root-access-keys"}
    assert all(f["region"] is None and f["severity"] == "alta" for f in root.values())

    # Con el mismo finding de aws-doctor (region None, resource "root"), queda uno solo de MFA.
    doc = doctor.items_a_findings({(CUENTA, "eu-west-1"): doctor.items_desde_json(
        {"root_user_waste": [{"has_mfa": False}]})})
    finales = validar.deduplicar(doc + list(root.values()))
    assert sorted(f["resource_id"] for f in finales) == ["root", "root-access-keys"]


def test_root_con_mfa_y_sin_keys_no_genera_nada():
    r = _r("iam", "account_summary", CUENTA, region="global",
           attrs={"mfa_enabled": True, "access_keys_present": False, "password_present": True})
    assert reglas.regla_root([r], {}) == []


def test_reporte_tipos_de_uso_del_ultimo_mes():
    sys.path.insert(0, str(RAIZ / "4-esquematizar"))
    import reporte
    disc = {"accounts": {
        "A": {"regions": {"eu-west-1": {"usage_types_monthly_usd": {
            "EU-NatGateway-Bytes": {"2026-08": 50.0, "2026-09": 80.0},
            "EU-BoxUsage:t3.micro": {"2026-09": 10.0}}}}},
        "B": {"regions": {"global": {"usage_types_monthly_usd": {"EU-NatGateway-Bytes": {"2026-09": 20.0},
                                                                  "Viejo": {"2026-08": 99.0}}}}}}}
    ut = reporte.tipos_de_uso(disc)
    assert ut["month"] == "2026-09" and ut["previous"] == "2026-08"
    assert [f["usage_type"] for f in ut["rows"]] == ["EU-NatGateway-Bytes", "EU-BoxUsage:t3.micro"]  # "Viejo": 0 hoy
    nat = ut["rows"][0]
    assert nat["usd"] == 100.0 and nat["previous_usd"] == 50.0
    assert nat["accounts"] == [{"account_id": "A", "usd": 80.0}, {"account_id": "B", "usd": 20.0}]
    assert reporte.tipos_de_uso({}) == {"month": None, "previous": None, "rows": []}


# ---------- Contrato v2 ----------

def test_huella_estable_y_canonica_entre_fuentes():
    from hallazgos import completar_v2, hallazgo
    regla = hallazgo("desperdicio", "media", CUENTA, REGION, "vol-1", "Volumen sin adjuntar", "x", 10.0, "regla",
                     rule_id="regla.ebs_sin_adjuntar")
    doc = hallazgo("desperdicio", "baja", CUENTA, REGION, "vol-1", "Otro título", "y", 11.5, "aws-doctor",
                   rule_id="aws-doctor.unused_ebs_volumes")
    otra_region = hallazgo("desperdicio", "media", CUENTA, "us-east-1", "vol-1", "t", "d", 10.0, "regla",
                           rule_id="regla.ebs_sin_adjuntar")
    a, b, c = (completar_v2(dict(f)) for f in (regla, doc, otra_region))
    assert a["issue_id"] == b["issue_id"] == "ebs.unattached"
    assert a["fingerprint"] == b["fingerprint"]                  # misma identidad con o sin aws-doctor
    assert a["fingerprint"] != c["fingerprint"]
    assert (a["price_source"], b["price_source"]) == ("static_estimate", "aws_doctor") and a["is_saving"]


def test_rds_publico_y_sin_cifrar_no_se_pierde_ninguno():
    r = {"account_id": CUENTA, "region": REGION, "service": "rds", "type": "db_instance", "id": "db1", "tags": {},
         "relations": {}, "attrs": {"publicly_accessible": True, "storage_encrypted": False}}
    f = validar.deduplicar(reglas.aplicar({"generated_at": "2026-10-18T00:00:00Z", "resources": [r]}))
    titulos = {x["title"] for x in f if x["resource_id"] == "db1"}
    assert titulos == {"Base de datos RDS con acceso público", "Base de datos RDS sin cifrado en reposo"}


def test_cruce_discrimina_por_servicio():
    from hallazgos import completar_v2, hallazgo
    a = completar_v2(hallazgo("inventario", "baja", CUENTA, REGION, None, "t", "d", 1.0, "cruce-billing",
                              rule_id="cruce.no_inventariado", discriminador="Amazon RDS"))
    b = completar_v2(hallazgo("inventario", "baja", CUENTA, REGION, None, "t", "d", 1.0, "cruce-billing",
                              rule_id="cruce.no_inventariado", discriminador="AWS Lambda"))
    assert a["fingerprint"] != b["fingerprint"] and "_discriminador" not in a


def test_validar_completo_fingerprints_unicos_y_run(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "version", lambda: None)
    fix = AQUI / "fixtures"
    disc = json.loads((fix / "discovery.json").read_text())
    inv = json.loads((fix / "inventory.json").read_text())
    inv["run"] = {"run_id": "2026-10-18T000000Z", "org_alias": "o-x", "flags": {"inventariar": {}}}
    findings, _ = validar.validar(CFG, disc, inv, tmp_path, sin_doctor=True)
    huellas = [f["fingerprint"] for f in findings]
    assert len(huellas) == len(set(huellas)) and all(f["rule_id"] for f in findings)
    run = json.loads((tmp_path / "findings.json").read_text())["run"]
    assert run["run_id"] == "2026-10-18T000000Z" and run["flags"]["validar"]["sin_doctor"] is True
    assert "inventariar" in run["flags"]


# ---------- Contrato v2 · carpetas de corrida ----------

def test_nueva_corrida_y_corrida_actual(tmp_path, monkeypatch):
    from common import corrida_actual, heredar_run, marcar_ultima, nueva_corrida
    cfg = {"output_dir": str(tmp_path)}
    assert corrida_actual(cfg).name == dt.datetime.now().strftime("%Y-%m-%d")  # sin ULTIMA: corridas viejas
    c1 = nueva_corrida(cfg)
    assert corrida_actual(cfg) != c1                     # ULTIMA recién se marca con discovery.json escrito
    marcar_ultima(c1)
    assert c1.is_dir() and corrida_actual(cfg) == c1 and (tmp_path / "ULTIMA").read_text().strip() == c1.name
    for malo in ("../otra", "a/b"):
        (tmp_path / "ULTIMA").write_text(malo)
        assert corrida_actual(cfg).name == dt.datetime.now().strftime("%Y-%m-%d")
    assert dt.datetime.strptime(c1.name, "%Y-%m-%dT%H%M%SZ")
    # Otra organización en el mismo output_dir: la fase siguiente aborta.
    import pytest
    with pytest.raises(SystemExit):
        heredar_run({"run": {"org_alias": "otra", "organization_id": "o-1"}}, {"org_alias": "esta"}, "inventariar", {})
    # Corrida sin alias (org_alias = organization_id): agregar el alias al config después no la bloquea.
    heredar_run({"run": {"org_alias": "o-1", "organization_id": "o-1"}}, {"org_alias": "esta"}, "inventariar", {})
    assert heredar_run({"run": {"org_alias": "esta", "flags": {"a": 1}}}, {"org_alias": "esta"}, "b", {"x": 2})[
        "flags"] == {"a": 1, "b": {"x": 2}}


def test_un_ahorro_por_recurso_entre_issues_distintos():
    from hallazgos import hallazgo
    doc = hallazgo("desperdicio", "baja", CUENTA, REGION, "snap-1", "huérfano", "d", 2.0, "aws-doctor",
                   rule_id="aws-doctor.orphaned_snapshots")
    regla = hallazgo("desperdicio", "baja", CUENTA, REGION, "snap-1", "viejo", "d", 2.0, "regla",
                     rule_id="regla.snapshots_viejos")
    riesgo = hallazgo("riesgo", "baja", CUENTA, REGION, "snap-1", "público", "d", None, "regla", rule_id="regla.x")
    costo_antes = hallazgo("costo", "baja", CUENTA, REGION, "vol-1", "gp2", "d", 1.0, "regla", rule_id="regla.gp2")
    desp_despues = hallazgo("desperdicio", "media", CUENTA, REGION, "vol-1", "sin adjuntar", "d", 8.0, "regla",
                            rule_id="regla.ebs_sin_adjuntar")
    salida = validar.deduplicar([doc, regla, riesgo, costo_antes, desp_despues])
    assert [f["rule_id"] for f in salida] == ["aws-doctor.orphaned_snapshots", "regla.x", "regla.ebs_sin_adjuntar"]
    assert sum(f["monthly_usd"] or 0 for f in salida if f["is_saving"]) == 10.0


def test_config_exige_cuentas_como_texto(tmp_path):
    import pytest
    from common import cargar_config
    malo = tmp_path / "malo.yaml"
    malo.write_text("exclude_accounts: [012345670123]\n")          # YAML: octal → otro número
    with pytest.raises(SystemExit):
        cargar_config(malo)
    bueno = tmp_path / "bueno.yaml"
    bueno.write_text('exclude_accounts: ["012345670123"]\n')
    assert cargar_config(bueno)["exclude_accounts"] == ["012345670123"]


def test_cargos_fuera_del_uso_impuestos_y_savings_plans():
    ce = CEFalso([(CUENTA, "eu-west-1", "Amazon EC2", "EU-BoxUsage", 100.0),
                  (CUENTA, "eu-west-1", "Amazon EC2", "EU-BoxUsage", 40.0, "SavingsPlanCoveredUsage"),
                  (CUENTA, "eu-west-1", "Amazon EC2", "EU-BoxUsage", -40.0, "SavingsPlanNegation"),
                  (CUENTA, "global", "Savings Plans for AWS Compute usage", "x", 30.0, "SavingsPlanRecurringFee"),
                  (CUENTA, "global", "Tax", "x", 23.0, "Tax")])
    cfg = {"billing_months": 1, "threshold_usd": 0.01, "include_accounts": []}
    disc, _, errores = descubrir.descubrir(ce, {CUENTA: "Prod"}, cfg, hoy=dt.date(2026, 10, 17))
    assert errores == []
    assert disc["charges_monthly_usd"][CUENTA] == {"SavingsPlanNegation": {"2026-09": -40.0},
                                                   "SavingsPlanRecurringFee": {"2026-09": 30.0},
                                                   "Tax": {"2026-09": 23.0}}
    uso = sum(disc["accounts"][CUENTA]["regions"]["eu-west-1"]["services"].values())
    assert uso == 140.0                                     # uso a precio de lista (incluye lo cubierto por el SP)
    assert uso + sum(v["2026-09"] for v in disc["charges_monthly_usd"][CUENTA].values()) == 153.0  # factura real
