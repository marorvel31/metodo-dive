"""Pruebas de la fase 4 sin AWS. Corre con pytest o directo:
    .venv/bin/python 4-esquematizar/tests/test_esquematizar.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI.parent.parent))

import esquematizar  # noqa: E402
import reporte  # noqa: E402

FIX = AQUI / "fixtures"


def cargar(nombre):
    return json.loads((FIX / nombre).read_text())


def validar_drawio(ruta):
    raiz = ET.parse(ruta).getroot()
    assert raiz.tag == "mxfile"
    paginas = raiz.findall("diagram")
    assert paginas, ruta
    for p in paginas:
        ids = [c.get("id") for c in p.iter() if c.get("id")]
        assert len(ids) == len(set(ids)), f"ids repetidos en {ruta}:{p.get('name')}"
        conocidos = set(ids)
        for c in p.iter("mxCell"):
            if c.get("edge") == "1":
                assert c.get("source") in conocidos and c.get("target") in conocidos, "arista colgando"
    return raiz


def sin_solapes(ruta):
    """Hermanos (mismo parent) no deben solaparse, salvo el halo rojo con su icono."""
    for p in ET.parse(ruta).getroot().findall("diagram"):
        cajas = {}
        for c in p.iter("mxCell"):
            geo = c.find("mxGeometry")
            if c.get("vertex") != "1" or geo is None or not c.get("id"):
                continue
            cid = c.get("id") or ""
            cajas.setdefault(c.get("parent"), []).append((cid, *(float(geo.get(k, 0)) for k in ("x", "y", "width", "height"))))
        # UserObject guarda el id afuera del mxCell
        for uo in p.iter("UserObject"):
            c = uo.find("mxCell")
            geo = c.find("mxGeometry")
            cajas.setdefault(c.get("parent"), []).append((uo.get("id"), *(float(geo.get(k, 0)) for k in ("x", "y", "width", "height"))))
        for hijos in cajas.values():
            for i, a in enumerate(hijos):
                for b in hijos[i + 1:]:
                    if a[0].replace("-halo", "") == b[0].replace("-halo", ""):
                        continue
                    solapa = a[1] < b[1] + b[3] and b[1] < a[1] + a[3] and a[2] < b[2] + b[4] and b[2] < a[2] + a[4]
                    assert not solapa, f"solape {a[0]} / {b[0]} en {ruta}"


def test_fixture_completo():
    with tempfile.TemporaryDirectory() as tmp:
        archivos = esquematizar.generar(cargar("inventory.json"), cargar("findings.json"), cargar("discovery.json"), Path(tmp) / "d")
        assert [a.name for a in archivos][0] == "00-organizacion.drawio"
        assert len(archivos) == 5  # org + 4 cuentas (una vacía)
        for a in archivos:
            validar_drawio(a)
            sin_solapes(a)
        prod = (Path(tmp) / "d" / "222222222222-Produccion.drawio").read_text()
        assert "31× ec2 instance" in prod, "el grupo grande debe colapsar"
        assert "strokeColor=#D0021B" in prod, "hallazgo alto con borde rojo"
        org = (Path(tmp) / "d" / "00-organizacion.drawio").read_text()
        assert org.count('edge="1"') >= 4  # 2 spokes TGW + peering + VPN


def test_determinista():
    with tempfile.TemporaryDirectory() as tmp:
        a = esquematizar.generar(cargar("inventory.json"), cargar("findings.json"), cargar("discovery.json"), Path(tmp) / "a")
        b = esquematizar.generar(cargar("inventory.json"), cargar("findings.json"), cargar("discovery.json"), Path(tmp) / "b")
        for x, y in zip(a, b):
            assert x.read_bytes() == y.read_bytes(), x.name


def test_escala_y_datos_raros():
    recursos = [{"account_id": "999999999999", "region": "eu-west-1", "service": "vpc", "type": "vpc", "id": "vpc-x"}]
    for i in range(40):
        recursos.append({"account_id": "999999999999", "region": "eu-west-1", "service": "vpc", "type": "subnet",
                         "id": f"subnet-{i}", "attrs": {"az": f"eu-west-1{'abc'[i % 3]}"}, "relations": {"vpc_id": "vpc-x"}})
    for i in range(800):
        recursos.append({"account_id": "999999999999", "region": "eu-west-1", "service": ["ec2", "lambda", "ecs"][i % 3],
                         "type": ["instance", "function", "service"][i % 3], "id": f"r-{i}",
                         "relations": {"subnet_ids": [f"subnet-{i % 40}"]}})
    recursos += [
        {"account_id": "999999999999", "region": "eu-west-1", "service": "raro", "type": None, "id": None},
        {"account_id": "999999999999", "region": None, "service": "s3", "type": "bucket", "id": "b", "relations": None, "attrs": None},
        {"account_id": "999999999999", "region": "eu-west-1", "service": "elb", "type": "load_balancer", "id": "lb",
         "relations": {"subnet_ids": ["subnet-no-inventariada"], "target_ids": ["r-0", "r-3", "no-existe"]}},
        {"account_id": "999999999999", "region": "eu-west-1", "service": "transitgateway", "type": "transit_gateway_attachment",
         "id": "tgw-attach-x", "attrs": {"transit_gateway_id": "tgw-ajeno"}, "relations": {"vpc_id": "vpc-x"}},
        "no soy un dict",
    ]
    inv = {"accounts": {"999999999999": {"name": "Grande"}, "000000000000": {}}, "resources": recursos,
           "errors": [{"account_id": "000000000000", "region": "us-east-1", "collector": "ec2", "error": "AccessDenied"}]}
    with tempfile.TemporaryDirectory() as tmp:
        archivos = esquematizar.generar(inv, None, None, Path(tmp))
        for a in archivos:
            validar_drawio(a)
            sin_solapes(a)
        grande = next(a for a in archivos if a.name.startswith("999"))
        assert len(grande.read_bytes()) < 2_000_000
        reporte.generar(inv, None, None, Path(tmp) / "r.html")
        # también con un inventario vacío
        esquematizar.generar({}, None, None, Path(tmp) / "vacio")
        reporte.generar({}, None, None, Path(tmp) / "vacio.html")


def test_reporte_js():
    node = shutil.which("node")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "reporte.html"
        reporte.generar(cargar("inventory.json"), cargar("findings.json"), cargar("discovery.json"), ruta, ["diagramas/00-organizacion.drawio"])
        contenido = ruta.read_text()
        assert "http://" not in contenido and "https://" not in contenido, "el reporte no debe cargar nada externo"
        if not node:
            print("node no está disponible: se omite la prueba de JS")
            return
        salida = subprocess.run([node, str(AQUI / "smoke_reporte.js"), str(ruta)], capture_output=True, text=True, check=True)
        r = json.loads(salida.stdout)
        assert r["filas"] == 100 and r["hallazgos"] == 7 and r["errores"] == 2 and r["filasEc2"] > 0, r


if __name__ == "__main__":
    for nombre, f in list(globals().items()):
        if nombre.startswith("test_"):
            f()
            print("ok", nombre)
