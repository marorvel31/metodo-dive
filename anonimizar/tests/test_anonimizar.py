"""Pruebas del anonimizador con fixtures sintéticas (sin AWS).

Correr: .venv/bin/python -m pytest anonimizar/tests -q
"""

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

import anonimizar as an  # noqa: E402

FIXTURES = AQUI / "fixtures"
CONFIG = FIXTURES / "dive.config.test.yaml"
REALES = ["111122223333", "444455556666", "777788889999", "i-0abc1234def567890", "vol-0abc12345678def90",
          "sg-0fedcba987654321a", "vpc-0a1b2c3d", "34.201.18.9", "34-201-18-9", "52.94.10.20",
          "acme.com", "acmepay.io", "acme-corp.co", "ops@", "seguridad@", "p7r2kd9xw4", "c9akciq32xyz",
          "50dc6c495c0c9188", "12345678-90ab-cdef-1234-567890abcdef", "Z0123456789ABCDEFGHIJ",
          "db-ABCDEFGHIJKLMNOPQRSTUVWXYZ", "999900001111", "Acme Prod", "ACMEPAY-Shared-Services",
          # nombres generados por CloudFormation / CDK / Beanstalk y secretos de servicio
          "4f1a9c2e", "4C7E20B1", "7B3D90F2", "A7C1D2E9", "58120937", "5AE19D3", "q1w2e3r4t5y6",
          "Zx9Kq2Lm7Np4", "9f8e7d6c5b4a3921", "a1b2c3d4e5", "hT7wQpZ2mXv9", "Z09876543ABCDEFGHJKLM",
          "0a1b2c3d4e5f60718", "Ab3xYz", "Qr7TuV", "Kd8sPq", "0f1e2d3c-4b5a-4968-8776-655443322110",
          "MGYxZTJkM2Mt", "zq8w4e2r6t1y", "Pd3kx9Lm2Qw5", "m3t8zq5c", "7H123456", "2r4k9", "k2m4n6p8q0",
          "8QWERTY12ASDF34G", "Ab3dE5gH7jK9mN1p",
          # prefijos en etiquetas truncadas de draw.io
          "0a1b2c3d4e5f…", "0f1e2d3c-4b5a-49…", "x7k2p9q4"]


@pytest.fixture
def corrida(tmp_path):
    destino = tmp_path / "salida" / "2026-09-26"
    shutil.copytree(FIXTURES / "corrida", destino)
    return destino


def correr(carpeta, *extra):
    return an.main([str(carpeta), "--config", str(CONFIG), *extra])


def todo_el_texto(carpeta):
    return "\n".join(an.contenido_verificable(p) for p in sorted(carpeta.rglob("*")) if p.is_file())


def test_salida_limpia_y_verificada(corrida):
    assert correr(corrida) == 0
    anon = corrida.parent / "2026-09-26-anon"
    texto = todo_el_texto(anon)
    for real in REALES:
        assert real.lower() not in texto.lower(), real
    assert not re.search("acme", texto, re.IGNORECASE)
    assert (corrida / "anon.mapping.json").exists()
    assert not list(anon.rglob("*.mapping.json"))
    assert (anon / "diagramas" / "red-empresa.drawio").exists()


def test_mapeo_consistente_entre_archivos(corrida):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    mapeo = json.loads((corrida / "anon.mapping.json").read_text())["categories"]
    falso_cuenta = mapeo["account_id"]["111122223333"]
    falso_inst = mapeo["resource_id"]["i-0abc1234def567890"]
    falso_ip = mapeo["ip"]["34.201.18.9"]
    for nombre in ("findings.md", "reporte.html", "diagramas/red-empresa.drawio", "inventory.json"):
        assert falso_cuenta in (anon / nombre).read_text(), nombre
    inv = json.loads((anon / "inventory.json").read_text())
    ec2 = inv["resources"][0]
    assert ec2["id"] == falso_inst and ec2["arn"].endswith(falso_inst)  # mismo ID dentro y fuera del ARN
    assert ec2["attrs"]["public_ip"] == falso_ip
    assert ec2["attrs"]["public_dns"].startswith("ec2-" + falso_ip.replace(".", "-"))
    assert falso_ip in (anon / "diagramas" / "red-empresa.drawio").read_text()
    assert re.fullmatch(r"\d{12}", falso_cuenta) and falso_cuenta != "111122223333"


def test_nombres_cuentas_recursos_y_dominios(corrida):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    disc = json.loads((anon / "discovery.json").read_text())
    nombres = sorted(a["name"] for a in disc["accounts"].values())
    assert nombres == ["cuenta-01-prod", "cuenta-02-shared", "cuenta-03-dev"]
    inv = json.loads((anon / "inventory.json").read_text())
    por_servicio = {}
    for r in inv["resources"]:
        por_servicio.setdefault(r["service"], r)
    lam = por_servicio["lambda"]
    assert lam["name"] == "api-pagos-prod"                                  # sin marca: intacto
    assert lam["id"] == "Empresa-api-prod"                                  # caja mixta en compuesto
    assert lam["arn"].endswith(":function:Empresa-api-prod")                # marca dentro de ARN
    assert lam["attrs"]["role"].endswith(":role/empresa-lambda-role")
    assert por_servicio["ec2"]["tags"]["Project"] == "Empresa Pagos"        # "AcMe"
    assert por_servicio["ec2"]["attrs"]["private_ip"] == "10.0.1.10"        # privadas se conservan
    r53 = por_servicio["route53"]
    assert re.fullmatch(r"pagos\.ejemplo\d*\.com\.", r53["name"])          # subdominio conservado
    assert r53["attrs"]["records"][0].startswith("api.pagos.ejemplo")
    assert por_servicio["apigateway"]["attrs"]["endpoint"].endswith(".execute-api.us-east-1.amazonaws.com/prod")
    md = (anon / "findings.md").read_text()
    assert md.startswith("# Hallazgos · EMPRESA")
    assert "Cuenta de pruebas Dev sin costo" in md       # nombre genérico no se toca en texto libre
    html = (anon / "reporte.html").read_text()
    assert "http://www.w3.org/2000/svg" in html and "fonts.googleapis.com" in html
    assert re.search(r"usuario\d*@ejemplo\d*\.com", html)


def test_costos_redondeados(corrida):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    disc = json.loads((anon / "discovery.json").read_text())
    cuentas = list(disc["accounts"].values())
    assert cuentas[0]["total_usd"] == 1200.0
    assert cuentas[0]["regions"]["global"]["services"]["Amazon Route 53"] == 230.0
    assert "$1,200" in (anon / "findings.md").read_text() and "770 USD" in (anon / "findings.md").read_text()
    assert disc["period"]["months"] == 6  # no es costo


def test_costos_porcentaje(corrida):
    correr(corrida, "--costos", "percent")
    anon = corrida.parent / "2026-09-26-anon"
    disc = json.loads((anon / "discovery.json").read_text())
    totales = [a["total_usd"] for a in disc["accounts"].values()]
    assert totales[:2] == [61.73, 38.27]  # 1234.56 y 765.44 sobre 2000
    f = json.loads((anon / "findings.json").read_text())["findings"][0]
    assert f["monthly_usd"] == round(12.34 / (2000 / 6) * 100, 2)  # mensual contra el promedio mensual
    assert "61.73%" in (anon / "findings.md").read_text()


def test_costos_exactos(corrida):
    correr(corrida, "--costos", "exact")
    anon = corrida.parent / "2026-09-26-anon"
    assert "$1,234.56" in (anon / "findings.md").read_text()


def test_ocultar_privadas(corrida):
    assert correr(corrida, "--ocultar-privadas") == 0
    anon = corrida.parent / "2026-09-26-anon"
    texto = todo_el_texto(anon)
    assert "10.0.1.10" not in texto and "ip-10-0-1-10" not in texto and "172.16.5.0/24" not in texto
    inv = json.loads((anon / "inventory.json").read_text())
    ec2 = inv["resources"][0]
    red = ".".join(ec2["attrs"]["private_ip"].split(".")[:2])
    assert ec2["attrs"]["private_ip"].endswith(".1.10")               # se conserva la forma de la subred
    assert f"{red}.0.0/16" in (anon / "diagramas" / "red-empresa.drawio").read_text()


def test_determinista_con_mapeo_reusado(corrida):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    primera = {p.name: p.read_text() for p in anon.rglob("*") if p.is_file()}
    correr(corrida)
    segunda = {p.name: p.read_text() for p in anon.rglob("*") if p.is_file()}
    assert primera == segunda


def test_verificacion_detecta_fugas(corrida, capsys):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    mapping = corrida / "anon.mapping.json"
    base = ["--config", str(CONFIG), "--mapping", str(mapping), "--verificar"]
    assert an.main([str(anon), *base]) == 0
    (anon / "fuga.md").write_text("Nota: la cuenta 111122223333 de ACME usa 34.201.18.9 y 123412341234.\n")
    assert an.main([str(anon), *base]) == 2
    err = capsys.readouterr().err
    assert "valor original (account_id)" in err and "término de marca" in err
    assert "12 dígitos que no son un ID falso" in err and "IP pública" in err


def test_verificacion_falla_si_un_termino_no_se_reemplaza(corrida):
    # El término 'pagos' no está en la config: la verificación lo busca solo si se agrega.
    assert an.main([str(corrida), "--config", str(CONFIG)]) == 0
    anon = corrida.parent / "2026-09-26-anon"
    mapping = corrida / "anon.mapping.json"
    assert an.main([str(anon), "--config", str(CONFIG), "--mapping", str(mapping), "--verificar",
                    "--termino", "pagos"]) == 2


def test_binarios_no_se_copian(corrida):
    (corrida / "diagramas" / "captura.png").write_bytes(b"\x89PNG acme 111122223333")
    assert correr(corrida) == 0
    assert not list((corrida.parent / "2026-09-26-anon").rglob("*.png"))


def test_drawio_comprimido_queda_legible(corrida):
    correr(corrida)
    drawio = (corrida.parent / "2026-09-26-anon" / "diagramas" / "red-empresa.drawio").read_text()
    assert "<mxGraphModel>" in drawio and drawio.count("<diagram ") == 2
    assert "empresa-api-prod-1" in drawio


def test_mapeo_de_privadas_reusado_sin_flag(corrida):
    assert correr(corrida, "--ocultar-privadas") == 0
    assert correr(corrida) == 0  # mismo mapeo, privadas visibles: no es fuga


def test_nombres_generados_conservan_la_historia(corrida):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    inv = json.loads((anon / "inventory.json").read_text())
    ids = [r["id"] for r in inv["resources"]]
    arns = " ".join(r["arn"] or "" for r in inv["resources"])
    # la parte legible se conserva, solo cambian hash y sufijo
    assert any(re.fullmatch(r"appstack-dev-database[0-9a-f]{8}-[0-9a-z]{12}", i) for i in ids)
    assert any(re.fullmatch(r"PortalStack-uat-Service[0-9A-F]{8}-[0-9A-Z]{12}", i) for i in ids)
    assert any(re.fullmatch(r"awseb-e-[0-9a-z]{10}-stack-AWSEBAutoScalingGroup-\w{12}", i) for i in ids)
    assert re.search(r"role/AppStack-dev-JobFnServiceRole\w{8}-\w{12}", arns)
    assert re.search(r"loadbalancer/app/awseb--AWSEB-\w{12}/\w{16}", arns)
    assert re.search(r"StartEC2Rule-\w{5}\b", arns)
    assert any(re.fullmatch(r"Z[0-9A-Z]{20}", i) for i in ids)                      # zona R53 con su Z
    assert any(re.fullmatch(r"s2svpn!vpn-[0-9a-f]{17}-\w{6}", i) for i in ids)
    assert re.search(r"secret:s2svpn!vpn-[0-9a-f]{17}-\w{6}-\w{6}\b", arns)
    assert re.search(r"secret:empresa-pos/cert\.pem-\w{6}\b", arns)


def test_mismo_token_en_otra_caja_y_otro_archivo(corrida):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    inv = json.loads((anon / "inventory.json").read_text())
    rds = next(r for r in inv["resources"] if r["id"].startswith("appstack-dev-database"))
    ecs = next(r for r in inv["resources"] if r["id"].startswith("PortalStack-uat-"))
    hash_rds, suf_rds = rds["id"].split("-")[2][len("database"):], rds["id"].split("-")[3]
    assert rds["tags"]["aws:cloudformation:logical-id"] == "Database" + hash_rds.upper()  # CDK en ambas cajas
    assert ecs["id"].split("-")[3] == suf_rds.upper()                                    # CFN en ambas cajas
    drawio = (anon / "diagramas" / "red-empresa.drawio").read_text()
    assert ecs["id"].lower() in drawio.lower()   # drawio se procesa antes que inventory.json
    api = next(r for r in inv["resources"] if r["service"] == "apigateway")["id"]
    assert f"API {api}" in drawio


def test_ids_pegados_a_guion_bajo_y_etiquetas_truncadas(corrida):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    mapeo = json.loads((corrida / "anon.mapping.json").read_text())["categories"]
    inst = mapeo["resource_id"]["i-0abc1234def567890"]
    uuid = mapeo["uuid"]["0f1e2d3c-4b5a-4968-8776-655443322110"]
    vpn = mapeo["resource_id"]["vpn-0a1b2c3d4e5f60718"]
    html = (anon / "reporte.html").read_text()
    assert f"AwsBackup_{inst}_{uuid}" in html
    drawio = (anon / "diagramas" / "red-empresa.drawio").read_text()
    assert f"s2svpn!{vpn[:16]}…" in drawio            # truncada: comienzo del falso, no del real
    assert f"rds!db-{uuid[:16]}…" in drawio


def test_verificacion_detecta_prefijo_truncado(corrida, capsys):
    correr(corrida)
    anon = corrida.parent / "2026-09-26-anon"
    (anon / "fuga.drawio").write_text('<mxfile><diagram><mxGraphModel value="vpn-0a1b2c3d4e5f…"/></diagram></mxfile>')
    assert an.main([str(anon), "--config", str(CONFIG), "--mapping", str(corrida / "anon.mapping.json"),
                    "--verificar"]) == 2
    assert "etiqueta truncada" in capsys.readouterr().err


def test_rendimiento_con_miles_de_recursos(tmp_path):
    """~3000 recursos con IDs, ARNs y nombres generados: anonimizar + verificar en pocos segundos."""
    import time
    carpeta = tmp_path / "salida" / "2026-09-27"
    carpeta.mkdir(parents=True)
    recursos = []
    for n in range(3000):
        h = f"{n:017x}"
        cuenta = f"{100000000000 + n % 40:012d}"
        recursos.append({"account_id": cuenta, "region": "us-east-1", "service": "ec2", "type": "instance",
                         "id": f"i-{h}", "arn": f"arn:aws:ec2:us-east-1:{cuenta}:instance/i-{h}",
                         "name": f"acme-app-{n}-stack-Fn{n:08X}-a{n:05d}bcdef1", "tags": {},
                         "attrs": {"public_ip": f"54.{n // 250}.{n % 250}.7", "sg": f"sg-{n:08x}"},
                         "relations": {}})
    (carpeta / "inventory.json").write_text(json.dumps({"accounts": {}, "resources": recursos}))
    (carpeta / "reporte.html").write_text("<script>" + json.dumps(recursos) + "</script>")
    inicio = time.time()
    assert correr(carpeta) == 0
    assert time.time() - inicio < 30
