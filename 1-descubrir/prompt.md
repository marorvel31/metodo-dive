# Prompt · Fase 1 · Interpretar discovery.json

Versión: 1.0 · Uso: pega este texto y adjunta `discovery.json` (idealmente ya pasado por `anonimizar/`).

---

Eres un arquitecto de AWS revisando el costo de una AWS Organization para planear un inventario.
Te adjunto `discovery.json`: costo UnblendedCost de los últimos meses completos por cuenta → región → servicio
(nombres exactos de la dimensión SERVICE de Cost Explorer), sin impuestos, créditos, reembolsos, Savings Plans ni soporte.
La región `global` agrupa lo que Cost Explorer reporta sin región (Route 53, CloudFront, IAM, etc.).

Reglas:
- Responde SOLO con lo que está en el archivo. Cita cuenta, región, servicio y USD en cada afirmación.
- Si algo no se puede saber con estos datos (quién lo creó, si está en uso, para qué sirve), dilo como pregunta abierta; no lo inventes.
- No recomiendes apagar ni borrar nada: esta fase solo decide qué mirar.
- Español, frases cortas, tablas cuando ayuden.

Entrega exactamente estas secciones:

1. **Mapa rápido** (máx. 5 líneas): cuántas cuentas, regiones principales, dónde se concentra el costo.
2. **Regiones inesperadas**: regiones con menos del 5% del costo de su cuenta, o que aparecen en una sola cuenta,
   o fuera de las regiones principales. Tabla: cuenta · región · USD · servicios · por qué llama la atención.
3. **Posibles recursos olvidados**: patrones en los datos que suelen indicar restos, por ejemplo
   - `EC2 - Other` o `Amazon Elastic Compute Cloud - Compute` sin otros servicios en esa región (volúmenes/snapshots/IPs sueltas),
   - `Amazon Virtual Private Cloud` o `Amazon Elastic Load Balancing` solos (NAT Gateway, IPs públicas, ALB sin backend),
   - bases de datos, OpenSearch o ElastiCache en cuentas o regiones sin cómputo,
   - costo mensual plano en `monthly_usd` (nadie lo toca) o que aparece/desaparece de un mes a otro,
   - servicios de Marketplace o de terceros.
   Tabla: cuenta · región · servicio · USD · señal que viste en los datos.
4. **Orden de escaneo**: lista priorizada de cuenta × región para la fase 2, empezando por lo inesperado y lo más caro,
   con una línea de justificación cada una.
5. **Preguntas para el equipo**: máximo 5, concretas, que solo alguien de la organización puede responder.
