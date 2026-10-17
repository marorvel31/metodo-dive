// Ejecuta el JS de reporte.html con un DOM mínimo simulado para detectar errores en tiempo de
// ejecución sin navegador. Uso: node smoke_reporte.js ruta/reporte.html
"use strict";
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const datos = html.match(/<script type="application\/json" id="datos">([\s\S]*?)<\/script>/)[1];
const codigo = html.match(/<script>\s*([\s\S]*?)<\/script>\s*<\/body>/)[1];

const elementos = {};
function el(id) {
  return {
    id, innerHTML: "", textContent: id === "datos" ? datos : "", value: "", hidden: false, attrs: {},
    listeners: {},
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] ?? null; },
    hasAttribute(k) { return k in this.attrs; }, closest() { return null; },
  };
}
global.document = {
  documentElement: el("html"),
  getElementById: (id) => (elementos[id] = elementos[id] || el(id)),
  querySelectorAll: () => [],
  addEventListener() {},
};
global.window = { matchMedia: () => ({ matches: false }) };
global.localStorage = { getItem() { throw new Error("bloqueado"); }, setItem() { throw new Error("bloqueado"); } };

new Function(codigo)();

const g = (id) => elementos[id];
const filas = (g("r-body").innerHTML.match(/<tr class="res"/g) || []).length;
const hall = (g("h-body").innerHTML.match(/<tr>/g) || []).length;
const errs = (g("e-body").innerHTML.match(/<tr>/g) || []).length;
if (!g("t-resumen").innerHTML.includes("Por cuenta")) throw new Error("resumen vacío");
// filtros: aplicar un servicio y una búsqueda y volver a disparar el render
g("f-svc").value = "ec2";
g("f-svc").listeners.change.forEach((f) => f());
const filasEc2 = (g("r-body").innerHTML.match(/<tr class="res"/g) || []).length;
g("f-q").value = "zzz-no-existe";
g("f-limpiar").listeners.click.forEach((f) => f());
console.log(JSON.stringify({ filas, filasEc2, hallazgos: hall, errores: errs, meta: g("r-meta").textContent }));
