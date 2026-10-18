"""Pruebas del dashboard: ingestión, historia por fingerprint, estados y controles del servidor HTTP.
Sin AWS: corridas sintéticas en un directorio temporal.

Uso: .venv/bin/python -m pytest 5-dashboard/tests -q
"""

import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

import base  # noqa: E402
import dashboard  # noqa: E402

ORG = "o-abc1234567"


def _f(fp, titulo, source="regla", usd=10.0, cat="desperdicio", region="eu-west-1", sev="media"):
    return {"id": "F1", "category": cat, "severity": sev, "account_id": "111122223333", "region": region,
            "resource_id": "r-" + fp, "title": titulo, "detail": "d", "monthly_usd": usd, "source": source,
            "rule_id": f"{source}.x", "issue_id": "x", "fingerprint": fp, "price_source": None,
            "is_saving": cat in ("desperdicio", "costo")}


def _corrida(base_dir, run_id, findings, sin_doctor=False, errores=None, skipped=None, alias="mi-org",
             inv_errores=None, generado=None):
    c = base_dir / run_id
    c.mkdir()
    run = {"run_id": run_id, "organization_id": ORG, "org_alias": alias, "currency": "USD",
           "flags": {"validar": {"sin_doctor": sin_doctor}}}
    (c / "findings.json").write_text(json.dumps({"run": run, "generated_at": generado or run_id, "findings": findings,
                                                 "errors": errores or []}))
    (c / "discovery.json").write_text(json.dumps({"run": run, "charges_monthly_usd": {
        "111122223333": {"Tax": {"2026-09": 25.0}, "SavingsPlanNegation": {"2026-09": -5.0}}},
        "accounts": {"111122223333": {
        "name": "Prod", "regions": {"eu-west-1": {
            "services_monthly_usd": {"Amazon EC2": {"2026-08": 100.0, "2026-09": 120.0}},
            "usage_types_monthly_usd": {"EU-NatGateway-Bytes": {"2026-09": 30.0}}}}}}}))
    (c / "inventory.json").write_text(json.dumps({"run": run, "resources": [
        {"account_id": "111122223333", "region": "eu-west-1", "service": "ec2", "type": "volume", "id": "r-" + "a" * 16,
         "arn": None, "name": "vol", "state": "available", "tags": {"env": "prod"}, "attrs": {"size_gb": 8}, "relations": {}},
        {"account_id": "444455556666", "region": "eu-west-1", "service": "s3", "type": "bucket", "id": "b1",
         "arn": None, "name": "b1", "state": None, "tags": {}, "attrs": {}, "relations": {}}],
        "skipped": skipped or [],
                                                  "errors": inv_errores or []}))
    (c / "reporte.html").write_text("<html><script>window.x=1</script></html>")
    return c


@pytest.fixture
def corridas(tmp_path):
    a = _f("a" * 16, "solo regla")
    d = _f("d" * 16, "solo aws-doctor", source="aws-doctor")
    g = _f("e" * 16, "se arregla")
    _corrida(tmp_path, "2026-10-01T000000Z", [a, d, g])
    _corrida(tmp_path, "2026-10-02T000000Z", [a], sin_doctor=True)              # sin doctor: d no verificado
    _corrida(tmp_path, "2026-10-03T000000Z", [a, d, _f("b" * 16, "nuevo")])     # g resuelto, b nuevo
    (tmp_path / "2026-09-01").mkdir()
    (tmp_path / "2026-09-01" / "findings.json").write_text(json.dumps({"findings": []}))  # sin run: se omite
    con = base.conectar(tmp_path / "db.sqlite")
    nuevas, omitidas = base.ingerir(con, [tmp_path])
    assert len(nuevas) == 3 and omitidas == ["2026-09-01"]
    assert base.ingerir(con, [tmp_path]) == ([], ["2026-09-01"])                 # idempotente
    return con


def _por_fp(lista):
    return {f["fingerprint"][0]: f for f in lista}


def test_historia_sin_doctor_no_es_resuelto(corridas):
    h = _por_fp(base.hallazgos(corridas, ORG, "2026-10-02T000000Z"))
    assert h["d"]["presencia"] == "no_verificado"          # la corrida no corrió aws-doctor
    assert h["e"]["presencia"] == "resuelto"               # regla: podía verlo y ya no está
    assert h["a"]["presencia"] == "presente" and not h["a"]["nuevo"]


def test_historia_nuevo_y_vuelta_tras_sin_doctor(corridas):
    h = _por_fp(base.hallazgos(corridas, ORG, "2026-10-03T000000Z"))
    assert h["b"]["nuevo"] and h["b"]["primera"] == "2026-10-03T000000Z"
    assert h["d"]["presencia"] == "presente" and not h["d"]["nuevo"]      # vuelve tras la corrida sin doctor
    assert h["d"]["primera"] == "2026-10-01T000000Z" and h["d"]["veces"] == 2
    assert h["e"]["presencia"] == "resuelto"


def test_region_no_habilitada_no_resuelve(tmp_path):
    _corrida(tmp_path, "2026-10-01T000000Z", [_f("a" * 16, "x", region="me-south-1")])
    _corrida(tmp_path, "2026-10-02T000000Z", [],
             skipped=[{"account_id": "111122223333", "region": "me-south-1", "reason": "region_not_enabled"}])
    con = base.conectar(tmp_path / "db.sqlite")
    base.ingerir(con, [tmp_path])
    assert base.hallazgos(con, ORG, "2026-10-02T000000Z")[0]["presencia"] == "no_verificado"


def test_estado_por_organizacion_y_ahorro(corridas):
    r = base.resumen(corridas, ORG, "2026-10-03T000000Z")
    assert r["ahorro_abierto"] == 30.0 and r["nuevos"] == 1 and r["resueltos"] == 0   # "e" se resolvió en la 2
    assert base.resumen(corridas, ORG, "2026-10-02T000000Z")["resueltos"] == 1
    assert r["gasto_ultimo_mes"] == 120.0 and r["usos"][0]["usage_type"] == "EU-NatGateway-Bytes"
    base.guardar_estado(corridas, ORG, "a" * 16, "aceptado", "riesgo asumido")
    assert base.resumen(corridas, ORG, "2026-10-03T000000Z")["ahorro_abierto"] == 20.0
    base.guardar_estado(corridas, ORG, "b" * 16, "resuelto", "")
    assert _por_fp(base.hallazgos(corridas, ORG, "2026-10-03T000000Z"))["b"]["reaparece"]
    for malo in [("a" * 16, "otro", ""), ("a" * 16, "abierto", "x" * 3000), ("f" * 16, "abierto", "")]:
        with pytest.raises(ValueError):
            base.guardar_estado(corridas, ORG, *malo)


# ---------- servidor ----------

@pytest.fixture
def servidor(tmp_path, corridas):
    db = tmp_path / "db.sqlite"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    puerto = srv.server_address[1]
    srv.RequestHandlerClass = dashboard.crear_handler(db, [tmp_path], "TOKEN123", puerto)
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    yield puerto
    srv.shutdown()
    srv.server_close()


def _pedir(puerto, metodo, ruta, headers=None, cuerpo=None):
    c = http.client.HTTPConnection("127.0.0.1", puerto, timeout=5)
    h = {"Host": f"127.0.0.1:{puerto}"}
    h.update(headers or {})
    c.request(metodo, ruta, body=cuerpo, headers=h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_servidor_controles_de_acceso(servidor):
    p = servidor
    tok = {"X-DIVE-Token": "TOKEN123"}
    st, hd, cuerpo = _pedir(p, "GET", "/")                         # la página no lleva datos ni token
    assert st == 200 and b"TOKEN123" not in cuerpo and "Set-Cookie" not in hd
    assert "frame-src 'self'" in hd["Content-Security-Policy"] and "script-src 'self'" in hd["Content-Security-Policy"]
    assert _pedir(p, "GET", "/", {"Host": "evil.example:80"})[0] == 403                # DNS rebinding
    assert _pedir(p, "GET", "/api/orgs")[0] == 403                                       # sin header
    assert _pedir(p, "GET", "/api/orgs", {"Cookie": "dive=TOKEN123"})[0] == 403         # las cookies no cuentan
    assert _pedir(p, "GET", "/api/orgs", {"X-DIVE-Token": "é".encode().decode("latin-1")})[0] == 403  # no ASCII
    st, hd, cuerpo = _pedir(p, "GET", "/api/orgs", tok)
    assert st == 200 and json.loads(cuerpo)[0]["org_id"] == ORG and "Set-Cookie" not in hd
    for ruta in ("/../db.sqlite", "/reporte/../../etc/passwd", "/web/app.js", "/db.sqlite"):
        assert _pedir(p, "GET", ruta)[0] in (403, 404), ruta


def test_servidor_reporte_con_ticket_de_un_uso(servidor):
    p = servidor
    run = "2026-10-03T000000Z"
    assert _pedir(p, "GET", f"/reporte/{run}")[0] == 403                                  # sin ticket
    assert _pedir(p, "GET", f"/api/ticket?run={run}")[0] == 403                           # ticket pide header
    t = json.loads(_pedir(p, "GET", f"/api/ticket?run={run}", {"X-DIVE-Token": "TOKEN123"})[2])["ticket"]
    assert _pedir(p, "GET", f"/reporte/2026-10-01T000000Z?k={t}")[0] == 403              # ticket de otra corrida
    t = json.loads(_pedir(p, "GET", f"/api/ticket?run={run}", {"X-DIVE-Token": "TOKEN123"})[2])["ticket"]
    st, hd, _ = _pedir(p, "GET", f"/reporte/{run}?k={t}")
    assert st == 200 and hd["Content-Security-Policy"].startswith("sandbox allow-scripts")
    assert _pedir(p, "GET", f"/reporte/{run}?k={t}")[0] == 403                            # de un solo uso


def test_servidor_post_estado(servidor):
    p = servidor
    cuerpo = json.dumps({"org": ORG, "fingerprint": "a" * 16, "status": "ignorado", "nota": "ok"})
    json_h = {"Content-Type": "application/json"}
    assert _pedir(p, "POST", "/api/estado", json_h, cuerpo)[0] == 403                      # sin header
    auth = {"X-DIVE-Token": "TOKEN123"}
    assert _pedir(p, "POST", "/api/estado", {**auth, "Content-Type": "text/plain"}, cuerpo)[0] == 415
    assert _pedir(p, "POST", "/api/estado", {**auth, **json_h}, "x" * 9000)[0] == 413
    assert _pedir(p, "POST", "/api/estado", {**auth, **json_h, "Content-Length": "abc"}, "")[0] == 413
    assert _pedir(p, "POST", "/api/estado", {**auth, **json_h}, "[" * 5000)[0] == 400       # anidado
    malo = json.dumps({"org": ORG, "fingerprint": "a" * 16, "status": "borrar", "nota": ""})
    assert _pedir(p, "POST", "/api/estado", {**auth, **json_h}, malo)[0] == 400
    st, _, r = _pedir(p, "POST", "/api/estado", {**auth, **json_h}, cuerpo)
    assert st == 200 and json.loads(r)["status"] == "ignorado"


def test_sin_acceso_a_la_cuenta_o_sin_aws_doctor_no_es_resuelto(tmp_path):
    a, d = _f("a" * 16, "regla"), _f("d" * 16, "doctor", source="aws-doctor")
    _corrida(tmp_path, "2026-10-01T000000Z", [a, d])
    _corrida(tmp_path, "2026-10-02T000000Z", [],
             inv_errores=[{"account_id": "111122223333", "region": None, "collector": "acceso", "error": "x"}],
             errores=[{"account_id": None, "region": None, "collector": "aws-doctor", "error": "no instalado"}])
    con = base.conectar(tmp_path / "db.sqlite")
    base.ingerir(con, [tmp_path])
    h = _por_fp(base.hallazgos(con, ORG, "2026-10-02T000000Z"))
    assert h["a"]["presencia"] == h["d"]["presencia"] == "no_verificado"


def test_resuelto_no_se_deshace_con_una_corrida_sin_doctor_despues(tmp_path):
    d = _f("d" * 16, "doctor", source="aws-doctor")
    _corrida(tmp_path, "2026-10-01T000000Z", [d])
    _corrida(tmp_path, "2026-10-02T000000Z", [])                    # con doctor y sin d: resuelto
    _corrida(tmp_path, "2026-10-03T000000Z", [], sin_doctor=True)   # no lo "des-resuelve"
    con = base.conectar(tmp_path / "db.sqlite")
    base.ingerir(con, [tmp_path])
    f = base.hallazgos(con, ORG, "2026-10-03T000000Z")[0]
    assert f["presencia"] == "resuelto" and f["resuelto_en"] == "2026-10-02T000000Z"


def test_reingesta_de_una_corrida_revalidada_y_json_raro(tmp_path):
    _corrida(tmp_path, "2026-10-01T000000Z", [_f("a" * 16, "x")], sin_doctor=True)
    (tmp_path / "rara").mkdir()
    (tmp_path / "rara" / "findings.json").write_text("[]")              # raíz que no es un objeto
    con = base.conectar(tmp_path / "db.sqlite")
    assert base.ingerir(con, [tmp_path]) == (["2026-10-01T000000Z"], ["rara"])
    import shutil
    shutil.rmtree(tmp_path / "2026-10-01T000000Z")
    _corrida(tmp_path, "2026-10-01T000000Z", [_f("a" * 16, "x"), _f("b" * 16, "y")], generado="otra-vez")
    assert base.ingerir(con, [tmp_path])[0] == ["2026-10-01T000000Z"]   # validar re-corrió: se rehace
    assert len(base.hallazgos(con, ORG, "2026-10-01T000000Z")) == 2
    assert con.execute("SELECT sin_doctor FROM runs").fetchone()[0] == 0


def test_recursos_filtro_de_cuenta_y_factura(corridas):
    run = "2026-10-03T000000Z"
    todos = base.recursos(corridas, ORG, run)
    assert len(todos) == 2 and {r["account_id"] for r in todos} == {"111122223333", "444455556666"}
    vol = [r for r in todos if r["service"] == "ec2"][0]
    assert vol["tags"] == {"env": "prod"} and vol["n_hallazgos"] == 1      # el finding "a" apunta a este recurso
    assert len(base.recursos(corridas, ORG, run, "444455556666")) == 1
    r = base.resumen(corridas, ORG, run)
    assert r["impuestos_ultimo_mes"] == 25.0 and r["factura_ultimo_mes"] == 120.0 + 25.0 - 5.0
    assert r["n_recursos"] == 2
    otra = base.resumen(corridas, ORG, run, "444455556666")                 # cuenta sin costos ni hallazgos
    assert otra["gasto_ultimo_mes"] == 0 and otra["impuestos_ultimo_mes"] == 0 and otra["abiertos"] == 0
    assert otra["n_recursos"] == 1 and all(e["n_findings"] == 0 for e in otra["evolucion"])


def test_api_rechaza_cuenta_invalida(servidor):
    st = _pedir(servidor, "GET", f"/api/recursos?org={ORG}&run=2026-10-03T000000Z&cuenta=x%27%20OR%201%3D1",
                {"X-DIVE-Token": "TOKEN123"})[0]
    assert st == 400


def test_resumen_inventario_y_ventana(corridas):
    r = base.resumen(corridas, ORG, "2026-10-03T000000Z")
    assert r["n_cuentas"] == 2 and r["n_cuentas_con_costo"] == 1 and r["regiones"] == ["eu-west-1"]  # 1 solo inventario
    v = r["ventana"]
    assert (v["desde"], v["hasta"], v["meses"]) == ("2026-08", "2026-09", 2)
    assert v["uso"] == 220.0 and v["impuestos"] == 25.0 and v["factura"] == 220.0 + 25.0 - 5.0
    otra = base.resumen(corridas, ORG, "2026-10-03T000000Z", "444455556666")
    assert otra["n_cuentas"] == 1 and otra["ventana"]["uso"] == 0 and otra["regiones"] == ["eu-west-1"]


def test_global_sin_verificar_si_falla_un_colector_en_us_east_1(tmp_path):
    root = _f("a" * 16, "Usuario root sin MFA", region=None, cat="riesgo")
    _corrida(tmp_path, "2026-10-01T000000Z", [root])
    _corrida(tmp_path, "2026-10-02T000000Z", [],
             inv_errores=[{"account_id": "111122223333", "region": "us-east-1", "collector": "iam", "error": "x"}])
    con = base.conectar(tmp_path / "db.sqlite")
    base.ingerir(con, [tmp_path])
    assert base.hallazgos(con, ORG, "2026-10-02T000000Z")[0]["presencia"] == "no_verificado"


def test_check_de_aws_doctor_que_falla_solo_tapa_sus_categorias(tmp_path):
    lam = dict(_f("a" * 16, "lambda", source="aws-doctor"), rule_id="aws-doctor.over_provisioned_lambdas")
    ebs = dict(_f("b" * 16, "ebs", source="aws-doctor"), rule_id="aws-doctor.unused_ebs_volumes")
    _corrida(tmp_path, "2026-10-01T000000Z", [lam, ebs])
    _corrida(tmp_path, "2026-10-02T000000Z", [], errores=[
        {"account_id": "111122223333", "region": "eu-west-1", "collector": "aws-doctor/Lambda", "error": "denied"}])
    con = base.conectar(tmp_path / "db.sqlite")
    base.ingerir(con, [tmp_path])
    h = _por_fp(base.hallazgos(con, ORG, "2026-10-02T000000Z"))
    assert h["a"]["presencia"] == "no_verificado" and h["b"]["presencia"] == "resuelto"


def test_cost_explorer_caido_en_una_region_y_run_id_de_otra_org(tmp_path):
    cruce = dict(_f("a" * 16, "Se cobra pero no está inventariado", source="cruce-billing", cat="inventario"))
    _corrida(tmp_path, "2026-10-01T000000Z", [cruce])
    c = _corrida(tmp_path, "2026-10-02T000000Z", [])
    d = json.loads((c / "discovery.json").read_text())
    d["errors"] = [{"region": "eu-west-1", "error": "ThrottlingException"}]
    (c / "discovery.json").write_text(json.dumps(d))
    otra = tmp_path / "otra"
    otra.mkdir()
    f = json.loads((c / "findings.json").read_text())
    f["run"]["organization_id"] = "o-otra000000"
    (otra / "2026-10-02T000000Z").mkdir()
    (otra / "2026-10-02T000000Z" / "findings.json").write_text(json.dumps(f))
    con = base.conectar(tmp_path / "db.sqlite")
    nuevas, omitidas = base.ingerir(con, [tmp_path, otra])
    assert len(nuevas) == 2 and any("otra organización" in o for o in omitidas)
    assert base.hallazgos(con, ORG, "2026-10-02T000000Z")[0]["presencia"] == "no_verificado"


def test_factura_con_uso_sin_umbral_y_cuentas_solo_cargos(tmp_path):
    c = _corrida(tmp_path, "2026-10-01T000000Z", [])
    d = json.loads((c / "discovery.json").read_text())
    d["usage_total_monthly_usd"] = {"111122223333": {"2026-08": 101.0, "2026-09": 125.0}}   # +5 bajo el umbral
    d["charges_monthly_usd"]["999900000000"] = {"SavingsPlanRecurringFee": {"2026-09": 50.0}}  # la que paga el SP
    (c / "discovery.json").write_text(json.dumps(d))
    con = base.conectar(tmp_path / "db.sqlite")
    base.ingerir(con, [tmp_path])
    r = base.resumen(con, ORG, "2026-10-01T000000Z")
    assert r["factura_ultimo_mes"] == 125.0 + 25.0 - 5.0 + 50.0 and r["gasto_ultimo_mes"] == 120.0
    assert "999900000000" in r["cuentas"] and "444455556666" in r["cuentas"] and r["n_cuentas"] == 3
    assert r["n_cuentas_con_costo"] == 1
