"""Pruebas de la fase 3 con datos sintéticos. No llaman a AWS ni ejecutan aws-doctor.

Uso: .venv/bin/python -m pytest 3-validar/tests -q
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parent
VALIDAR = AQUI.parent
sys.path.insert(0, str(VALIDAR))

import validar  # noqa: E402  (agrega RAIZ y 2-inventariar al path)
import doctor  # noqa: E402
import reglas  # noqa: E402

FIX = AQUI / "fixtures"
CFG = {"access_mode": "sso", "max_workers": 4}


def cargar(nombre):
    return json.loads((FIX / nombre).read_text())


def por_titulo(findings, texto):
    return [f for f in findings if texto in f["title"]]


# ---------- c) reglas ----------

@pytest.fixture
def hallazgos_reglas():
    return reglas.aplicar(cargar("inventory.json"))


def test_ebs_y_gp2(hallazgos_reglas):
    ebs = por_titulo(hallazgos_reglas, "Volumen EBS sin adjuntar")
    assert [f["resource_id"] for f in ebs] == ["vol-0001"]
    assert ebs[0]["monthly_usd"] == 10.0 and ebs[0]["severity"] == "media"
    gp2 = por_titulo(hallazgos_reglas, "gp2")
    assert [f["resource_id"] for f in gp2] == ["vol-0001"] and gp2[0]["monthly_usd"] == 2.0


def test_eip_snapshot_ec2(hallazgos_reglas):
    assert [f["resource_id"] for f in por_titulo(hallazgos_reglas, "Elastic IP")] == ["eipalloc-0001"]
    assert [f["resource_id"] for f in por_titulo(hallazgos_reglas, "Snapshot EBS")] == ["snap-0001"]
    assert [f["resource_id"] for f in por_titulo(hallazgos_reglas, "detenida")] == ["i-0bbb"]


def test_security_groups(hallazgos_reglas):
    sgs = {f["resource_id"]: f for f in por_titulo(hallazgos_reglas, "Security group")}
    assert set(sgs) == {"sg-0001", "sg-0003"}          # sg-0002 solo abre 80/443
    assert sgs["sg-0001"]["severity"] == "alta"          # 22 al mundo
    assert sgs["sg-0003"]["severity"] == "media"         # 8080, derivado de attrs.ingress
    assert "443" not in sgs["sg-0001"]["detail"]


def test_rds_opensearch_s3(hallazgos_reglas):
    assert [f["resource_id"] for f in por_titulo(hallazgos_reglas, "RDS con acceso público")] == ["db-dev"]
    # db-miembro pertenece a un cluster: el cifrado se evalúa en el cluster
    assert [f["resource_id"] for f in por_titulo(hallazgos_reglas, "sin cifrado")] == ["db-dev"]
    os_ = por_titulo(hallazgos_reglas, "OpenSearch")
    assert os_[0]["resource_id"] == "logs-dom" and os_[0]["severity"] == "alta"
    assert [f["resource_id"] for f in por_titulo(hallazgos_reglas, "Bucket S3")] == ["bucket-a"]


def test_tags_agregado_por_cuenta(hallazgos_reglas):
    tags = por_titulo(hallazgos_reglas, "sin tag")
    assert [f["account_id"] for f in tags] == ["111111111111"]   # la cuenta de dev está toda tageada
    # 1 recurso es de un colector que no lee tags (TAGS = False): queda fuera del conteo y se informa.
    assert tags[0]["resource_id"] is None and tags[0]["title"].startswith("11 de 13")
    assert "1 recursos de servicios cuyos tags no se leen" in tags[0]["detail"]


# ---------- b) cruce billing ----------

@pytest.fixture
def hallazgos_cruce():
    return validar.cruce_billing(cargar("discovery.json"), cargar("inventory.json"), validar.cargar_colectores())


def test_se_cobra_sin_inventario(hallazgos_cruce):
    no_inv = por_titulo(hallazgos_cruce, "no está inventariado")
    servicios = {(f["region"], f["title"].split(": ")[1]) for f in no_inv}
    assert ("us-east-1", "Amazon Relational Database Service") in servicios
    assert ("eu-west-1", "AWS Key Management Service") in servicios
    rds = next(f for f in no_inv if "Relational" in f["title"])
    assert "AccessDenied" in rds["detail"] and rds["monthly_usd"] == 50.0 and rds["severity"] == "media"
    # S3 es GLOBAL y tiene buckets en la cuenta; EC2 tiene instancias
    assert not any("Simple Storage" in f["title"] or "Compute" in f["title"] for f in no_inv)


def test_servicio_sin_colector(hallazgos_cruce):
    titulos = {f["title"] for f in por_titulo(hallazgos_cruce, "sin colector")}
    assert "Servicio sin colector: Amazon Simple Email Service" in titulos
    assert not any("Tax" in t for t in titulos)  # cargo de facturación, no recurso


def test_region_sorpresa(hallazgos_cruce):
    sorpresa = por_titulo(hallazgos_cruce, "Región sorpresa")
    assert [(f["account_id"], f["region"]) for f in sorpresa] == [("111111111111", "eu-west-1")]


# ---------- a) aws-doctor ----------

def test_parsear_json():
    items, formato = doctor.parsear_salida((FIX / "doctor_waste.json").read_text())
    assert formato == "json"
    ids = {(i["clave"], i["id"]) for i in items}
    assert ("unused_ebs_volumes", "vol-0001") in ids
    assert ("unused_elastic_ips", "eipalloc-0001") in ids
    assert ("idle_rds_instances", "db-x") in ids
    assert next(i for i in items if i["id"] == "db-x")["usd"] == 49.76


def test_parsear_texto_con_celdas_combinadas():
    items, formato = doctor.parsear_salida((FIX / "doctor_waste.txt").read_text())
    assert formato == "texto"
    por_clave = {}
    for i in items:
        por_clave.setdefault(i["clave"], []).append(i["id"])
    assert por_clave["unused_ebs_volumes"] == ["vol-0aa1", "vol-0aa2", "vol-0aa3"]
    assert por_clave["stopped_instance_volumes"] == ["vol-0bb1"]
    assert por_clave["unused_elastic_ips"] == ["eipalloc-0c1"]      # prefiere el ID sobre la IP
    assert por_clave["s3_buckets_without_lifecycle"] == ["bucket-a", "bucket-b"]
    assert por_clave["stopped_instances"] == ["i-0dd1"]
    assert por_clave["idle_rds_instances"] == ["mysql-1"]
    assert next(i for i in items if i["id"] == "mysql-1")["usd"] == 149.76
    assert "Waste Summary" not in str(items)


def test_items_a_findings_agrupa_y_deduplica():
    items, _ = doctor.parsear_salida((FIX / "doctor_waste.json").read_text())
    # Mismo JSON en dos regiones: S3 (global) no debe duplicarse; los log groups sí son por región.
    findings = doctor.items_a_findings({("111111111111", "us-east-1"): items, ("111111111111", "eu-west-1"): items})
    s3 = por_titulo(findings, "Buckets S3 sin lifecycle")
    assert len(s3) == 1 and s3[0]["region"] is None and s3[0]["title"].endswith("(2)")
    logs = por_titulo(findings, "Log groups")
    assert len(logs) == 2 and logs[0]["monthly_usd"] == 0.5
    rds = por_titulo(findings, "RDS sin conexiones")
    assert rds[0]["severity"] == "media" and all(f["source"] == "aws-doctor" for f in findings)


def test_ejecutar_con_subprocess_mockeado(tmp_path, monkeypatch):
    perfiles = tmp_path / "dive-config"
    perfiles.write_text("[profile dive-111111111111]\n")
    import common.aws
    monkeypatch.setattr(common.aws, "DIVE_AWS_CONFIG", perfiles)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/aws-doctor")
    salida_json = (FIX / "doctor_waste.json").read_text()
    llamadas = []

    def falso_run(cmd, capture_output, text, env, timeout):
        llamadas.append((cmd, env))
        region = cmd[cmd.index("--region") + 1]
        if cmd[cmd.index("--profile") + 1] == "dive-222222222222":
            return subprocess.CompletedProcess(cmd, 1, "", "Error: token SSO expirado")
        return subprocess.CompletedProcess(cmd, 0, salida_json if region == "us-east-1" else "{}", "")

    monkeypatch.setattr(doctor.subprocess, "run", falso_run)
    findings, errores = doctor.ejecutar(CFG, cargar("discovery.json"), tmp_path)

    cmds = sorted(" ".join(c[2:]) for c, _ in llamadas)
    assert len(llamadas) == 3                                   # 2 regiones de prod + 1 de dev, sin "global"
    assert "--output json --region us-east-1 --profile dive-111111111111" in cmds
    assert all(env["AWS_CONFIG_FILE"] == str(perfiles) for _, env in llamadas)
    assert (tmp_path / "aws-doctor" / "111111111111_us-east-1.json").exists()
    assert (tmp_path / "aws-doctor" / "222222222222_us-east-1.txt").exists()   # cruda + stderr de respaldo
    assert [e["account_id"] for e in errores] == ["222222222222"] and "SSO" in errores[0]["error"]
    assert por_titulo(findings, "Volumen EBS sin adjuntar")


def test_sin_aws_doctor_instalado(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    findings, errores = doctor.ejecutar(CFG, cargar("discovery.json"), tmp_path)
    assert findings == [] and "no instalado" in errores[0]["error"]


# ---------- de punta a punta ----------

def test_validar_completo(tmp_path, monkeypatch):
    def falso_doctor(cfg, discovery, salida, timeout, creds_fn, habilitadas):
        items, _ = doctor.parsear_salida((FIX / "doctor_waste.json").read_text())
        return doctor.items_a_findings({("111111111111", "us-east-1"): items}), []

    monkeypatch.setattr(doctor, "ejecutar", falso_doctor)
    monkeypatch.setattr(doctor, "version", lambda: "aws-doctor version 0.0-test")  # sin ejecutar el binario
    findings, _ = validar.validar(CFG, cargar("discovery.json"), cargar("inventory.json"), tmp_path)

    datos = json.loads((tmp_path / "findings.json").read_text())
    assert set(datos) >= {"generated_at", "findings"}
    claves = {"id", "category", "severity", "account_id", "region", "resource_id", "title", "detail",
              "rule_id", "issue_id", "fingerprint", "price_source", "is_saving",
              "monthly_usd", "source"}
    assert all(set(f) == claves for f in datos["findings"])
    assert {f["source"] for f in findings} == {"aws-doctor", "cruce-billing", "regla"}

    # Orden: severidad y luego $ descendente; IDs consecutivos.
    orden = ["alta", "media", "baja"]
    assert [orden.index(f["severity"]) for f in findings] == sorted(orden.index(f["severity"]) for f in findings)
    assert findings[0]["id"] == "F001"

    # vol-0001 lo marcan aws-doctor y la regla (desperdicio): queda uno solo, el de aws-doctor.
    vol = [f for f in findings if f["resource_id"] == "vol-0001" and f["category"] == "desperdicio"]
    assert len(vol) == 1 and vol[0]["source"] == "aws-doctor"
    # gp2→gp3 (costo) sobre un volumen que ya es desperdicio se descarta: el ahorro no se cuenta dos veces.
    assert not any(f["resource_id"] == "vol-0001" and f["category"] == "costo" for f in findings)

    md = (tmp_path / "findings.md").read_text()
    assert "## Severidad alta" in md and "Produccion (111111111111)" in md and "sg-0001" in md


def test_sin_doctor_no_llama_subprocess(tmp_path, monkeypatch):
    def prohibido(*a, **k):
        raise AssertionError("no debe ejecutar aws-doctor")

    monkeypatch.setattr(doctor.subprocess, "run", prohibido)
    findings, errores = validar.validar(CFG, cargar("discovery.json"), cargar("inventory.json"), tmp_path,
                                        sin_doctor=True)
    assert findings and not errores and all(f["source"] != "aws-doctor" for f in findings)
