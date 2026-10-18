"""Anonimizar: convierte las salidas de una corrida en una versión presentable en público.

Lee todos los archivos de salida/<corrida>/ y escribe su versión anónima en salida/<corrida>-anon/.
El mapeo real→anónimo se guarda en salida/<corrida>/anon.mapping.json (fuera de la carpeta
anónima; el .gitignore excluye *.mapping.json) y se reusa en corridas siguientes para que
los reemplazos sean estables.

Al final vuelve a escanear todo lo anonimizado. Si encuentra cualquier valor original, una
cadena de 12 dígitos que no sea un ID falso, un término de marca, una IP pública, un email o
un dominio sin anonimizar, lo lista y sale con código 2: esa salida NO se puede presentar.

Uso:
  python anonimizar/anonimizar.py salida/<corrida> [--ocultar-privadas] [--mapping otro.mapping.json]
                                                 [--termino acme ...] [--config dive.config.yaml]
  python anonimizar/anonimizar.py --verificar salida/<corrida>-anon --mapping salida/<corrida>/anon.mapping.json
"""

import argparse
import base64
import bisect
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import sys
import urllib.parse
import zlib
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

from common import RAIZ, cargar_config  # noqa: E402

TEXTO = {".json", ".md", ".drawio", ".xml", ".html", ".htm", ".txt", ".csv", ".svg", ".yaml", ".yml", ".log"}
MAPEO_NOMBRE = "anon.mapping.json"

# --------------------------------------------------------------------------- patrones

# Nombres de cuenta que son una sola palabra genérica: se mapean en los campos JSON de
# cuentas, pero no se reemplazan en texto libre (reemplazarían "dev" o "prod" en cualquier frase).
GENERICOS = {"dev", "prod", "uat", "qa", "test", "staging", "shared", "sandbox", "security", "audit",
             "network", "networking", "management", "master", "root", "backup", "infra", "data",
             "log", "logs", "produccion", "production", "development", "desarrollo", "pruebas"}
AMBIENTES = [("prod", "prod"), ("uat", "uat"), ("dev", "dev"), ("shared", "shared"),
             ("staging", "staging"), ("stg", "staging"), ("qa", "qa"), ("sandbox", "sandbox")]

# Dominios de AWS: se conserva el sufijo y se anonimizan solo las etiquetas con pinta de ID.
DOMINIOS_AWS = ("amazonaws.com", "amazonaws.com.cn", "cloudfront.net", "awsapps.com", "amazoncognito.com",
                "elasticbeanstalk.com", "ec2.internal", "compute.internal", "on.aws", "api.aws")
# Dominios públicos que no identifican al cliente (namespaces XML/SVG, CDNs, AWS).
DOMINIOS_PUBLICOS = {"amazon.com", "aws.amazon.com", "amazontrust.com", "awsstatic.com", "aws.dev",
                     "w3.org", "diagrams.net", "draw.io", "jgraph.com", "googleapis.com", "gstatic.com",
                     "google.com", "github.com", "githubusercontent.com", "cloudflare.com", "jsdelivr.net",
                     "schema.org", "mozilla.org", "example.com", "example.net", "example.org"}
# TLDs que se consideran dominio. Se excluyen a propósito los que chocan con extensiones de
# archivo (md, py, sh, js, json...), para no tratar "findings.md" como dominio.
TLDS = ("com|net|org|io|co|cloud|dev|app|ai|info|biz|us|uk|es|mx|ar|cl|pe|br|ec|uy|pa|cr|gt|ve|bo|do|"
        "hn|sv|ni|de|fr|it|nl|eu|ca|au|in|me|tech|xyz|online|site|store|pay|bank|finance|money|"
        "global|lat|internal|local|corp|lan|aws|shop|club|agency|digital|health|cat|"
        "vn|ph|th|pk|ng|ke|ma|eg|"
        # ccTLDs europeos y otros frecuentes, y gov/mil/edu (antes un dominio .pt pasaba la verificación)
        "pt|ch|be|at|se|dk|no|fi|ie|cz|pl|gr|ro|hu|lu|sk|si|hr|bg|lt|lv|ee|is|li|mt|cy|"
        "jp|cn|kr|sg|hk|tw|nz|za|ru|ua|tr|il|ae|sa|gov|mil|edu")
SEGUNDO_NIVEL = {"com", "co", "org", "net", "gov", "gob", "edu", "ac"}

RE_DOMINIO = re.compile(
    # Bordes: no se empieza pegado a letra/@/-, ni tras "." salvo en un wildcard "*.dominio" (SANs de ACM), ni en
    # el "40" de un email codificado (%40dominio); sí tras %2F/%3A de una URL codificada.
    r"(?<![\w@-])(?<![^*]\.)(?<!^\.)(?!(?<=%)40)((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + TLDS +
    r"|xn--[a-z0-9-]{2,59}))"
    r"(?![\w-]|\.[a-z0-9])",
    re.IGNORECASE)
RE_EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]+)@((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])", re.IGNORECASE)
RE_IP = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?(?!\.?\d)")
# Candidatos IPv6 (luego se validan con ipaddress): sin letra, dígito, ":", "-", "_", "/" ni "." pegados, para
# no tomar ARNs (apigateway:eu-west-1::/restapis, s3:::bucket); tras un solo ":" sí, si empieza con hex
# ("IPv6:2a05::1"). Las horas (10:00:00) no son IPv6 válidas. Sin "." después: ::ffff:1.2.3.4 es IPv4 (RE_IP).
RE_IP6 = re.compile(r"(?:(?<![0-9A-Za-z.:_/-])|(?<=[^:][:/])(?=[0-9A-Fa-f]))"
                    r"((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?:/(\d{1,3}))?(?![0-9A-Za-z:.])")
RE_EMAIL_URL = re.compile(r"(?<![\w.+-])([\w.+-]+)%40((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])", re.IGNORECASE)
# Organizations: o-<10-32> (organización), r-<4> con algún dígito (root; sin dígito chocaría con ids como
# r-meta del reporte), ou-<root>-<8-32> (unidad organizativa).
RE_ORG = re.compile(r"(?<![A-Za-z0-9-])((?:o-[a-z0-9]{10,32})|(?:ou-[a-z0-9]{4,32}-[a-z0-9]{8,32})|(?:r-(?=[a-z]{0,3}\d)[a-z0-9]{4}))"
                    r"(?![A-Za-z0-9-])")
# Campos del contrato v2 que también viajan como texto (JSON embebido en reporte.html).
RE_CAMPO_FINGERPRINT = re.compile(r"(\"fingerprint\"\s*:\s*\")([0-9a-f]{16})(\")")
RE_CAMPO_ORG_ALIAS = re.compile(r"(\"org_alias\"\s*:\s*\")([^\"\\]+)(\")")
RE_12_GUIONES = re.compile(r"(?<![\d-])(\d{4})-(\d{4})-(\d{4})(?![\d-])")
RE_IP_GUIONES = re.compile(r"(?<![a-z0-9])(ec2|ip)-(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})(?![\d])", re.IGNORECASE)
RE_12 = re.compile(r"(?<!\d)(\d{12})(?!\d)")
RE_UUID = re.compile(r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
                     re.IGNORECASE)
RE_ACCESS_KEY = re.compile(r"(?<![A-Z0-9])((?:AKIA|ASIA)[A-Z0-9]{16})(?![A-Z0-9])")
PREFIJOS_ID = ("tgw-attach|tgw-rtb|vpce-svc|rtbassoc|aclassoc|eipalloc|eipassoc|subnet|snap|vol|ami|sgr|sg|"
               "vpc|eni|eigw|igw|nat|rtb|acl|tgw|vpce|pcx|lt|dopt|cgw|vgw|vpn|fsap|fsmt|fs|pl|sir|fleet|r|i")
# Los bordes son solo "no alfanumérico": un ID puede ir pegado a _ ! / - (AwsBackup_i-0abc..._<uuid>,
# s2svpn!vpn-0abc...-XXXXXX). Deben ser tan permisivos como los de la verificación.
RE_ID_HEX = re.compile(r"(?<![A-Za-z0-9])(" + PREFIJOS_ID + r")-([0-9a-f]{8}|[0-9a-f]{17})(?![A-Za-z0-9])")
RE_ID_RDS = re.compile(r"(?<![A-Za-z0-9])(db|cluster)-([A-Z0-9]{26})(?![A-Za-z0-9])")
# Route 53 hosted zones (Z...) y distribuciones CloudFront (E...): mayúsculas + al menos un dígito.
RE_ID_MAYUS = re.compile(r"(?<![A-Za-z0-9-])(Z[0-9A-Z]{9,31}|E[0-9A-Z]{12,13})(?![A-Za-z0-9])")
# Logical IDs de CDK: Nombre + hash de 8 hex (Database4F1A9C2E). Servicios como RDS los pasan a
# minúsculas dentro del nombre físico (<stack>-dev-database4f1a9c2e-<aleatorio>): se cubren ambas formas.
# El hash puede venir cortado a 5-7 (nombres IAM de 64 caracteres) antes de "-<aleatorio>", o seguido de la
# constante de CDK para SecretTargetAttachment.
CDK_ATTACH = "3fdaad7efa858a3daf9490cf0a702aeb"
RE_CDK_CAMEL = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Za-z0-9]*?[A-Za-z])([0-9A-F]{8}|[0-9A-F]{5,7}(?=-))"
                          r"(" + CDK_ATTACH + r")?(?![A-Z0-9])")
RE_CDK_BAJO = re.compile(r"(?<![A-Za-z0-9])([a-z]{3,}?)([0-9a-f]{8})(?![A-Za-z0-9])")
# Nombres compuestos (a-b_c!d): cada parte con pinta de aleatoria (CloudFormation, Beanstalk, CDK) se mapea.
# Posesivo y sin "…" al final: las etiquetas truncadas las maneja _truncados.
RE_COMPUESTO = re.compile(r"[A-Za-z0-9]++(?:[-_!][A-Za-z0-9]++)++(?!…)")
# Sufijo aleatorio de 6 caracteres de Secrets Manager (en el ARN y en secretos de servicio vpn-...-XXXXXX).
RE_SECRETO_SUF = re.compile(r"(:secret:[^\s\"'<>,;)\]}]+-)([A-Za-z0-9]{6})(?=$|[\s\"'<>,;)\]}])")
# IDs de API Gateway (REST y HTTP/WebSocket) dentro de ARNs y rutas.
RE_APIGW = re.compile(r"(/(?:restapis|apis)/)([a-z0-9]{10})(?![A-Za-z0-9])")
# IDs de petición en mensajes de error de AWS (RequestID: X, HostID: <base64>).
RE_REQUEST_ID = re.compile(r"((?:Request ?ID|Host ?ID|x-amz-request-id|x-amz-id-2)\s*[:=]\s*\"?)([A-Za-z0-9+/=-]{8,})",
                           re.IGNORECASE)
# UUIDs codificados en base64 (ej. version_id de algunos servicios): 36 bytes → 48 caracteres.
RE_UUID_B64 = re.compile(r"(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{48})(?![A-Za-z0-9+/=])")
RE_VPN_SUF = re.compile(r"(?<=vpn-[0-9a-f]{17}-)([A-Za-z0-9]{6})(?![A-Za-z0-9])")
RE_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(?:\d{12})?:[^\s\"'<>,;)\]}]+")
# Partes aleatorias dentro de un ARN (ej. .../app/mi-alb/50dc6c495c0c9188). No se miran las que siguen a
# un guion: esas son sufijos de IDs de recurso (i-0abc...), que ya mapeó su propio paso.
RE_TOKEN_ARN = re.compile(r"(?<=[/:_.])([0-9A-Za-z]{16,})(?=$|[/:_.-])")

RE_COSTO = re.compile(r"(\$\s?)(\d(?:[\d,]*\d)?(?:\.\d+)?)|(?<![\w.,])(\d(?:[\d,]*\d)?(?:\.\d+)?)(\s?USD\b)")
# \w acotado: sin tope es cuadrático en secuencias largas de letras.
RE_COSTO_CLAVE = re.compile(r"([\"']?)(\w{0,64}(?:usd|cost|costo)\w{0,64})\1(\s*:\s*)(-?\d+(?:\.\d+)?)", re.IGNORECASE)
RE_MENSUAL = re.compile(r"^\s*(?:USD\s*)?(?:/\s*mes|/\s*month|/\s*mo\b|al mes|por mes|mensual|per month)",
                        re.IGNORECASE)
RE_CLAVE_COSTO = re.compile(r"usd|cost|costo", re.IGNORECASE)
RE_CLAVE_MENSUAL = re.compile(r"month|mensual|(?<![a-z])mes(?![a-z])", re.IGNORECASE)

# Valor numérico de una medida en JSON ("stored_bytes": 225339777173): 12 dígitos que no son una cuenta.
RE_CLAVE_MEDIDA = re.compile(r"\"\w*(?:bytes|size|count|_gb|_mb|seconds|_ms)\w*\"\s*:\s*$", re.IGNORECASE)
# Campos (o tags) cuyo valor es una persona: no hay patrón que la reconozca en texto libre, pero la clave sí.
RE_CLAVE_PERSONA = re.compile(r"^(?:owner|user_?name|created_?by|creator|contact|responsable|dueñ?o|author)$",
                              re.IGNORECASE)
RE_DIAGRAMA = re.compile(r"(<diagram\b[^>]*>)(.*?)(</diagram>)", re.DOTALL)


def pinta_de_id(s):
    """Un token sin espacios, >= 8 caracteres alfanuméricos, con letras y dígitos (ej. k7xw2pq9mz)."""
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,}", s)) and bool(re.search(r"\d", s)) and bool(re.search(r"[A-Za-z]", s))


def es_aleatorio(seg, ultimo=False):
    """Parte de un nombre compuesto que parece generada. Nunca palabra+número (prod2024, tunnel1).
    - En cualquier posición: >= 10 alfanuméricos con letras y dígitos, sin palabras CamelCase (EC2ContainerRole).
    - Como última parte: 5 a 13 caracteres con letras y dígitos, aunque tenga mayúsculas mezcladas. Es el
      sufijo que agregan CloudFormation (Stack-Logical-qeNTEC75Ilgj), la consola (role-6a7jkupz), Quick
      Setup (…-1r308) o los buckets/log groups (…-c181a260)."""
    if not (re.fullmatch(r"[A-Za-z0-9]+", seg) and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg)) \
            or re.fullmatch(r"[A-Za-z]+\d{1,4}|\d{1,4}[A-Za-z]+", seg):
        return False
    if ultimo and 5 <= len(seg) <= 13:
        return True
    return len(seg) >= 10 and not re.search(r"[A-Z][a-z]{2,}", seg)


class IndicePrefijos:
    """Busca si un fragmento (ej. una etiqueta truncada "vpn-2baa9935…") es el comienzo de un valor conocido."""

    def __init__(self, pares):
        self.claves = sorted(pares)
        self.pares = pares

    def buscar(self, frag):
        f = frag.lower()
        i = bisect.bisect_left(self.claves, f)
        if i < len(self.claves) and self.claves[i].startswith(f) and self.claves[i] != f:
            return self.claves[i]
        return None


# Acotado a 255: sin tope, una línea larga sin "…" es cuadrática (40k caracteres tardaban ~5 s).
RE_TRUNCADO = re.compile(r"([A-Za-z0-9][A-Za-z0-9!_./:-]{0,255})(?=…)")


def fragmentos_truncados(texto):
    """Para cada etiqueta cortada con "…", devuelve (inicio, fin) de cada sufijo que empieza en un separador,
    con >= 6 caracteres y al menos un dígito (candidatos a ser el comienzo de un ID)."""
    for m in RE_TRUNCADO.finditer(texto):
        run = m.group(1)
        cortes = [0] + [i + 1 for i, c in enumerate(run) if c in "!_./:-"]
        for c in cortes:
            frag = run[c:]
            if len(frag) >= 6 and re.search(r"\d", frag):
                yield m.start() + c, m.end()


def trie_regex(palabras):
    """Regex alternada en forma de árbol (rápida con miles de literales). Espera palabras en minúsculas
    si se compila con IGNORECASE."""
    arbol = {}
    for w in palabras:
        nodo = arbol
        for ch in w:
            nodo = nodo.setdefault(ch, {})
        nodo[""] = {}

    def armar(nodo):
        hijos = sorted(k for k in nodo if k)
        if not hijos:
            return ""
        alts = [re.escape(ch) + armar(nodo[ch]) for ch in hijos]
        r = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return f"(?:{r})?" if "" in nodo else r

    return armar(arbol)


def con_caja_de(hallado, original, falso):
    if hallado == original:
        return falso
    if hallado.islower() or not any(c.isalpha() for c in hallado):
        return falso.lower()
    if hallado.isupper():
        return falso.upper()
    return falso


def slug_nombre(s):
    """El mismo slug que 4-esquematizar/esquematizar.py usa en los nombres de archivo de los diagramas."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "")).strip("-")[:60]


def es_ip(texto):
    try:
        return ipaddress.IPv4Address(texto)
    except ValueError:
        return None


def ip_publica(ip):
    return ip.is_global and not ip.is_multicast


def es_ip6(texto):
    try:
        return ipaddress.IPv6Address(texto)
    except ValueError:
        return None


REDES_DOC = [ipaddress.IPv4Network(r) for r in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
REDES_PRIVADAS = [ipaddress.IPv4Network(r) for r in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]


def ip_privada(ip):
    return any(ip in r for r in REDES_PRIVADAS)


def etiqueta_aleatoria(p):
    """Parte de un hostname de AWS con pinta de ID: alfanumérica con dígitos, o solo dígitos (>= 8)."""
    return pinta_de_id(p) or (p.isdigit() and len(p) >= 8 and len(p) != 12)


def dominio_registrable(host):
    partes = host.lower().rstrip(".").split(".")
    if len(partes) >= 3 and partes[-2] in SEGUNDO_NIVEL and len(partes[-1]) == 2:
        return ".".join(partes[-3:])
    return ".".join(partes[-2:])


def sufijo_aws(host):
    h = host.lower().rstrip(".")
    return next((s for s in DOMINIOS_AWS if h == s or h.endswith("." + s)), None)


def es_publico(host, extra):
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in DOMINIOS_PUBLICOS | extra)


# --------------------------------------------------------------------------- mapeo

class Mapeo:
    """Guarda real→falso por categoría. Los falsos salen de HMAC(sal, valor): son estables
    mientras se reuse el mismo archivo de mapeo y no se pueden revertir sin él."""

    CATEGORIAS = ("account_id", "account_name", "resource_id", "uuid", "token", "cdk", "ip", "ip_private",
                  "ip6", "domain", "email", "access_key", "term", "person", "org", "fingerprint")

    def __init__(self, datos=None):
        datos = datos or {}
        self.sal = datos.get("salt") or secrets.token_hex(16)
        self.m = {c: dict(datos.get("categories", {}).get(c, {})) for c in self.CATEGORIAS}
        self.nombres_genericos = set(datos.get("generic_account_names", []))
        self.prohibidos = []  # términos de marca: ningún falso puede contenerlos
        self._rehacer_inversos()

    def _rehacer_inversos(self):
        self.falsos = {c: set(v.values()) for c, v in self.m.items()}
        # Partes de los falsos (ej. el hex de i-<hex>): el paso de nombres compuestos no debe re-mapearlas.
        self.seg_falsos = set()
        for c in ("resource_id", "uuid", "token", "cdk", "access_key"):
            for f in self.falsos[c]:
                self.seg_falsos.update(p.lower() for p in re.split(r"[-_!/:.]", f))

    def a_dict(self):
        return {"version": 1, "salt": self.sal, "generic_account_names": sorted(self.nombres_genericos),
                "categories": self.m}

    def _hash(self, cat, valor, intento):
        return hmac.new(self.sal.encode(), f"{cat}|{valor}|{intento}".encode(), hashlib.sha256).digest()

    def _valido(self, falso, cat):
        bajo = falso.lower()
        if any(t in bajo for t in self.prohibidos):
            return False
        if cat != "account_id" and RE_12.search(falso):
            return False
        return falso not in self.falsos[cat]

    def obtener(self, cat, real, generador):
        """Devuelve el falso de `real`, creándolo con generador(bytes) si no existe."""
        if real in self.falsos[cat]:  # ya es un valor anónimo: idempotente
            return real
        if real in self.m[cat]:
            return self.m[cat][real]
        for intento in range(1000):
            falso = generador(self._hash(cat, real, intento))
            if self._valido(falso, cat) and falso != real:
                break
        else:
            raise RuntimeError(f"No se pudo generar un valor falso para {cat}")
        self.m[cat][real] = falso
        self.falsos[cat].add(falso)
        self.seg_falsos.update(p.lower() for p in re.split(r"[-_!/:.]", falso))
        return falso

    def siguiente(self, cat, real, plantilla):
        """Falsos secuenciales (cuenta-01, ejemplo2.com, usuario3@...)."""
        if real in self.falsos[cat]:
            return real
        if real not in self.m[cat]:
            n = len(self.m[cat]) + 1
            while plantilla(n) in self.falsos[cat]:
                n += 1
            self.m[cat][real] = plantilla(n)
            self.falsos[cat].add(plantilla(n))
        return self.m[cat][real]


def hex_de(h, n):
    return h.hex()[:n] if n <= 64 else (h.hex() * 2)[:n]


def alnum_como(original, h):
    """Mismo largo y misma clase de caracteres que el original."""
    if original.isdigit():
        abc = "0123456789"
    elif re.fullmatch(r"[0-9a-f]+", original):
        abc = "0123456789abcdef"
    elif re.fullmatch(r"[0-9A-F]+", original):
        abc = "0123456789ABCDEF"
    elif re.fullmatch(r"[0-9A-Z]+", original):
        abc = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    elif re.fullmatch(r"[0-9a-z]+", original):
        abc = "0123456789abcdefghijklmnopqrstuvwxyz"
    else:
        abc = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    semilla = int.from_bytes(h, "big")
    salida = []
    for _ in original:
        semilla, r = divmod(semilla, len(abc))
        if semilla == 0:
            semilla = int.from_bytes(hashlib.sha256(h + bytes(len(salida))).digest(), "big")
        salida.append(abc[r])
    return "".join(salida)


# --------------------------------------------------------------------------- anonimizador

class Anonimizador:
    def __init__(self, mapeo, terminos, costos="rounded", ocultar_privadas=False, total_usd=None, meses=None,
                 dominios_publicos=()):
        self.mp = mapeo
        self.costos = costos
        self.ocultar_privadas = ocultar_privadas
        self.total = total_usd
        self.meses = meses or 1
        self.dom_publicos = {d.lower() for d in dominios_publicos}
        if isinstance(terminos, dict):
            self.terminos = {k.lower(): v for k, v in terminos.items() if k}
        else:
            self.terminos = {t.lower(): "empresa" for t in terminos or [] if t}
        for real, falso in self.terminos.items():
            self.mp.m["term"][real] = falso
        self.mp.prohibidos = list(self.terminos)
        self.re_terminos = (re.compile("|".join(re.escape(t) for t in sorted(self.terminos, key=len, reverse=True)),
                                       re.IGNORECASE) if self.terminos else None)
        self.re_nombres = None
        self._nombres_bajo = {}
        self._final, self._final_n = [], None
        self.nombres_por_id = {}

    # ---- nombres de cuenta

    def registrar_cuentas(self, pares):
        """pares: {account_id: nombre}. Numera cuenta-NN en orden de account_id (determinista)."""
        for acc_id, nombre in sorted(pares.items()):
            self.cuenta(acc_id)
            if not nombre:
                continue
            self.nombres_por_id[acc_id] = nombre
            bajo = nombre.lower()
            amb = []
            for clave, sufijo in AMBIENTES:
                if re.search(r"(?<![a-z])" + clave, bajo) and sufijo not in amb:
                    amb.append(sufijo)
            self.mp.siguiente("account_name", nombre,
                              lambda n, amb=amb: f"cuenta-{n:02d}" + "".join(f"-{a}" for a in amb))
            if bajo.strip() in GENERICOS:
                self.mp.nombres_genericos.add(nombre)
        self._compilar_nombres()

    def _compilar_nombres(self):
        nombres = [n for n in self.mp.m["account_name"] if n not in self.mp.nombres_genericos]
        self._nombres_bajo = {n.lower(): f for n, f in self.mp.m["account_name"].items()}
        # El slug con que esquematizar nombra los diagramas (<cuenta>-Jos-N-ez.drawio): va pegado a un "-".
        slugs = {slug_nombre(n): slug_nombre(self.mp.m["account_name"][n]) for n in nombres}
        self._slugs = {s.lower(): f for s, f in slugs.items() if len(s) >= 4 and s.lower() not in self._nombres_bajo}
        self.re_slugs = (re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(s) for s in sorted(
            self._slugs, key=len, reverse=True)) + r")(?=\.drawio|\.xml|\.png|\.svg|[\"'/\s]|$)", re.IGNORECASE)
            if self._slugs else None)
        nombres.sort(key=len, reverse=True)
        self.re_nombres = (re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in nombres) + r")(?![\w-])",
                                      re.IGNORECASE) if nombres else None)

    def nombre_cuenta(self, nombre):
        return self.mp.m["account_name"].get(nombre) or self._nombres_bajo.get(nombre.lower(), nombre)

    # ---- generadores por categoría

    def _cuenta_partes(self, acc_id):
        falso = self.cuenta(acc_id)
        return falso[:4], falso[4:8], falso[8:]

    def cuenta(self, acc_id):
        return self.mp.obtener("account_id", acc_id, lambda h: str(int.from_bytes(h[:8], "big") % 9 * 10**11
                                                                    + 10**11 + int.from_bytes(h[8:16], "big") % 10**11))

    def id_recurso(self, prefijo, sufijo):
        return self.mp.obtener("resource_id", f"{prefijo}-{sufijo}", lambda h: f"{prefijo}-{alnum_como(sufijo, h)}")

    def token(self, t):
        """Tokens largos se mapean sin distinguir mayúsculas (CloudFormation los genera en mayúsculas y RDS
        los guarda en minúsculas); los cortos (< 8) sí distinguen, para no chocar con palabras."""
        if t in self.mp.falsos["token"] or t.lower() in self.mp.falsos["token"]:
            return t
        if len(t) < 8:
            return self.mp.obtener("token", t, lambda h: alnum_como(t, h))
        falso = self.mp.m["token"].get(t) or self.mp.obtener("token", t.lower(), lambda h: alnum_como(t.lower(), h))
        return con_caja_de(t, t.lower(), falso) if not any(c.islower() for c in t) else falso

    def cdk(self, prefijo, hexa):
        if hexa.upper() in self.mp.falsos["cdk"]:
            return prefijo + hexa
        falso = self.mp.obtener("cdk", hexa.upper(), lambda h: alnum_como(hexa.upper(), h))
        return prefijo + (falso.lower() if hexa.islower() else falso)

    def _cdk_sub(self, m):
        prefijo, hexa = m.group(1), m.group(2)
        resto = (m.group(3) or "") if m.re is RE_CDK_CAMEL else ""
        todo = prefijo + hexa + resto
        if (prefijo + hexa).lower() in self.mp.seg_falsos or hexa.upper() in self.mp.falsos["cdk"]:
            return todo
        if re.fullmatch(r"[0-9a-fA-F]+", prefijo + hexa) or not re.search(r"\d", hexa) \
                or not re.search(r"[a-z]", prefijo):
            return todo
        # En minúsculas se exige una letra a-f en el hash, para no tocar palabra+fecha (build20240805).
        if m.re is RE_CDK_BAJO and not re.search(r"[a-f]", hexa):
            return todo
        return self.cdk(prefijo, hexa) + resto

    def _uuid_b64_sub(self, m):
        try:
            u = base64.b64decode(m.group(1), validate=True).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            return m.group(1)
        if not RE_UUID.fullmatch(u):
            return m.group(1)
        return base64.b64encode(self.uuid(u).encode()).decode()

    def _compuesto_sub(self, m):
        partes = re.split(r"([-_!])", m.group(0))
        for i in range(0, len(partes), 2):
            seg = partes[i]
            if seg.lower() in self.mp.seg_falsos or not es_aleatorio(seg, ultimo=i == len(partes) - 1):
                continue
            cdk = RE_CDK_BAJO.fullmatch(seg) or RE_CDK_CAMEL.fullmatch(seg)
            if cdk and cdk.group(2).upper() in self.mp.falsos["cdk"]:
                continue  # ya es Nombre+hash falso del paso CDK
            partes[i] = self.token(seg)
        return "".join(partes)

    def uuid(self, u):
        return self.mp.obtener("uuid", u.lower(), lambda h: "-".join(
            [h.hex()[:8], h.hex()[8:12], "4" + h.hex()[13:16], "8" + h.hex()[17:20], h.hex()[20:32]]))

    def ip(self, texto):
        ip = es_ip(texto)
        if ip is None:
            return texto
        if ip_publica(ip):
            def gen(h):
                # Primero los rangos de documentación (762 direcciones); si se llenan, 198.18.0.0/15
                # (benchmarking, RFC 2544), que tampoco es enrutable.
                if len(self.mp.m["ip"]) < 700:
                    red = ("203.0.113", "198.51.100", "192.0.2")[h[0] % 3]
                    return f"{red}.{1 + h[1] % 254}"
                return f"198.{18 + h[0] % 2}.{h[1]}.{1 + h[2] % 254}"
            return self.mp.obtener("ip", texto, gen)
        if self.ocultar_privadas and ip_privada(ip):
            o = texto.split(".")
            red = self.mp.obtener("ip_private", "red:" + ".".join(o[:2]), lambda h: f"10.{1 + h[0] % 254}")
            falso = f"{red}.{o[2]}.{o[3]}"
            if falso != texto:
                self.mp.m["ip_private"][texto] = falso
                self.mp.falsos["ip_private"].add(falso)
            return falso
        return texto

    def dominio(self, reg):
        return self.mp.siguiente("domain", reg.lower(), lambda n: "ejemplo.com" if n == 1 else f"ejemplo{n}.com")

    def org_id(self, valor):
        partes = valor.split("-")  # o-x, r-x, ou-<root>-<ou>: cada parte en minúsculas, mismo largo
        return self.mp.obtener("org", valor, lambda h: "-".join(
            [partes[0]] + ["".join("0123456789abcdefghijklmnopqrstuvwxyz"[b % 36] for b in
                                   hashlib.sha256(h + p.encode()).digest()[:len(p)]) for p in partes[1:]]))

    def org_alias(self, valor):
        if RE_ORG.fullmatch(valor):
            return self.org_id(valor)
        return self.mp.siguiente("org", valor, lambda n: f"org-{n:02d}")

    def huella(self, valor):
        """El fingerprint es un sha256 sin sal: con los IDs de cuenta (10^12) se podría revertir. En la copia
        anónima se rehace con la sal del mapeo: sigue siendo estable entre corridas que reusan el mapeo."""
        return self.mp.obtener("fingerprint", valor, lambda h: h.hex()[:16])

    def ip6(self, texto):
        """IPv6 pública → 2001:db8::/32 (documentación). Las privadas (ULA, link-local) se dejan, como las
        IPv4 privadas. Cada dirección se mapea por separado: la contención VPC /56 ⊃ subred /64 no se conserva."""
        ip = es_ip6(texto)
        if ip is None:
            return texto
        if ip_publica(ip):
            return self.mp.obtener("ip6", str(ip), lambda h: str(ipaddress.IPv6Address(b"\x20\x01\x0d\xb8" + h[:12])))
        if self.ocultar_privadas and ip.is_private and not ip.is_loopback and str(ip) != "::":
            return self.mp.obtener("ip6", str(ip), lambda h: str(ipaddress.IPv6Address(b"\xfd" + h[:15])))
        return texto

    def email(self, local, dom):
        if es_publico(dom, self.dom_publicos) or sufijo_aws(dom):
            return f"{local}@{dom}"
        if re.fullmatch(r"usuario\d*", local) and dominio_registrable(dom) in self.mp.falsos["domain"]:
            return f"{local}@{dom}"  # ya anónimo
        falso = self.mp.siguiente("email", f"{local}@{dom}".lower(),
                                  lambda n: "usuario@" if n == 1 else f"usuario{n}@")
        return falso + self._host(dom)

    # ---- pasos de texto

    def _host(self, host):
        if sufijo_aws(host):
            etiquetas = host.split(".")
            return ".".join("-".join(self.token(p) if etiqueta_aleatoria(p) else p for p in e.split("-"))
                            for e in etiquetas)
        if es_publico(host, self.dom_publicos):
            return host
        reg = dominio_registrable(host)
        if reg in self.mp.falsos["domain"]:
            return host
        sub = host[: len(host) - len(reg)]
        return sub + self.dominio(reg)

    def _costo(self, valor, mensual):
        if self.costos == "exact" or valor == 0:
            return None
        if self.costos == "percent" and self.total:
            base = self.total / self.meses if mensual else self.total
            return ("pct", round(valor / base * 100, 2))
        dig = 2 - int(math.floor(math.log10(abs(valor)))) - 1
        return ("usd", round(valor, dig), max(dig, 0))

    def _costo_texto(self, texto):
        def sub(m):
            num = m.group(2) or m.group(3)
            valor = float(num.replace(",", ""))
            mensual = bool(RE_MENSUAL.match(texto[m.end():m.end() + 16]))
            r = self._costo(valor, mensual)
            if r is None:
                return m.group(0)
            if r[0] == "pct":
                return f"{r[1]}%"
            s = f"{r[1]:,.{r[2]}f}" if "," in num else f"{r[1]:.{r[2]}f}"
            return f"{m.group(1)}{s}" if m.group(2) else f"{s}{m.group(4)}"

        def sub_clave(m):
            valor = float(m.group(4))
            r = self._costo(valor, bool(RE_CLAVE_MENSUAL.search(m.group(2))))
            if r is None:
                return m.group(0)
            return f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{r[1]}"

        texto = RE_COSTO.sub(sub, texto)
        return RE_COSTO_CLAVE.sub(sub_clave, texto)

    def texto(self, t, costos=True):
        if not t:
            return t
        if costos and self.costos != "exact":
            t = self._costo_texto(t)
        t = RE_ACCESS_KEY.sub(lambda m: self.mp.obtener("access_key", m.group(1),
                                                         lambda h: "AKIAEXAMPLE" + alnum_como("A" * 9, h)), t)
        t = RE_CAMPO_FINGERPRINT.sub(lambda m: m.group(1) + self.huella(m.group(2)) + m.group(3), t)
        t = RE_CAMPO_ORG_ALIAS.sub(lambda m: m.group(1) + self.org_alias(m.group(2)) + m.group(3), t)
        t = RE_ORG.sub(lambda m: self.org_id(m.group(1)), t)
        t = RE_EMAIL.sub(lambda m: self.email(m.group(1), m.group(2)), t)
        t = RE_EMAIL_URL.sub(lambda m: self.email(m.group(1), m.group(2)).replace("@", "%40", 1), t)
        t = RE_IP6.sub(lambda m: self.ip6(m.group(1)) + (f"/{m.group(2)}" if m.group(2) else ""), t)
        t = RE_12_GUIONES.sub(lambda m: "-".join(self._cuenta_partes("".join(m.groups()))), t)
        t = RE_IP_GUIONES.sub(lambda m: m.group(1) + "-" + self.ip(".".join(m.group(2, 3, 4, 5))).replace(".", "-"), t)
        t = RE_DOMINIO.sub(lambda m: self._host(m.group(1)), t)
        if self._final_n is not None:
            t = self._truncados(t)
        t = RE_ID_HEX.sub(lambda m: self.id_recurso(m.group(1), m.group(2)), t)
        t = RE_ID_RDS.sub(lambda m: self.id_recurso(m.group(1), m.group(2)), t)
        t = RE_ID_MAYUS.sub(lambda m: self.mp.obtener("resource_id", m.group(1),
                                                       lambda h: m.group(1)[0] + alnum_como(m.group(1)[1:], h))
                            if re.search(r"\d", m.group(1)) and m.group(1).lower() not in self.mp.seg_falsos
                            else m.group(1), t)
        t = RE_UUID.sub(lambda m: self.uuid(m.group(1)), t)
        t = RE_UUID_B64.sub(self._uuid_b64_sub, t)
        t = RE_ARN.sub(lambda m: RE_TOKEN_ARN.sub(
            lambda k: self.token(k.group(1)) if re.search(r"\d", k.group(1)) and not RE_12.fullmatch(k.group(1))
            and k.group(1).lower() not in self.mp.seg_falsos else k.group(1), m.group(0)), t)
        t = RE_REQUEST_ID.sub(lambda m: m.group(1) + (m.group(2) if m.group(2) in self.mp.falsos["token"]
                                                      else self.token(m.group(2))), t)
        t = RE_VPN_SUF.sub(lambda m: self.token(m.group(1)), t)
        t = RE_APIGW.sub(lambda m: m.group(1) + self.token(m.group(2)), t)
        t = RE_SECRETO_SUF.sub(lambda m: m.group(1) + self.token(m.group(2)), t)
        t = RE_CDK_CAMEL.sub(self._cdk_sub, t)
        t = RE_CDK_BAJO.sub(self._cdk_sub, t)
        t = RE_COMPUESTO.sub(self._compuesto_sub, t)
        t = self._pasada_final(t)
        t = RE_12.sub(lambda m: self.cuenta(m.group(1)), t)
        t = RE_IP.sub(lambda m: self.ip(m.group(1)) + (f"/{m.group(2)}" if m.group(2) else ""), t)
        if self.re_nombres:
            t = self.re_nombres.sub(lambda m: self.nombre_cuenta(m.group(1)), t)
        if getattr(self, "re_slugs", None):
            t = self.re_slugs.sub(lambda m: self._slugs[m.group(1).lower()], t)
        t = self._personas(t)
        if self.re_terminos:
            t = self.re_terminos.sub(lambda m: con_caja(m.group(0), self.terminos[m.group(0).lower()]), t)
        return t

    CATS_FINAL = ("token", "cdk", "uuid", "access_key", "resource_id")  # la última gana si se repite

    def congelar(self):
        """Compila todo lo aprendido en la primera pasada. La pasada final reemplaza cualquier valor
        aprendido en cualquier archivo o contexto, en cualquier caja, donde sea que aparezca."""
        largos, cortos = {}, {}
        for cat in self.CATS_FINAL:
            for real, falso in self.mp.m[cat].items():
                if len(real) >= 8:
                    largos[real.lower()] = (real, falso)
                else:
                    cortos[real] = (real, falso)
        self._final = []
        if largos:
            self._final.append((re.compile(r"(?<![A-Za-z0-9])(" + trie_regex(largos) + r")(?![A-Za-z0-9])",
                                           re.IGNORECASE), largos, str.lower))
        if cortos:
            self._final.append((re.compile(r"(?<![A-Za-z0-9])(" + trie_regex(cortos) + r")(?![A-Za-z0-9])"),
                                cortos, lambda x: x))
        self._final_n = sum(len(self.mp.m[c]) for c in self.CATS_FINAL)
        self._prefijos = IndicePrefijos({k: v for k, v in {**largos, **{r.lower(): v for r, v in cortos.items()}}.items()
                                         if len(k) >= 8})

    def _truncados(self, t):
        """Etiquetas cortadas con "…" (draw.io): si el fragmento es el comienzo de un valor conocido, se
        reemplaza por el comienzo de su falso; si no, se trata como token."""
        if "…" not in t:
            return t
        cambios = []
        ocupado = -1
        for ini, fin in fragmentos_truncados(t):
            if ini < ocupado:
                continue
            frag = t[ini:fin]
            clave = self._prefijos.buscar(frag)
            if clave:
                real, falso = self._prefijos.pares[clave]
                cambios.append((ini, fin, con_caja_de(frag, real[:len(frag)], falso[:len(frag)])))
                ocupado = fin
        # Sin valor conocido: la última parte cortada, si parece aleatoria, se trata como token.
        for m in RE_TRUNCADO.finditer(t):
            if any(ini < m.end() and m.start() < fin for ini, fin, _ in cambios):
                continue
            ultima = re.split(r"[!_./:-]", m.group(1))[-1]
            if len(ultima) >= 6 and pinta_de_id(ultima) and ultima.lower() not in self.mp.seg_falsos \
                    and not re.fullmatch(r"[A-Za-z]+\d{1,4}|\d{1,4}[A-Za-z]+", ultima):
                cambios.append((m.end() - len(ultima), m.end(), self.token(ultima)))
        cambios.sort()
        for ini, fin, nuevo in reversed(cambios):
            t = t[:ini] + nuevo + t[fin:]
        return t

    def _pasada_final(self, t):
        if self._final_n is None:
            return t  # primera pasada: solo se aprende
        for patron, tabla, clave in self._final:
            t = patron.sub(lambda m: con_caja_de(m.group(1), *tabla[clave(m.group(1))]), t)
        return t

    # ---- JSON

    def persona(self, valor):
        if "@" in valor or not re.search(r"[A-Za-z]", valor) or len(valor.strip()) < 3:
            return self.texto(valor)  # email, número o sigla: el paso de texto normal
        if valor in self.mp.falsos["person"]:
            return valor
        return self.mp.siguiente("person", valor.strip().lower(), lambda n: f"persona{n}")

    def _personas(self, t):
        """Reemplaza en texto libre (reporte.html, .md) las personas aprendidas de los campos JSON."""
        reales = sorted((r for r in self.mp.m["person"] if len(r) >= 3), key=len, reverse=True)
        if not reales:
            return t
        if getattr(self, "_re_personas_n", None) != len(reales):
            self._re_personas = re.compile(r"(?<![\w-])(" + "|".join(re.escape(r) for r in reales) + r")(?![\w-])",
                                           re.IGNORECASE)
            self._re_personas_n = len(reales)
        return self._re_personas.sub(lambda m: self.mp.m["person"][m.group(1).lower()], t)

    def json(self, obj, ruta=()):
        if isinstance(obj, dict):
            salida = {}
            for k, v in obj.items():
                k2 = self.texto(k, costos=False) if isinstance(k, str) else k
                if isinstance(k, str) and isinstance(v, str) and RE_CLAVE_PERSONA.match(k):
                    salida[k2] = self.persona(v)
                elif k == "run_id" and isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{6}Z)?", v):
                    salida[k2] = v
                elif k == "fingerprint" and isinstance(v, str):
                    salida[k2] = self.huella(v)
                elif k == "org_alias" and isinstance(v, str):
                    salida[k2] = self.org_alias(v)
                elif (k == "name" and len(ruta) >= 2 and ruta[-2] == "accounts" and isinstance(v, str)) \
                        or (k == "account_name" and isinstance(v, str)):
                    salida[k2] = self.nombre_cuenta(v) if v in self.mp.m["account_name"] else self.texto(v)
                else:
                    salida[k2] = self.json(v, ruta + (k,))
            return salida
        if isinstance(obj, list):
            return [self.json(v, ruta) for v in obj]
        if isinstance(obj, str):
            return self.texto(obj)
        if isinstance(obj, (int, float)) and not isinstance(obj, bool) and self._es_costo(ruta):
            r = self._costo(float(obj), any(RE_CLAVE_MENSUAL.search(str(k)) for k in ruta))
            return obj if r is None else r[1]
        return obj

    @staticmethod
    def _es_costo(ruta):
        claves = [str(k) for k in ruta]
        return any(RE_CLAVE_COSTO.search(k) for k in claves) or "services" in claves

    # ---- drawio

    def drawio(self, t):
        """Descomprime cada <diagram> a XML plano (draw.io lo abre igual) y anonimiza todo una sola vez.
        Nunca se aplica el texto sobre el base64: un reemplazo ahí corrompería el diagrama."""
        def sub(m):
            contenido = m.group(2).strip()
            if contenido and not contenido.startswith("<"):
                xml = descomprimir_diagrama(contenido)
                if xml is None:
                    raise ValueError("diagrama comprimido que no se pudo descomprimir")
                return m.group(1) + xml + m.group(3)
            return m.group(0)
        return self.texto(RE_DIAGRAMA.sub(sub, t))


def con_caja(original, reemplazo):
    if original.isupper():
        return reemplazo.upper()
    if original[:1].isupper():
        return reemplazo[:1].upper() + reemplazo[1:]
    return reemplazo


def descomprimir_diagrama(b64):
    """draw.io guarda cada <diagram> como base64(deflate crudo(urlencode(xml)))."""
    try:
        return urllib.parse.unquote(zlib.decompress(base64.b64decode(b64), -15).decode("utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------- preescaneo

def preescanear(archivos):
    """Junta cuentas (id→nombre), costo total, meses e IDs de recurso "raros" de los JSON."""
    cuentas, tokens, total, meses = {}, set(), None, None

    def recorrer(o, ruta=()):
        if isinstance(o, dict):
            if ruta and ruta[-1] == "accounts":
                for k, v in o.items():
                    if RE_12.fullmatch(str(k)):
                        nombre = v.get("name") if isinstance(v, dict) else None
                        if nombre or k not in cuentas:
                            cuentas[k] = nombre or cuentas.get(k)
            if "account_id" in o and RE_12.fullmatch(str(o["account_id"])):
                cuentas.setdefault(str(o["account_id"]), None)
                if o.get("account_name") and not cuentas[str(o["account_id"])]:
                    cuentas[str(o["account_id"])] = o["account_name"]
            if "service" in o and isinstance(o.get("id"), str) and pinta_de_id(o["id"]):
                tokens.add(o["id"])
            for k, v in o.items():
                recorrer(v, ruta + (k,))
        elif isinstance(o, list):
            for v in o:
                recorrer(v, ruta)

    for ruta in archivos:
        if ruta.suffix != ".json":
            continue
        try:
            datos = json.loads(ruta.read_text())
        except (ValueError, UnicodeDecodeError):
            continue
        recorrer(datos)
        if ruta.name == "discovery.json" and isinstance(datos.get("accounts"), dict):
            total = sum(float(a.get("total_usd") or 0) for a in datos["accounts"].values())
            meses = (datos.get("period") or {}).get("months")
    return cuentas, tokens, total, meses


# --------------------------------------------------------------------------- verificación

def contenido_verificable(ruta):
    t = ruta.read_text(errors="replace")
    if ruta.suffix in (".drawio", ".xml"):
        extra = [descomprimir_diagrama(m.group(2).strip()) for m in RE_DIAGRAMA.finditer(t)
                 if m.group(2).strip() and not m.group(2).strip().startswith("<")]
        t += "\n" + "\n".join(x for x in extra if x)
    return t


def verificar(carpeta, mapeo, terminos, dominios_publicos=(), privadas=False):
    """Devuelve una lista de (archivo, motivo, fragmento). Vacía = se puede presentar."""
    problemas = []
    falsos = mapeo.falsos
    extra = {d.lower() for d in dominios_publicos}
    # Un grupo por tipo de borde; cada grupo es una sola regex de árbol (miles de valores, una pasada).
    alnum = (r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])")
    grupos = {  # nombre: (inicio, fin, ignorar_caja, categorías)
        "cuentas": (r"(?<!\d)", r"(?!\d)", False, ("account_id",)),
        "nombres": (r"(?<![\w-])", r"(?![\w-])", True, ("account_name", "person")),
        "ips": (r"(?<![\d.])", r"(?!\d|\.\d)", False, ("ip", "ip_private")),
        "ips6": (r"(?<![0-9A-Za-z:])", r"(?![0-9A-Za-z:])", True, ("ip6",)),
        "dominios": (r"(?<![A-Za-z0-9-])", r"(?![A-Za-z0-9-])", True, ("domain", "email")),
        "ids": (*alnum, True, ("resource_id", "uuid", "token", "access_key", "fingerprint")),
        "orgs": (r"(?<![A-Za-z0-9-])", r"(?![A-Za-z0-9-])", True, ("org",)),
        "ids_cortos": (*alnum, False, ("token",)),
        # el hash CDK puede ir pegado a su prefijo en minúsculas (database4f1a9c2e): sin borde inicial
        "cdk": ("", r"(?![A-Za-z0-9])", True, ("cdk",)),
    }
    originales = []
    for nombre, (ini, fin, icase, cats) in grupos.items():
        tabla = {}
        for cat in cats:
            for real in mapeo.m[cat]:
                if cat == "account_name" and real in mapeo.nombres_genericos:
                    continue
                if cat == "ip_private" and (real.startswith("red:") or not privadas):
                    continue
                if cat == "token" and (len(real) < 8) != (nombre == "ids_cortos"):
                    continue
                tabla[real.lower() if icase else real] = cat
        if tabla:
            patron = re.compile(ini + "(" + trie_regex(tabla) + ")" + fin, re.IGNORECASE if icase else 0)
            originales.append((patron, tabla, icase))
    re_term = re.compile("|".join(re.escape(t) for t in terminos), re.IGNORECASE) if terminos else None
    prefijos = IndicePrefijos({r.lower(): None for c in ("resource_id", "uuid", "token", "cdk", "access_key")
                               for r in mapeo.m[c] if len(r) >= 8})

    for ruta in sorted(p for p in carpeta.rglob("*") if p.is_file()):
        rel = ruta.relative_to(carpeta)
        if ruta.name.endswith(".mapping.json"):
            problemas.append((rel, "archivo de mapeo dentro de la carpeta anónima", ruta.name))
            continue
        if ruta.suffix.lower() not in TEXTO:
            problemas.append((rel, "archivo no verificable (binario)", ruta.suffix))
            continue
        t = contenido_verificable(ruta)

        def reportar(motivo, m):
            ini = max(0, m.start() - 30)
            problemas.append((rel, motivo, t[ini:m.end() + 30].replace("\n", " ")))

        for patron, tabla, icase in originales:
            for m in patron.finditer(t):
                reportar(f"valor original ({tabla[m.group(1).lower() if icase else m.group(1)]})", m)
        for ini, fin in fragmentos_truncados(t):
            if prefijos.buscar(t[ini:fin]):
                problemas.append((rel, "comienzo de un valor original en etiqueta truncada",
                                  t[max(0, ini - 30):fin + 5].replace("\n", " ")))
        for m in RE_12.finditer(t):
            if m.group(1) not in falsos["account_id"] and not RE_CLAVE_MEDIDA.search(t, max(0, m.start() - 60), m.start()):
                reportar("12 dígitos que no son un ID falso", m)
        if re_term:
            for m in re_term.finditer(t):
                reportar("término de marca", m)
        for m in RE_IP.finditer(t):
            ip = es_ip(m.group(1))
            if ip and ip_publica(ip):
                reportar("IP pública", m)
        for m in RE_IP_GUIONES.finditer(t):
            ip = es_ip(".".join(m.group(2, 3, 4, 5)))
            if ip and ip_publica(ip):
                reportar("IP pública en hostname", m)
        for m in RE_IP6.finditer(t):
            ip = es_ip6(m.group(1))
            if ip and ip_publica(ip) and str(ip) not in falsos["ip6"]:
                reportar("IPv6 pública", m)
        for m in RE_ORG.finditer(t):
            if m.group(1) not in falsos["org"]:
                reportar("ID de Organizations sin anonimizar", m)
        for m in RE_12_GUIONES.finditer(t):
            if "".join(m.groups()) not in falsos["account_id"]:
                reportar("ID de cuenta con guiones", m)
        decodificado = html.unescape(urllib.parse.unquote(urllib.parse.unquote(t)))
        if decodificado != t:
            for m in RE_EMAIL.finditer(decodificado):
                if not (es_publico(m.group(2), extra) or sufijo_aws(m.group(2))
                        or dominio_registrable(m.group(2)) in falsos["domain"]):
                    problemas.append((rel, "email codificado (%xx / &#..;) sin anonimizar",
                                      decodificado[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")))
        for m in RE_EMAIL_URL.finditer(t):
            dom = m.group(2)
            if es_publico(dom, extra) or sufijo_aws(dom):
                continue
            if f"{m.group(1).lower()}@" in falsos["email"] and dominio_registrable(dom) in falsos["domain"]:
                continue
            reportar("email (%40) sin anonimizar", m)
        for m in RE_EMAIL.finditer(t):
            dom = m.group(2)
            if es_publico(dom, extra) or sufijo_aws(dom):
                continue
            if f"{m.group(1).lower()}@" in falsos["email"] and dominio_registrable(dom) in falsos["domain"]:
                continue
            reportar("email sin anonimizar", m)
        for m in RE_DOMINIO.finditer(t):
            host = m.group(1)
            if sufijo_aws(host) or es_publico(host, extra) or dominio_registrable(host) in falsos["domain"]:
                continue
            reportar("dominio sin anonimizar", m)
        for m in RE_ACCESS_KEY.finditer(t):
            if m.group(1) not in falsos["access_key"]:
                reportar("access key", m)
        for patron in (RE_ID_HEX, RE_ID_RDS):
            for m in patron.finditer(t):
                if m.group(0) not in falsos["resource_id"]:
                    reportar("ID de recurso sin anonimizar", m)
        for m in RE_UUID.finditer(t):
            if m.group(1).lower() not in falsos["uuid"]:
                reportar("UUID sin anonimizar", m)
    return problemas


# --------------------------------------------------------------------------- CLI

def resolver(ruta):
    p = Path(ruta)
    if not p.exists() and not p.is_absolute() and (RAIZ / p).exists():
        p = RAIZ / p
    return p.resolve()


def cargar_anon_config(ruta_config):
    try:
        return cargar_config(ruta_config).get("anonymize") or {}
    except SystemExit as e:
        print(f"AVISO: {e} Se sigue sin replace_terms (los nombres de marca NO se reemplazarán).",
              file=sys.stderr)
        return {}


def terminos_de(cfg_anon, extra):
    t = cfg_anon.get("replace_terms") or []
    if isinstance(t, dict):
        t = dict(t)
        t.update({x: "empresa" for x in extra})
        return t
    return list(t) + list(extra)


def con_alias_de_la_corrida(terminos, carpeta):
    """El org_alias de la corrida suele ser la marca (bucket "<alias>-logs"): se agrega a los términos para
    reemplazarlo y verificarlo en texto libre, no solo en el campo org_alias."""
    try:
        alias = (json.loads((Path(carpeta) / "discovery.json").read_text()).get("run") or {}).get("org_alias")
    except (OSError, ValueError, AttributeError):
        return terminos
    if not isinstance(alias, str) or len(alias) < 4 or RE_ORG.fullmatch(alias):
        return terminos
    if isinstance(terminos, dict):
        return terminos if alias.lower() in {k.lower() for k in terminos} else {**terminos, alias: "empresa"}
    return terminos if alias.lower() in {t.lower() for t in terminos} else [*terminos, alias]


def imprimir_problemas(problemas):
    print(f"\nVERIFICACIÓN FALLIDA: {len(problemas)} hallazgo(s). NO presentes esta salida.\n", file=sys.stderr)
    for rel, motivo, frag in problemas[:200]:
        print(f"  {rel}: {motivo}: …{frag}…", file=sys.stderr)
    if len(problemas) > 200:
        print(f"  … y {len(problemas) - 200} más", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Anonimiza una corrida del Método DIVE.")
    ap.add_argument("carpeta", help="salida/<corrida> (o salida/<corrida>-anon con --verificar)")
    ap.add_argument("--config", help="dive.config.yaml (por defecto el de la raíz o DIVE_CONFIG)")
    ap.add_argument("--mapping", help=f"archivo de mapeo a usar/reusar (por defecto <carpeta>/{MAPEO_NOMBRE})")
    ap.add_argument("--termino", action="append", default=[], help="término de marca extra (repetible)")
    ap.add_argument("--costos", choices=["exact", "rounded", "percent"], help="sobrescribe anonymize.costs")
    ap.add_argument("--ocultar-privadas", action="store_true", help="también reemplaza IPs/CIDRs privados")
    ap.add_argument("--verificar", action="store_true", help="solo verifica una carpeta ya anonimizada")
    ap.add_argument("--sin-terminos", action="store_true",
                    help="permite anonimizar sin replace_terms (los nombres de marca NO se reemplazan ni verifican)")
    args = ap.parse_args(argv)
    os.umask(0o077)  # la copia anonimizada y el mapeo solo los lee el usuario

    cfg = cargar_anon_config(args.config)
    terminos = terminos_de(cfg, args.termino)
    publicos = cfg.get("keep_domains") or []
    origen = resolver(args.carpeta)
    terminos = con_alias_de_la_corrida(terminos, origen)
    if not origen.is_dir():
        raise SystemExit(f"No existe la carpeta {origen}")

    if args.verificar:
        if not args.mapping:
            raise SystemExit("--verificar necesita --mapping")
        mapeo = Mapeo(json.loads(resolver(args.mapping).read_text()))
        lista = list(terminos) + list(mapeo.m["term"])
        problemas = verificar(origen, mapeo, lista, publicos, args.ocultar_privadas)
        if problemas:
            imprimir_problemas(problemas)
            return 2
        if not lista:
            print(f"Verificación OK: {origen}, PERO SIN términos de marca: los nombres del cliente NO se verificaron.")
            return 0
        print(f"Verificación OK: {origen}")
        return 0

    if not terminos and not args.sin_terminos:
        print("ERROR: falta anonymize.replace_terms (o --termino). Sin los nombres de marca, buckets, dominios y "
              "ARNs con el nombre del cliente pasan la verificación. Usa --sin-terminos para seguir igual.",
              file=sys.stderr)
        return 2
    if origen.name.endswith("-anon"):
        raise SystemExit("Esa carpeta ya es anónima; pasa la carpeta original salida/<corrida>.")
    ruta_mapeo = resolver(args.mapping) if args.mapping else origen / MAPEO_NOMBRE
    mapeo = Mapeo(json.loads(ruta_mapeo.read_text()) if ruta_mapeo.exists() else None)
    destino = origen.parent / f"{origen.name}-anon"
    if destino.exists():
        shutil.rmtree(destino)
    destino.mkdir(parents=True)

    archivos = sorted(p for p in origen.rglob("*") if p.is_file() and not p.name.endswith(".mapping.json")
                      and p.name != ".DS_Store")
    cuentas, tokens, total, meses = preescanear(archivos)
    costos = args.costos or cfg.get("costs") or "rounded"
    if costos == "percent" and not total:
        print("AVISO: costs=percent sin discovery.json con total; se usa rounded.", file=sys.stderr)
        costos = "rounded"
    an = Anonimizador(mapeo, terminos, costos, args.ocultar_privadas, total, meses, publicos)
    an.registrar_cuentas(cuentas)
    for t in sorted(tokens):
        if an.texto(t, costos=False) == t:  # sin patrón conocido (ej. ID de API Gateway): token
            an.token(t)

    # Pasada 1 aprende todos los valores; pasada 2 escribe, ya con la pasada final compilada, para que un
    # valor aprendido en el último archivo también se reemplace en los primeros.
    omitidos = []
    for escribir in (False, True):
        if escribir:
            an.congelar()
        for ruta in archivos:
            rel = ruta.relative_to(origen)
            rel_anon = Path(an.texto(str(rel), costos=False))
            if ruta.suffix.lower() not in TEXTO:
                if escribir:
                    omitidos.append(rel)
                continue
            contenido = ruta.read_text(errors="replace")
            if ruta.suffix == ".json":
                try:
                    datos = an.json(json.loads(contenido))
                    if costos != "exact" and isinstance(datos, dict):
                        datos["anonymized"] = {"costs": costos}
                    resultado = json.dumps(datos, indent=2, ensure_ascii=False) + "\n"
                except ValueError:
                    resultado = an.texto(contenido)
            else:
                resultado = an.drawio(contenido) if ruta.suffix in (".drawio", ".xml") else an.texto(contenido)
            if escribir:
                salida = destino / rel_anon
                salida.parent.mkdir(parents=True, exist_ok=True)
                salida.write_text(resultado)

    ruta_mapeo.write_text(json.dumps(mapeo.a_dict(), indent=2, ensure_ascii=False) + "\n")
    ruta_mapeo.chmod(0o600)  # el mapeo (con la sal) es la llave para des-anonimizar
    print(f"Anonimizado: {len(archivos) - len(omitidos)} archivo(s) → {destino}")
    print(f"Mapeo: {ruta_mapeo}")
    for rel in omitidos:
        print(f"AVISO: omitido (binario, no se puede anonimizar): {rel}", file=sys.stderr)

    problemas = verificar(destino, mapeo, list(an.terminos), publicos, args.ocultar_privadas)
    if problemas:
        imprimir_problemas(problemas)
        (destino / "VERIFICACION_FALLIDA.txt").write_text(
            "La verificación encontró valores sin anonimizar. No presentes esta carpeta.\n")
        return 2
    if not an.terminos:
        (destino / "SIN_TERMINOS_DE_MARCA.txt").write_text(
            "Anonimizado con --sin-terminos: los nombres de marca del cliente NO se reemplazaron ni verificaron.\n"
            "Revisa a mano buckets, dominios, ARNs y tags antes de presentar.\n")
        print("Verificación OK, PERO SIN términos de marca: los nombres del cliente NO se verificaron "
              "(ver SIN_TERMINOS_DE_MARCA.txt).")
        return 0
    print("Verificación OK: no quedan valores originales, IDs de 12 dígitos reales ni términos de marca.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
