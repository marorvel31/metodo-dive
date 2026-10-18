"""Fase 5 · Dashboard: sirve en 127.0.0.1 las corridas de una o varias organizaciones, con historia por
fingerprint y el estado que el usuario le da a cada hallazgo (abierto, aceptado, ignorado, resuelto).

Sin dependencias (http.server + sqlite3). Los datos reales nunca salen de la máquina: el servidor solo
escucha en 127.0.0.1, pide un token por arranque y valida el Host (contra DNS rebinding).

El token va en el fragmento de la URL (#t=...), que el navegador nunca envía a ningún servidor; app.js lo
manda solo en el header X-DIVE-Token. Sin cookies: una cookie de 127.0.0.1 viaja a CUALQUIER puerto de
127.0.0.1 (las cookies no distinguen puerto), o sea a otros servicios locales.

Uso:
  python 5-dashboard/dashboard.py                       # output_dir de DIVE_CONFIG, puerto 8765, abre el navegador
  python 5-dashboard/dashboard.py --runs ~/dive-data/org1/runs --runs ~/dive-data/org2/runs --puerto 8765
  python 5-dashboard/dashboard.py --sin-navegador       # no abre el navegador (solo imprime la URL)
  python 5-dashboard/dashboard.py --solo-ingerir        # actualiza la base y sale

Las corridas nuevas del output_dir aparecen al recargar la página: no hace falta reiniciar.
"""

import argparse
import errno
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))
sys.path.insert(0, str(AQUI))

import base  # noqa: E402
from common import cargar_config  # noqa: E402  (también fija el umask 077: la base queda solo para el usuario)

WEB = AQUI / "web"
ESTATICOS = {"/app.js": ("app.js", "text/javascript; charset=utf-8"),
             "/app.css": ("app.css", "text/css; charset=utf-8")}
MAX_CUERPO = 8 * 1024
RE_RUN_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z")  # [0-9] y no \d: \d acepta dígitos Unicode
RE_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
# frame-src 'self': el iframe del reporte no puede navegar fuera (exfiltración por location.href).
CSP_APP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
           "frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# reporte.html usa script y estilos inline: se sirve en un sandbox (origen opaco, sin acceso a la API) y solo
# dentro del dashboard.
CSP_REPORTE = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
               "img-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")
VIDA_TICKET = 30  # segundos de un ticket de un solo uso para abrir un reporte en el iframe


def _igual(a, b):
    return secrets.compare_digest(str(a).encode(), str(b).encode())


def crear_handler(db, carpetas, token, puerto):
    hosts = {f"127.0.0.1:{puerto}", f"localhost:{puerto}"}
    tickets, candado = {}, threading.Lock()  # ticket → (run_id, vence)

    class Handler(BaseHTTPRequestHandler):
        server_version = "dive-dashboard"
        sys_version = ""
        timeout = 10  # una conexión lenta (o a medias) no retiene un hilo para siempre

        def log_message(self, fmt, *args):  # solo método y path: nunca la query (lleva el ticket)
            ruta = urlparse(getattr(self, "path", "") or "").path
            sys.stderr.write(f"{getattr(self, 'command', '?')} {ruta} → {args[1] if len(args) > 1 else ''}\n")

        # ---- respuestas

        def _enviar(self, codigo, cuerpo=b"", tipo="text/plain; charset=utf-8", csp=CSP_APP, extra=None):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY" if csp == CSP_APP else "SAMEORIGIN")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cuerpo)

        def _json(self, datos, codigo=200):
            self._enviar(codigo, json.dumps(datos, ensure_ascii=False, default=str).encode(),
                         "application/json; charset=utf-8")

        def _error(self, codigo, msg):
            self._enviar(codigo, msg.encode())

        # ---- controles

        def _host_ok(self):
            todos = self.headers.get_all("Host") or []
            return len(todos) == 1 and todos[0] in hosts

        def _header_ok(self):
            return _igual(self.headers.get("X-DIVE-Token", ""), token)

        def _nuevo_ticket(self, run_id):
            t = secrets.token_urlsafe(18)
            with candado:
                ahora = time.monotonic()
                for k in [k for k, (_, v) in tickets.items() if v < ahora]:
                    del tickets[k]
                tickets[t] = (run_id, ahora + VIDA_TICKET)
            return t

        def _usar_ticket(self, t, run_id):
            with candado:
                dato = tickets.pop(t, None)
            return bool(dato) and dato[0] == run_id and dato[1] >= time.monotonic()

        # ---- rutas

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "Host no permitido")
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            # La página y sus estáticos no llevan datos ni token: se sirven sin autenticar.
            if url.path == "/":
                return self._enviar(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path in ESTATICOS:
                nombre, tipo = ESTATICOS[url.path]
                return self._enviar(200, (WEB / nombre).read_bytes(), tipo)
            if url.path.startswith("/reporte/"):
                return self._reporte(url.path.removeprefix("/reporte/"), q.get("k", ""))
            if url.path.startswith("/api/"):
                if not self._header_ok():
                    return self._error(HTTPStatus.FORBIDDEN, "Falta X-DIVE-Token")
                if url.path == "/api/ticket":
                    run = q.get("run", "")
                    if not RE_RUN_ID.fullmatch(run):
                        return self._error(HTTPStatus.BAD_REQUEST, "run inválido")
                    return self._json({"ticket": self._nuevo_ticket(run)})
                return self._api_get(url.path, q)
            return self._error(HTTPStatus.NOT_FOUND, "No existe")

        def _reporte(self, run_id, ticket):
            """Un iframe no puede mandar headers: se abre con un ticket de un solo uso y 30 s de vida."""
            if not RE_RUN_ID.fullmatch(run_id):
                return self._error(HTTPStatus.NOT_FOUND, "No existe")
            if not self._usar_ticket(ticket, run_id):
                return self._error(HTTPStatus.FORBIDDEN, "Ticket inválido o vencido")
            con = base.conectar(db)
            try:
                carpeta = base.carpeta_de(con, run_id)
            finally:
                con.close()
            ruta = carpeta / "reporte.html" if carpeta else None
            if not ruta or not ruta.is_file():
                return self._error(HTTPStatus.NOT_FOUND, "Esa corrida no tiene reporte.html")
            return self._enviar(200, ruta.read_bytes(), "text/html; charset=utf-8", csp=CSP_REPORTE)

        def _api_get(self, ruta, q):
            con = base.conectar(db)
            try:
                if ruta == "/api/orgs":
                    base.ingerir(con, carpetas)  # corridas nuevas sin reiniciar (solo lee las que cambiaron)
                    return self._json(base.orgs(con))
                org = q.get("org", "")
                cuenta = q.get("cuenta") or None
                if cuenta and not re.fullmatch(r"[0-9]{12}", cuenta):
                    return self._error(HTTPStatus.BAD_REQUEST, "cuenta inválida")
                if ruta == "/api/corridas":
                    return self._json(base.corridas(con, org, cuenta))
                run = q.get("run", "")
                if ruta == "/api/recursos":
                    return self._json(base.recursos(con, org, run, cuenta))
                if ruta == "/api/resumen":
                    datos = base.resumen(con, org, run, cuenta)
                    return self._json(datos) if datos else self._error(HTTPStatus.NOT_FOUND, "Corrida desconocida")
                if ruta == "/api/hallazgos":
                    return self._json(base.hallazgos(con, org, run))
                return self._error(HTTPStatus.NOT_FOUND, "No existe")
            finally:
                con.close()

        def do_POST(self):
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "Host no permitido")
            if not self._header_ok():
                return self._error(HTTPStatus.FORBIDDEN, "Token inválido")
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Se espera JSON")
            try:
                largo = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                largo = -1
            if largo <= 0 or largo > MAX_CUERPO:
                return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Cuerpo vacío o demasiado grande")
            if urlparse(self.path).path != "/api/estado":
                return self._error(HTTPStatus.NOT_FOUND, "No existe")
            try:
                datos = json.loads(self.rfile.read(largo))
                fp = str(datos.get("fingerprint", ""))
                if not RE_FINGERPRINT.fullmatch(fp):
                    raise ValueError("fingerprint inválido")
                con = base.conectar(db)
                try:
                    return self._json(base.guardar_estado(con, str(datos.get("org", "")), fp,
                                                          datos.get("status"), datos.get("nota", "")))
                finally:
                    con.close()
            except (ValueError, AttributeError, RecursionError) as e:
                return self._error(HTTPStatus.BAD_REQUEST, str(e)[:200])
            except sqlite3.Error:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "No se pudo guardar (¿base bloqueada?)")

    return Handler


def main():
    p = argparse.ArgumentParser(description="Dashboard local del Método DIVE")
    p.add_argument("--config", help="dive.config.yaml (por defecto DIVE_CONFIG): toma su output_dir")
    p.add_argument("--runs", action="append", help="output_dir con corridas (repetible, para varias organizaciones)")
    p.add_argument("--db", help="base SQLite (por defecto <primer output_dir>/dashboard.sqlite)")
    p.add_argument("--puerto", type=int, default=8765, help="8765 por defecto; si está ocupado, uno libre. 0 = uno libre")
    p.add_argument("--sin-navegador", action="store_true", help="no abrir el navegador, solo imprimir la URL")
    p.add_argument("--solo-ingerir", action="store_true")
    a = p.parse_args()

    carpetas = [Path(x).expanduser().resolve() for x in a.runs or []]
    if not carpetas:
        cfg = cargar_config(a.config)
        salida = Path(cfg["output_dir"]).expanduser()
        carpetas = [salida if salida.is_absolute() else (AQUI.parent / salida).resolve()]
    carpetas = [c for c in carpetas if c.is_dir()]
    if not carpetas:
        raise SystemExit("No hay carpetas de corridas: pasa --runs o revisa output_dir.")
    db = Path(a.db).expanduser() if a.db else carpetas[0] / "dashboard.sqlite"

    con = base.conectar(db)
    nuevas, omitidas = base.ingerir(con, carpetas)
    total = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con.close()
    print(f"Base {db}: {total} corridas ({len(nuevas)} nuevas)."
          + (f" {len(omitidas)} carpetas sin bloque run (anteriores al contrato v2) se omiten." if omitidas else ""))
    if a.solo_ingerir:
        return

    token = secrets.token_urlsafe(24)
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", a.puerto), None)
    except OSError as e:
        if e.errno != errno.EADDRINUSE or a.puerto == 0:
            raise
        print(f"El puerto {a.puerto} está ocupado (¿otro dashboard abierto?): uso uno libre.")
        servidor = ThreadingHTTPServer(("127.0.0.1", 0), None)
    puerto = servidor.server_address[1]
    servidor.RequestHandlerClass = crear_handler(db, carpetas, token, puerto)
    url = f"http://127.0.0.1:{puerto}/#t={token}"
    print(f"\n  Dashboard:  {url}\n\n  Solo en esta máquina. El token cambia en cada arranque. Ctrl+C para cerrar.\n", flush=True)
    if not a.sin_navegador:
        webbrowser.open(url)  # el token va en el fragmento: no llega al historial del servidor ni a ningún log
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
