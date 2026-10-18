"""Huecos del anonimizador encontrados en la revisión de seguridad: ccTLDs (.pt), IPv6, emails con %40,
IDs de cuenta con guiones y la corrida sin replace_terms. Sin AWS.

Correr: .venv/bin/python -m pytest anonimizar/tests -q
"""

import json
import re
import sys
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

import anonimizar as an  # noqa: E402


@pytest.fixture
def anon():
    return an.Anonimizador(an.Mapeo(), ["acme"], "rounded")


@pytest.mark.parametrize("dominio", ["loja.acme-shop.pt", "api.cliente.ch", "x.empresa.com.pt", "host.agencia.gov"])
def test_cctlds_se_anonimizan(anon, dominio):
    salida = anon.texto(f"endpoint {dominio}/ruta")
    assert dominio not in salida and "ejemplo" in salida


def test_ipv6_publica_a_documentacion_y_privadas_intactas(anon):
    salida = anon.texto("vpc 2a05:d018:1234:5600::/56 y host 2a00:1450:4003:80e::200e, ula fd12:3456::1, ll fe80::1")
    assert "2a05:d018" not in salida and "2a00:1450" not in salida
    assert "/56" in salida and salida.count("2001:db8:") == 2
    assert "fd12:3456::1" in salida and "fe80::1" in salida
    # La misma dirección siempre da el mismo falso.
    assert anon.texto("2a00:1450:4003:80e::200e") == anon.texto("2a00:1450:4003:80e::200e")


@pytest.mark.parametrize("texto", ["2026-10-17T10:00:00Z", "a las 10:00:00", "arn:aws:iam::123456789012:root",
                                   "arn:aws:s3:::bucket", "puertos 80:443",
                                   "arn:aws:apigateway:eu-west-1::/restapis/abc123"])
def test_ipv6_sin_falsos_positivos(anon, texto):
    assert "2001:db8" not in anon.texto(texto)


def test_email_codificado_y_cuenta_con_guiones(anon):
    salida = anon.texto("contacto ana%40acme-shop.pt, cuenta 1234-5678-9012")
    assert "ana" not in salida and "%40" in salida and "acme-shop" not in salida
    assert "1234-5678-9012" not in salida
    falso = salida.rsplit(" ", 1)[1]
    assert len(falso) == 14 and falso.count("-") == 2
    assert anon.texto("123456789012") == falso.replace("-", "")   # mismo mapeo que sin guiones


def test_verificar_detecta_cada_hueco(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({
        "n": "loja.cliente.pt 2a05:d018::1 1234-5678-9012 ana%40cliente.pt"}))
    motivos = {m for _, m, _ in an.verificar(tmp_path, an.Mapeo(), [])}
    assert {"dominio sin anonimizar", "IPv6 pública", "ID de cuenta con guiones",
            "email (%40) sin anonimizar"} <= motivos


def test_sin_replace_terms_falla_salvo_flag(tmp_path, capsys):
    corrida = tmp_path / "2026-10-17"
    corrida.mkdir()
    (corrida / "inventory.json").write_text(json.dumps({"resources": []}))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("anonymize:\n  costs: rounded\n")
    assert an.main([str(corrida), "--config", str(cfg)]) == 2
    assert "replace_terms" in capsys.readouterr().err
    assert an.main([str(corrida), "--config", str(cfg), "--sin-terminos"]) == 0


# ---------- revisión del diff P1 ----------

@pytest.mark.parametrize("texto,fuera", [
    ("cb=https%3A%2F%2Fapp.cliente.com%2Fx", "cliente.com"),            # URL codificada
    ("SAN *.cliente.pt y *.api.cliente.pt", "cliente.pt"),               # wildcard de ACM
    ("IPv6:2a05:d018::1", "2a05:d018"),                                  # clave:IPv6
    ("arn:aws:ec2:eu-west-1:1:ipv6/2a05:d018:1234:5600::/56", "2a05:d018"),
    ("x ::ffff:52.95.1.2 y", "52.95.1.2"),                               # IPv4-mapped: la trata RE_IP
    ("tienda.cliente.shop y xn--caf-dma.xn--p1ai", "cliente.shop"),
])
def test_bordes_de_dominios_e_ipv6(anon, texto, fuera):
    assert fuera not in anon.texto(texto)


@pytest.mark.parametrize("texto", ["arn:aws:s3:::b", "arn:aws:s3:::cafe-bucket", "mac 0a:1b:2c:3d:4e:5f",
                                   "findings.md", "cache.t4g.medium"])
def test_bordes_sin_falsos_positivos(anon, texto):
    assert anon.texto(texto) == texto


def test_ipv6_privada_solo_con_ocultar_privadas():
    con = an.Anonimizador(an.Mapeo(), ["acme"], "rounded", ocultar_privadas=True)
    assert "fd12:3456:789a::1" not in con.texto("ula fd12:3456:789a::1")
    assert con.texto("::1 y ::") == "::1 y ::"


def test_verificar_detecta_email_doblemente_codificado(tmp_path):
    (tmp_path / "x.md").write_text("contacto ana%2540cliente.pt")
    assert any("codificado" in m for _, m, _ in an.verificar(tmp_path, an.Mapeo(), []))


def test_costos_por_cuenta_del_reporte_se_anonimizan():
    """La tabla de tipos de uso del reporte lleva costos por cuenta: deben redondearse como el resto."""
    sys.path.insert(0, str(AQUI.parent.parent / "4-esquematizar"))
    import reporte
    disc = {"accounts": {"111122223333": {"regions": {"eu-west-1": {"usage_types_monthly_usd": {
        "EU-NatGateway-Bytes": {"2026-09": 1234.56}}}}}}}
    embebido = reporte.json_embebido({"usage_types": reporte.tipos_de_uso(disc)})
    salida = an.Anonimizador(an.Mapeo(), ["acme"], "rounded").texto(embebido)
    assert "1234.56" not in salida and "111122223333" not in salida


def test_verificar_no_confunde_medidas_de_12_digitos_con_cuentas(tmp_path):
    (tmp_path / "x.json").write_text('{"stored_bytes": 225339777173, "cuenta": "111122223333"}')
    motivos = [(m, f) for _, m, f in an.verificar(tmp_path, an.Mapeo(), [])]
    assert len(motivos) == 1 and "111122223333" in motivos[0][1]


def test_personas_en_campos_y_luego_en_texto_libre(tmp_path):
    a = an.Anonimizador(an.Mapeo(), ["acme"], "rounded")
    salida = a.json({"tags": {"Owner": "Ana Silva", "env": "prod"},
                     "unused_iam_users": [{"user_name": "anasilva"}]})
    assert salida["tags"]["Owner"].startswith("persona") and salida["tags"]["env"] == "prod"
    assert salida["unused_iam_users"][0]["user_name"].startswith("persona")
    assert "Ana Silva" not in a.texto('<script>{"owner":"Ana Silva"}</script>')    # reporte.html
    (tmp_path / "x.md").write_text("dueño: Ana Silva")
    assert an.verificar(tmp_path, a.mp, ["acme"])                                     # el original se detecta


def test_slug_del_nombre_de_cuenta_en_archivos_de_diagrama():
    a = an.Anonimizador(an.Mapeo(), ["acme"], "rounded")
    a.registrar_cuentas({"111122223333": "José Núñez"})
    salida = a.texto("diagramas/111122223333-Jos-N-ez.drawio")
    assert "Jos-N-ez" not in salida and "111122223333" not in salida


def test_persona_numerica_no_se_registra():
    a = an.Anonimizador(an.Mapeo(), ["acme"], "rounded")
    a.json({"user_name": "134", "owner": "ab"})
    assert a.mp.m["person"] == {}


# ---------- Contrato v2 ----------

def test_campos_v2_se_anonimizan_y_las_huellas_quedan_consistentes(tmp_path):
    corrida = tmp_path / "2026-10-18T101500Z"
    corrida.mkdir()
    run = {"run_id": corrida.name, "organization_id": "o-ab12cd34ef", "management_account_id": "111122223333",
           "org_alias": "acme-holding", "currency": "USD"}
    huella = "0123456789abcdef"
    f = {"id": "F001", "category": "riesgo", "severity": "alta", "account_id": "111122223333", "region": None,
         "resource_id": "root", "title": "t", "detail": "d", "monthly_usd": None, "source": "regla",
         "rule_id": "regla.root_sin_mfa", "issue_id": "root.no_mfa", "fingerprint": huella,
         "price_source": None, "is_saving": False}
    (corrida / "discovery.json").write_text(json.dumps({"run": run, "accounts": {}}))
    (corrida / "findings.json").write_text(json.dumps({"run": run, "findings": [f]}))
    (corrida / "reporte.html").write_text('<script type="application/json" id="datos">' + json.dumps(
        {"findings": [f], "org": "ou-ab12-cd34ef56 r-ab12"}) + "</script>")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text('anonymize:\n  replace_terms: ["acme"]\n')
    assert an.main([str(corrida), "--config", str(cfg)]) == 0

    anon = corrida.parent / f"{corrida.name}-anon"
    todo = "".join(p.read_text() for p in anon.rglob("*") if p.is_file())
    for original in ("o-ab12cd34ef", "acme-holding", "111122223333", huella, "ou-ab12-cd34ef56", "r-ab12"):
        assert original not in todo, original
    h_json = json.loads((anon / "findings.json").read_text())["findings"][0]["fingerprint"]
    assert len(h_json) == 16 and f'"fingerprint": "{h_json}"' in (anon / "reporte.html").read_text()
    assert json.loads((anon / "discovery.json").read_text())["run"]["org_alias"].startswith("org-")


def test_org_alias_de_la_corrida_se_trata_como_marca(tmp_path):
    corrida = tmp_path / "2026-10-18T101500Z"
    corrida.mkdir()
    (corrida / "discovery.json").write_text(json.dumps({"run": {"org_alias": "globexcorp"}, "accounts": {}}))
    (corrida / "inventory.json").write_text(json.dumps({"resources": [
        {"id": "globexcorp-logs", "arn": "arn:aws:s3:::globexcorp-logs", "ou": "ou-ab12-cd34ef56", "root": "r-ab12",
         "html_id": "r-meta"}]}))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text('anonymize:\n  replace_terms: ["acme"]\n')
    assert an.main([str(corrida), "--config", str(cfg)]) == 0
    anon = (corrida.parent / f"{corrida.name}-anon" / "inventory.json").read_text()
    assert "globexcorp" not in anon.lower() and "ou-ab12-cd34ef56" not in anon and "r-ab12" not in anon
    assert "r-meta" in anon                                   # no es un ID de Organizations
    ou = re.search(r'"ou": "([^"]+)"', anon).group(1)
    assert re.fullmatch(r"ou-[a-z0-9]{4}-[a-z0-9]{8}", ou)


def test_run_id_se_conserva():
    a = an.Anonimizador(an.Mapeo(), ["acme"], "rounded")
    assert a.json({"run": {"run_id": "2026-10-18T073802Z"}})["run"]["run_id"] == "2026-10-18T073802Z"
