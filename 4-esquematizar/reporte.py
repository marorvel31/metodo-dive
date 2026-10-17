"""Fase 4 · Reporte: inventory.json (+ findings.json, discovery.json) → reporte.html autocontenido.

Un solo archivo HTML con CSS/JS inline y los datos embebidos: funciona offline, sin CDNs.
Pestañas: resumen (cuentas, regiones, recursos, costo del periodo), recursos (filtros por
cuenta/región/servicio/tipo + buscador), hallazgos por severidad y errores de escaneo.

Uso:
  python 4-esquematizar/reporte.py                          # usa salida/<hoy>/inventory.json
  python 4-esquematizar/reporte.py --inventory salida/2026-09-26/inventory.json [--salida carpeta]
"""

import argparse
import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

from common import leer_json  # noqa: E402


def datos_reporte(inventory, findings=None, discovery=None, diagramas=None):
    inventory, findings, discovery = inventory or {}, findings or {}, discovery or {}
    cuentas = {}
    for acc, d in (discovery.get("accounts") or {}).items():
        cuentas[str(acc)] = {"name": (d or {}).get("name") or str(acc), "cost": (d or {}).get("total_usd"),
                             "cost_regions": {k: (v or {}).get("total_usd") for k, v in ((d or {}).get("regions") or {}).items()}}
    for acc, d in (inventory.get("accounts") or {}).items():
        c = cuentas.setdefault(str(acc), {"name": str(acc), "cost": None, "cost_regions": {}})
        c["name"] = (d or {}).get("name") or c["name"]
    recursos = []
    for r in inventory.get("resources") or []:
        if not isinstance(r, dict):
            continue
        acc = str(r.get("account_id"))
        cuentas.setdefault(acc, {"name": acc, "cost": None, "cost_regions": {}})
        recursos.append({k: r.get(k) for k in ("account_id", "region", "service", "type", "id", "arn", "name",
                                                "state", "tags", "attrs", "relations")})
    servicios_costo = {}
    for d in (discovery.get("accounts") or {}).values():
        for reg in ((d or {}).get("regions") or {}).values():
            for s, v in ((reg or {}).get("services") or {}).items():
                servicios_costo[s] = servicios_costo.get(s, 0) + (v or 0)
    return {
        "inventory_generated_at": inventory.get("generated_at"),
        "findings_generated_at": findings.get("generated_at"),
        "period": discovery.get("period"),
        "accounts": cuentas,
        "resources": recursos,
        "findings": [f for f in findings.get("findings") or [] if isinstance(f, dict)],
        "errors": [e for e in inventory.get("errors") or [] if isinstance(e, dict)],
        "scanned": [s for s in inventory.get("scanned") or [] if isinstance(s, dict)],
        "service_costs": dict(sorted(servicios_costo.items(), key=lambda kv: -kv[1])),
        "diagrams": diagramas or [],
        "has_discovery": bool(discovery),
        "has_findings": bool(findings),
    }


def json_embebido(datos):
    # "</" rompería el <script>; U+2028/2029 rompen JS antiguo.
    return (json.dumps(datos, ensure_ascii=False, default=str, separators=(",", ":"))
            .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


PLANTILLA = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inventario AWS</title>
<style>
:root{
  --bg:#f6f7f8;--panel:#ffffff;--text:#1d2a33;--muted:#5b6b76;--border:#dde3e7;--accent:#155f74;
  --accent-soft:#e3eff2;--row-hover:#f1f5f7;--alta:#b42318;--alta-bg:#fdecea;--media:#9a6700;--media-bg:#fff4d6;
  --baja:#1f6f43;--baja-bg:#e6f4ea;--code:#f1f3f5;color-scheme:light;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0f171d;--panel:#16222a;--text:#e3e9ed;--muted:#97a6b0;--border:#2a3a45;--accent:#82cfce;
    --accent-soft:#1d3440;--row-hover:#1c2b34;--alta:#ff8a80;--alta-bg:#3a1c1c;--media:#f5c451;--media-bg:#3a2f14;
    --baja:#7fd8a0;--baja-bg:#16301f;--code:#0f1a20;color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#0f171d;--panel:#16222a;--text:#e3e9ed;--muted:#97a6b0;--border:#2a3a45;--accent:#82cfce;
  --accent-soft:#1d3440;--row-hover:#1c2b34;--alta:#ff8a80;--alta-bg:#3a1c1c;--media:#f5c451;--media-bg:#3a2f14;
  --baja:#7fd8a0;--baja-bg:#16301f;--code:#0f1a20;color-scheme:dark;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--border);padding:16px}
.wrap{max-width:1280px;margin:0 auto}
.top{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
h1{font-size:20px;margin:0}
.sub{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
button,select,input{font:inherit;color:inherit}
.btn{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:6px 12px;cursor:pointer;min-height:36px}
.btn:hover{border-color:var(--accent)}
nav{display:flex;gap:4px;margin-top:12px;overflow-x:auto}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;cursor:pointer;color:var(--muted);white-space:nowrap;min-height:40px}
nav button[aria-selected="true"]{color:var(--text);border-bottom-color:var(--accent);font-weight:600}
.badge{display:inline-block;min-width:20px;padding:0 6px;border-radius:10px;background:var(--accent-soft);color:var(--text);font-size:12px;text-align:center;margin-left:4px}
main{padding:16px}
section[hidden]{display:none}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px}
.card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.card .n{color:var(--muted);font-size:12px}
h2{font-size:15px;margin:24px 0 8px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap;background:var(--panel);position:sticky;top:0}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--text)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover{background:var(--row-hover)}
tr.res{cursor:pointer}
td.id{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
.filters{display:flex;flex-wrap:wrap;gap:8px;padding:12px;border-bottom:1px solid var(--border)}
.filters select,.filters input{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 8px;min-height:36px;max-width:100%}
.filters input[type=search]{flex:1 1 220px}
.meta{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;color:var(--muted);font-size:13px}
.pager{display:flex;gap:6px;align-items:center}
.sev{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap}
.sev-alta{color:var(--alta);background:var(--alta-bg)}.sev-media{color:var(--media);background:var(--media-bg)}.sev-baja{color:var(--baja);background:var(--baja-bg)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:4px 12px;cursor:pointer;min-height:32px}
.chip[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-soft);font-weight:600}
.detail td{background:var(--bg)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.kv h4{margin:0 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
pre{margin:0;background:var(--code);border:1px solid var(--border);border-radius:6px;padding:8px;font-size:12px;white-space:pre-wrap;word-break:break-all}
a,.link{color:var(--accent);cursor:pointer;text-decoration:underline;background:none;border:0;padding:0;font:inherit;text-align:left;overflow-wrap:anywhere}
.empty{padding:24px;text-align:center;color:var(--muted)}
.state-off{color:var(--muted)}
.hint{color:var(--muted);font-size:12px;margin:6px 0 0}
footer{color:var(--muted);font-size:12px;padding:16px;text-align:center}
@media (max-width:600px){
  main,header{padding:12px}
  .card .v{font-size:20px}
  th,td{padding:6px 8px}
  .hide-sm{display:none}
}
@media (prefers-reduced-motion:no-preference){.btn,.chip{transition:border-color .15s}}
</style>
</head>
<body>
<header><div class="wrap">
  <div class="top">
    <div><h1>Inventario AWS</h1><div class="sub" id="subtitulo"></div></div>
    <button class="btn" id="tema" type="button" aria-label="Cambiar tema claro/oscuro">Tema</button>
  </div>
  <nav role="tablist">
    <button role="tab" data-tab="resumen" aria-selected="true">Resumen</button>
    <button role="tab" data-tab="recursos" aria-selected="false">Recursos<span class="badge" id="b-recursos"></span></button>
    <button role="tab" data-tab="hallazgos" aria-selected="false">Hallazgos<span class="badge" id="b-hallazgos"></span></button>
    <button role="tab" data-tab="errores" aria-selected="false">Errores de escaneo<span class="badge" id="b-errores"></span></button>
  </nav>
</div></header>
<main class="wrap">
  <section id="t-resumen"></section>
  <section id="t-recursos" hidden>
    <div class="panel">
      <div class="filters">
        <select id="f-acc" aria-label="Cuenta"></select>
        <select id="f-reg" aria-label="Región"></select>
        <select id="f-svc" aria-label="Servicio"></select>
        <select id="f-typ" aria-label="Tipo"></select>
        <input type="search" id="f-q" placeholder="Buscar por id, nombre, ARN, tag o atributo" aria-label="Buscar">
        <button class="btn" id="f-limpiar" type="button">Limpiar</button>
      </div>
      <div class="meta"><span id="r-meta"></span><span class="pager" id="r-pager"></span></div>
      <div class="scroll"><table>
        <thead><tr>
          <th class="sortable" data-k="account">Cuenta</th><th class="sortable" data-k="region">Región</th>
          <th class="sortable" data-k="service">Servicio</th><th class="sortable" data-k="type">Tipo</th>
          <th class="sortable" data-k="name">Nombre</th><th class="sortable hide-sm" data-k="id">ID</th>
          <th class="sortable" data-k="state">Estado</th><th class="sortable num" data-k="nf">Hallazgos</th>
        </tr></thead>
        <tbody id="r-body"></tbody>
      </table></div>
    </div>
    <p class="hint">Toca una fila para ver tags, atributos y relaciones. Los IDs en relaciones son enlaces al recurso.</p>
  </section>
  <section id="t-hallazgos" hidden>
    <div class="panel">
      <div class="filters">
        <div class="chips" id="h-sev"></div>
        <select id="h-acc" aria-label="Cuenta"></select>
        <select id="h-cat" aria-label="Categoría"></select>
        <input type="search" id="h-q" placeholder="Buscar en hallazgos" aria-label="Buscar hallazgos">
      </div>
      <div class="meta"><span id="h-meta"></span></div>
      <div class="scroll"><table>
        <thead><tr><th>Severidad</th><th>Categoría</th><th>Cuenta</th><th class="hide-sm">Región</th><th>Recurso</th><th>Hallazgo</th><th class="num">USD/mes</th><th class="hide-sm">Fuente</th></tr></thead>
        <tbody id="h-body"></tbody>
      </table></div>
    </div>
  </section>
  <section id="t-errores" hidden>
    <div class="panel"><div class="scroll"><table>
      <thead><tr><th>Cuenta</th><th>Región</th><th>Colector</th><th>Error</th></tr></thead>
      <tbody id="e-body"></tbody>
    </table></div></div>
    <p class="hint">Un error no detiene la corrida: lo que falló en una cuenta/región falta en el inventario, el resto sí está.</p>
  </section>
</main>
<footer>Método DIVE · fase 4 · generado a partir de inventory.json, findings.json y discovery.json</footer>
<script type="application/json" id="datos">__DATOS__</script>
<script>
(function(){
"use strict";
var D = JSON.parse(document.getElementById("datos").textContent);
var R = D.resources || [], F = D.findings || [], E = D.errors || [], A = D.accounts || {};
var POR_PAGINA = 100;
var SEV_ORDEN = {alta:0, high:0, critical:0, media:1, medium:1, baja:2, low:2};

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]; }); }
function usd(v){ return (v == null || isNaN(v)) ? "—" : "$" + Number(v).toLocaleString("en-US", {minimumFractionDigits:2, maximumFractionDigits:2}); }
function nombreCuenta(id){ var a = A[id]; return a && a.name ? a.name : String(id); }
function sevNorm(s){ s = String(s || "").toLowerCase(); return s === "high" || s === "critical" ? "alta" : s === "medium" ? "media" : s === "low" ? "baja" : s; }
function unicos(arr){ var m = {}; arr.forEach(function(x){ if (x != null && x !== "") m[x] = 1; }); return Object.keys(m).sort(); }
function contar(arr, f){ var m = {}; arr.forEach(function(x){ var k = f(x); m[k] = (m[k] || 0) + 1; }); return m; }
function guardar(k, v){ try { localStorage.setItem(k, v); } catch (e) {} }
function leer(k){ try { return localStorage.getItem(k); } catch (e) { return null; } }

// --- tema
var raiz = document.documentElement;
var temaGuardado = leer("dive-tema");
if (temaGuardado === "light" || temaGuardado === "dark") raiz.setAttribute("data-theme", temaGuardado);
$("tema").addEventListener("click", function(){
  var oscuroSistema = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var actual = raiz.getAttribute("data-theme") || (oscuroSistema ? "dark" : "light");
  var nuevo = actual === "dark" ? "light" : "dark";
  raiz.setAttribute("data-theme", nuevo); guardar("dive-tema", nuevo);
});

// --- hallazgos por recurso
var hallazgosPor = {};
F.forEach(function(f){ if (f.resource_id) (hallazgosPor[f.resource_id] = hallazgosPor[f.resource_id] || []).push(f); });
R.forEach(function(r){
  var fs = (hallazgosPor[r.id] || []).concat(r.arn && r.arn !== r.id ? (hallazgosPor[r.arn] || []) : []);
  r._nf = fs.length;
  r._alta = fs.some(function(f){ return sevNorm(f.severity) === "alta"; });
  r._q = [r.id, r.name, r.arn, r.account_id, nombreCuenta(r.account_id), r.region, r.service, r.type, r.state,
          JSON.stringify(r.tags || {}), JSON.stringify(r.attrs || {}), JSON.stringify(r.relations || {})].join(" ").toLowerCase();
});

// --- subtítulo y badges
var regiones = unicos(R.map(function(r){ return r.region; }));
$("subtitulo").textContent = "Inventario " + (D.inventory_generated_at || "—") +
  (D.period ? " · costos " + D.period.start + " → " + D.period.end : "");
$("b-recursos").textContent = R.length;
$("b-hallazgos").textContent = F.length;
$("b-errores").textContent = E.length;

// --- pestañas
var tabs = document.querySelectorAll("nav button[data-tab]");
function abrir(tab){
  Array.prototype.forEach.call(tabs, function(b){ b.setAttribute("aria-selected", String(b.getAttribute("data-tab") === tab)); });
  ["resumen","recursos","hallazgos","errores"].forEach(function(t){ $("t-" + t).hidden = t !== tab; });
  guardar("dive-tab", tab);
}
Array.prototype.forEach.call(tabs, function(b){ b.addEventListener("click", function(){ abrir(b.getAttribute("data-tab")); }); });

// --- resumen
function resumen(){
  var cuentas = Object.keys(A).sort(function(a, b){ return (A[b].cost || 0) - (A[a].cost || 0) || nombreCuenta(a).localeCompare(nombreCuenta(b)); });
  var costo = 0, hayCosto = false;
  cuentas.forEach(function(a){ if (A[a].cost != null) { costo += A[a].cost; hayCosto = true; } });
  var sev = contar(F, function(f){ return sevNorm(f.severity); });
  var meses = D.period && D.period.months ? D.period.months : null;
  var h = '<div class="cards">' +
    card("Cuentas", cuentas.length, "") + card("Regiones", regiones.length, regiones.slice(0, 4).join(", ") + (regiones.length > 4 ? "…" : "")) +
    card("Recursos", R.length.toLocaleString("en-US"), unicos(R.map(function(r){ return r.service; })).length + " servicios") +
    card("Costo" + (meses ? " " + meses + " meses" : ""), hayCosto ? usd(costo) : "—", D.period ? esc(D.period.start + " → " + D.period.end) : "sin discovery.json") +
    card("Hallazgos", F.length, (sev.alta || 0) + " alta · " + (sev.media || 0) + " media · " + (sev.baja || 0) + " baja") +
    card("Errores de escaneo", E.length, E.length ? "revisar pestaña Errores" : "corrida completa") + "</div>";

  var porCuenta = contar(R, function(r){ return r.account_id; });
  var regCuenta = {};
  R.forEach(function(r){ (regCuenta[r.account_id] = regCuenta[r.account_id] || {})[r.region] = 1; });
  var errCuenta = contar(E, function(e){ return e.account_id; });
  var sevCuenta = {};
  F.forEach(function(f){ var m = sevCuenta[f.account_id] = sevCuenta[f.account_id] || {}; var s = sevNorm(f.severity); m[s] = (m[s] || 0) + 1; });
  h += '<h2>Por cuenta</h2><div class="panel scroll"><table><thead><tr><th>Cuenta</th><th class="hide-sm">ID</th><th class="num">Recursos</th><th class="num">Regiones</th><th class="num">Costo periodo</th><th>Hallazgos</th><th class="num">Errores</th></tr></thead><tbody>';
  cuentas.forEach(function(a){
    var s = sevCuenta[a] || {};
    var chips = ["alta","media","baja"].filter(function(k){ return s[k]; }).map(function(k){ return '<span class="sev sev-' + k + '">' + s[k] + " " + k + "</span>"; }).join(" ");
    h += '<tr><td><button class="link" data-ir-cuenta="' + esc(a) + '">' + esc(nombreCuenta(a)) + '</button></td><td class="id hide-sm">' + esc(a) + '</td><td class="num">' + (porCuenta[a] || 0) +
      '</td><td class="num">' + Object.keys(regCuenta[a] || {}).length + '</td><td class="num">' + usd(A[a].cost) + "</td><td>" + (chips || "—") +
      '</td><td class="num">' + (errCuenta[a] || 0) + "</td></tr>";
  });
  h += "</tbody></table></div>";

  var porSvc = contar(R, function(r){ return r.service + " / " + r.type; });
  var claves = Object.keys(porSvc).sort(function(a, b){ return porSvc[b] - porSvc[a] || a.localeCompare(b); });
  h += '<h2>Recursos por servicio y tipo</h2><div class="panel scroll"><table><thead><tr><th>Servicio / tipo</th><th class="num">Recursos</th></tr></thead><tbody>';
  claves.forEach(function(k){ h += "<tr><td>" + esc(k) + '</td><td class="num">' + porSvc[k] + "</td></tr>"; });
  if (!claves.length) h += '<tr><td colspan="2" class="empty">Sin recursos</td></tr>';
  h += "</tbody></table></div>";

  var sc = D.service_costs || {}, sk = Object.keys(sc);
  if (sk.length) {
    h += '<h2>Costo por servicio (Cost Explorer, todo el periodo)</h2><div class="panel scroll"><table><thead><tr><th>Servicio</th><th class="num">USD</th><th class="num">%</th></tr></thead><tbody>';
    var tot = sk.reduce(function(s, k){ return s + (sc[k] || 0); }, 0) || 1;
    sk.forEach(function(k){ h += "<tr><td>" + esc(k) + '</td><td class="num">' + usd(sc[k]) + '</td><td class="num">' + (100 * sc[k] / tot).toFixed(1) + "%</td></tr>"; });
    h += "</tbody></table></div>";
  }
  if ((D.diagrams || []).length) {
    h += '<h2>Diagramas (abrir con draw.io)</h2><div class="panel" style="padding:12px"><ul style="margin:0;padding-left:20px">';
    D.diagrams.forEach(function(d){ h += '<li><a href="' + esc(d) + '">' + esc(d) + "</a></li>"; });
    h += "</ul></div>";
  }
  $("t-resumen").innerHTML = h;
}
function card(k, v, n){ return '<div class="card"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v) + '</div><div class="n">' + esc(n) + "</div></div>"; }
document.addEventListener("click", function(ev){
  var t = ev.target.closest ? ev.target.closest("[data-ir-cuenta],[data-ir-id]") : null;
  if (!t) return;
  if (t.hasAttribute("data-ir-cuenta")) { limpiar(); $("f-acc").value = t.getAttribute("data-ir-cuenta"); }
  else { limpiar(); $("f-q").value = t.getAttribute("data-ir-id"); }
  estado.pagina = 0; recursos(); abrir("recursos"); ev.stopPropagation();
});

// --- recursos
var estado = {orden: "account", dir: 1, pagina: 0, abierto: null};
function opciones(sel, etiqueta, valores, fmt){
  sel.innerHTML = '<option value="">' + esc(etiqueta) + "</option>" + valores.map(function(v){ return '<option value="' + esc(v) + '">' + esc(fmt ? fmt(v) : v) + "</option>"; }).join("");
}
opciones($("f-acc"), "Todas las cuentas", Object.keys(A).sort(function(a, b){ return nombreCuenta(a).localeCompare(nombreCuenta(b)); }), function(a){ return nombreCuenta(a) + " (" + a + ")"; });
opciones($("f-reg"), "Todas las regiones", regiones);
opciones($("f-svc"), "Todos los servicios", unicos(R.map(function(r){ return r.service; })));
function tiposDisponibles(){
  var s = $("f-svc").value, actual = $("f-typ").value;
  opciones($("f-typ"), "Todos los tipos", unicos(R.filter(function(r){ return !s || r.service === s; }).map(function(r){ return r.type; })));
  $("f-typ").value = actual; if ($("f-typ").value !== actual) $("f-typ").value = "";
}
tiposDisponibles();
function valorOrden(r, k){
  if (k === "account") return nombreCuenta(r.account_id).toLowerCase();
  if (k === "nf") return r._nf;
  return String(r[k] == null ? "" : r[k]).toLowerCase();
}
function filtrados(){
  var fa = $("f-acc").value, fr = $("f-reg").value, fs = $("f-svc").value, ft = $("f-typ").value;
  var q = $("f-q").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  var out = R.filter(function(r){
    return (!fa || String(r.account_id) === fa) && (!fr || r.region === fr) && (!fs || r.service === fs) && (!ft || r.type === ft) &&
      q.every(function(t){ return r._q.indexOf(t) >= 0; });
  });
  var k = estado.orden, d = estado.dir;
  out.sort(function(a, b){ var x = valorOrden(a, k), y = valorOrden(b, k); return (x < y ? -1 : x > y ? 1 : 0) * d || String(a.id).localeCompare(String(b.id)); });
  return out;
}
function enlazarIds(v){
  if (Array.isArray(v)) return v.map(enlazarIds).join(", ");
  return '<button class="link" data-ir-id="' + esc(v) + '">' + esc(v) + "</button>";
}
function detalleHtml(r){
  var rel = r.relations || {}, relH = Object.keys(rel).map(function(k){ return "<div><b>" + esc(k) + ":</b> " + enlazarIds(rel[k]) + "</div>"; }).join("") || "—";
  var fs = (hallazgosPor[r.id] || []).concat(r.arn && r.arn !== r.id ? (hallazgosPor[r.arn] || []) : []);
  var fH = fs.map(function(f){ return '<div><span class="sev sev-' + esc(sevNorm(f.severity)) + '">' + esc(sevNorm(f.severity)) + "</span> " + esc(f.title) + "</div>"; }).join("");
  return '<div class="kv">' +
    "<div><h4>Identidad</h4><pre>" + esc("id: " + r.id + "\narn: " + (r.arn || "—") + "\ncuenta: " + nombreCuenta(r.account_id) + " (" + r.account_id + ")") + "</pre></div>" +
    "<div><h4>Relaciones</h4>" + relH + (fH ? "<h4 style=\"margin-top:10px\">Hallazgos</h4>" + fH : "") + "</div>" +
    "<div><h4>Atributos</h4><pre>" + esc(JSON.stringify(r.attrs || {}, null, 2)) + "</pre></div>" +
    "<div><h4>Tags</h4><pre>" + esc(JSON.stringify(r.tags || {}, null, 2)) + "</pre></div></div>";
}
function recursos(){
  var lista = filtrados(), total = lista.length, paginas = Math.max(1, Math.ceil(total / POR_PAGINA));
  if (estado.pagina >= paginas) estado.pagina = paginas - 1;
  var ini = estado.pagina * POR_PAGINA, fin = Math.min(total, ini + POR_PAGINA);
  var h = "";
  lista.slice(ini, fin).forEach(function(r, i){
    var clave = r.account_id + "|" + r.region + "|" + r.id;
    var off = /^(stopped|stopping|terminated|deleted)$/i.test(r.state || "");
    h += '<tr class="res" data-clave="' + esc(clave) + '" tabindex="0"><td>' + esc(nombreCuenta(r.account_id)) + "</td><td>" + esc(r.region) + "</td><td>" + esc(r.service) +
      "</td><td>" + esc(r.type) + "</td><td>" + esc(r.name || "—") + '</td><td class="id hide-sm">' + esc(r.id) + '</td><td class="' + (off ? "state-off" : "") + '">' + esc(r.state || "—") +
      '</td><td class="num">' + (r._nf ? '<span class="sev sev-' + (r._alta ? "alta" : "media") + '">' + r._nf + "</span>" : "") + "</td></tr>";
    if (estado.abierto === clave) h += '<tr class="detail"><td colspan="8">' + detalleHtml(r) + "</td></tr>";
  });
  $("r-body").innerHTML = h || '<tr><td colspan="8" class="empty">Ningún recurso coincide con los filtros</td></tr>';
  $("r-meta").textContent = total ? "Mostrando " + (ini + 1) + "–" + fin + " de " + total.toLocaleString("en-US") + " recursos" : "0 recursos";
  $("r-pager").innerHTML = paginas > 1 ? '<button class="btn" data-pag="-1" type="button"' + (estado.pagina ? "" : " disabled") + '>Anterior</button><span>' + (estado.pagina + 1) + " / " + paginas +
    '</span><button class="btn" data-pag="1" type="button"' + (estado.pagina < paginas - 1 ? "" : " disabled") + ">Siguiente</button>" : "";
  var ths = document.querySelectorAll("#t-recursos th.sortable");
  Array.prototype.forEach.call(ths, function(th){
    var k = th.getAttribute("data-k");
    th.setAttribute("aria-sort", k === estado.orden ? (estado.dir > 0 ? "ascending" : "descending") : "none");
    th.textContent = th.textContent.replace(/ [▲▼]$/, "") + (k === estado.orden ? (estado.dir > 0 ? " ▲" : " ▼") : "");
  });
}
function limpiar(){ ["f-acc","f-reg","f-svc","f-typ","f-q"].forEach(function(id){ $(id).value = ""; }); tiposDisponibles(); }
["f-acc","f-reg","f-typ"].forEach(function(id){ $(id).addEventListener("change", function(){ estado.pagina = 0; recursos(); }); });
$("f-svc").addEventListener("change", function(){ tiposDisponibles(); estado.pagina = 0; recursos(); });
var tBusqueda = null;
$("f-q").addEventListener("input", function(){ clearTimeout(tBusqueda); tBusqueda = setTimeout(function(){ estado.pagina = 0; recursos(); }, 150); });
$("f-limpiar").addEventListener("click", function(){ limpiar(); estado.pagina = 0; recursos(); });
$("r-pager").addEventListener("click", function(ev){ var b = ev.target.closest ? ev.target.closest("[data-pag]") : null; if (b) { estado.pagina += Number(b.getAttribute("data-pag")); recursos(); } });
Array.prototype.forEach.call(document.querySelectorAll("#t-recursos th.sortable"), function(th){
  th.addEventListener("click", function(){ var k = th.getAttribute("data-k"); if (estado.orden === k) estado.dir *= -1; else { estado.orden = k; estado.dir = 1; } recursos(); });
});
function alternar(tr){ var c = tr.getAttribute("data-clave"); estado.abierto = estado.abierto === c ? null : c; recursos(); }
$("r-body").addEventListener("click", function(ev){ var tr = ev.target.closest ? ev.target.closest("tr.res") : null; if (tr) alternar(tr); });
$("r-body").addEventListener("keydown", function(ev){ if (ev.key !== "Enter" && ev.key !== " ") return; var tr = ev.target.closest ? ev.target.closest("tr.res") : null; if (tr) { ev.preventDefault(); alternar(tr); } });

// --- hallazgos
var sevActiva = "";
function chipsSev(){
  var c = contar(F, function(f){ return sevNorm(f.severity); });
  var h = '<button class="chip" type="button" data-sev="" aria-pressed="' + (sevActiva === "") + '">Todas (' + F.length + ")</button>";
  ["alta","media","baja"].forEach(function(s){ h += '<button class="chip" type="button" data-sev="' + s + '" aria-pressed="' + (sevActiva === s) + '">' + s + " (" + (c[s] || 0) + ")</button>"; });
  $("h-sev").innerHTML = h;
}
opciones($("h-acc"), "Todas las cuentas", unicos(F.map(function(f){ return f.account_id; })), function(a){ return nombreCuenta(a); });
opciones($("h-cat"), "Todas las categorías", unicos(F.map(function(f){ return f.category; })));
function hallazgos(){
  chipsSev();
  var fa = $("h-acc").value, fc = $("h-cat").value, q = $("h-q").value.trim().toLowerCase();
  var lista = F.filter(function(f){
    return (!sevActiva || sevNorm(f.severity) === sevActiva) && (!fa || String(f.account_id) === fa) && (!fc || f.category === fc) &&
      (!q || [f.id, f.title, f.detail, f.resource_id, f.account_id, nombreCuenta(f.account_id), f.region, f.source].join(" ").toLowerCase().indexOf(q) >= 0);
  }).sort(function(a, b){
    return ((SEV_ORDEN[String(a.severity).toLowerCase()] == null ? 9 : SEV_ORDEN[String(a.severity).toLowerCase()]) -
            (SEV_ORDEN[String(b.severity).toLowerCase()] == null ? 9 : SEV_ORDEN[String(b.severity).toLowerCase()])) ||
      ((b.monthly_usd || 0) - (a.monthly_usd || 0)) || String(a.id).localeCompare(String(b.id));
  });
  var mensual = lista.reduce(function(s, f){ return s + (Number(f.monthly_usd) || 0); }, 0);
  $("h-meta").textContent = lista.length + " hallazgos" + (mensual ? " · " + usd(mensual) + "/mes en juego" : "");
  $("h-body").innerHTML = lista.map(function(f){
    var s = sevNorm(f.severity);
    return '<tr><td><span class="sev sev-' + esc(s) + '">' + esc(s) + "</span></td><td>" + esc(f.category) + "</td><td>" + esc(nombreCuenta(f.account_id)) +
      '</td><td class="hide-sm">' + esc(f.region || "—") + '</td><td class="id">' + (f.resource_id ? enlazarIds(f.resource_id) : "—") + "</td><td><b>" + esc(f.title) + "</b>" +
      (f.detail ? '<div class="sub">' + esc(f.detail) + "</div>" : "") + '</td><td class="num">' + (f.monthly_usd != null ? usd(f.monthly_usd) : "—") + '</td><td class="hide-sm">' + esc(f.source || "") + "</td></tr>";
  }).join("") || '<tr><td colspan="8" class="empty">' + (D.has_findings ? "Ningún hallazgo coincide" : "No hay findings.json para esta corrida") + "</td></tr>";
}
$("h-sev").addEventListener("click", function(ev){ var b = ev.target.closest ? ev.target.closest("[data-sev]") : null; if (b) { sevActiva = b.getAttribute("data-sev"); hallazgos(); } });
["h-acc","h-cat"].forEach(function(id){ $(id).addEventListener("change", hallazgos); });
$("h-q").addEventListener("input", hallazgos);

// --- errores
$("e-body").innerHTML = E.slice().sort(function(a, b){ return nombreCuenta(a.account_id).localeCompare(nombreCuenta(b.account_id)) || String(a.region).localeCompare(String(b.region)); }).map(function(e){
  return "<tr><td>" + esc(nombreCuenta(e.account_id)) + '<div class="sub">' + esc(e.account_id) + "</div></td><td>" + esc(e.region || "—") + "</td><td>" + esc(e.collector || "—") + '</td><td class="id">' + esc(e.error) + "</td></tr>";
}).join("") || '<tr><td colspan="4" class="empty">Sin errores: todos los colectores terminaron bien</td></tr>';

resumen(); recursos(); hallazgos();
var tabGuardada = leer("dive-tab");
if (tabGuardada && $("t-" + tabGuardada)) abrir(tabGuardada);
})();
</script>
</body>
</html>
"""


def generar(inventory, findings=None, discovery=None, ruta=None, diagramas=None):
    datos = datos_reporte(inventory, findings, discovery, diagramas)
    contenido = PLANTILLA.replace("__DATOS__", json_embebido(datos))
    Path(ruta).write_text(contenido, encoding="utf-8")
    return ruta


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config")
    p.add_argument("--inventory", help="Ruta a inventory.json (por defecto salida/<hoy>/inventory.json)")
    p.add_argument("--findings", help="Ruta a findings.json (por defecto, junto a inventory.json)")
    p.add_argument("--discovery", help="Ruta a discovery.json (por defecto, junto a inventory.json)")
    p.add_argument("--salida", help="Carpeta donde escribir reporte.html (por defecto, la de inventory.json)")
    a = p.parse_args()

    if a.inventory:
        inv_path = Path(a.inventory)
    else:
        from common import cargar_config, carpeta_salida
        inv_path = carpeta_salida(cargar_config(a.config)) / "inventory.json"
    if not inv_path.exists():
        raise SystemExit(f"No existe {inv_path}. Corre primero la fase 2 o pasa --inventory.")
    base = Path(a.salida) if a.salida else inv_path.parent
    base.mkdir(parents=True, exist_ok=True)

    def opcional(ruta):
        return leer_json(ruta) if ruta and Path(ruta).exists() else None

    diagramas = sorted(f"diagramas/{x.name}" for x in (base / "diagramas").glob("*.drawio")) if (base / "diagramas").is_dir() else []
    ruta = generar(leer_json(inv_path), opcional(a.findings or inv_path.parent / "findings.json"),
                   opcional(a.discovery or inv_path.parent / "discovery.json"), base / "reporte.html", diagramas)
    print(ruta)


if __name__ == "__main__":
    main()
