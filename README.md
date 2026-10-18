# Método DIVE

Inventario de una AWS Organization con muchas cuentas, en una tarde, con IA como copiloto y sin darle a nadie (ni a la IA) permisos de escritura.

| Fase | Pregunta | Fuente de verdad | Entregable |
|---|---|---|---|
| **0 · Preparar** | ¿Cómo entro a todas las cuentas sin riesgo? | IAM Identity Center | permission set `DIVE_ReadOnly` + perfiles `dive-*` |
| **D · Descubrir** | ¿Dónde hay algo? | Billing (Cost Explorer, 6 meses) | `discovery.json` |
| **I · Inventariar** | ¿Qué hay exactamente? | APIs de AWS, solo donde el billing dice que hay costo | `inventory.json` |
| **V · Validar** | ¿Está sano? ¿Cuadra con lo que pago? | AWS Doctor + cruce billing ↔ inventario + reglas | `findings.json` / `findings.md` |
| **E · Esquematizar** | ¿Cómo se ve? | `inventory.json` | diagramas `.drawio` + `reporte.html` |
| Anonimizar | ¿Lo puedo mostrar? | todas las salidas | carpeta `-anon` verificada |

## Principios

1. **La factura es el primer mapa.** En vez de escanear 30+ servicios × todas las regiones × todas las cuentas, solo se escanea donde hubo uso facturado. Menos llamadas, menos throttling, ninguna llamada de más a producción.
2. **Contratos JSON entre fases.** Cada fase lee un archivo y escribe otro (ver [CONTRATO.md](CONTRATO.md)). Nada se parsea desde texto de consola, y cualquier fase se puede repetir sola.
3. **La IA escribe código y analiza, nunca inventa datos.** Todo lo que está en los JSON viene de AWS. Los prompts de cada fase (`*/prompt.md`) están versionados y exigen citar IDs.
4. **El agente usa tu identidad, no tus permisos.** Aunque seas admin, el escaneo corre con un rol de solo lectura de metadatos: puede listar un secreto, nunca leer su valor.
5. **Sin secretos en la salida.** Los colectores descartan variables de entorno, UserData, task definitions, PSKs, policies y endpoints de suscripción.
6. **Checkpoint humano entre fases.** Se revisa cada entregable antes de seguir.

## Requisitos

- Python 3.11+ (el anonimizador usa cuantificadores posesivos de `re`) y [uv](https://github.com/astral-sh/uv) (o pip)
- AWS CLI v2 con acceso a la cuenta management
- [aws-doctor](https://github.com/elC0mpa/aws-doctor) (opcional, fase V)

```bash
uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock   # versiones exactas con hash
# (sin uv: python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock)
cp dive.config.example.yaml dive.config.yaml   # y ajusta management_profile
```

## Fase 0 · Preparar el acceso

El rol de escaneo es `ViewOnlyAccess` + [una policy mínima](setup/readonly-policy.json) con exactamente lo que piden los colectores y aws-doctor. `ViewOnlyAccess` sola se queda corta (no trae Cost Explorer ni Transit Gateway), y `ReadOnlyAccess` es demasiado (incluye `s3:GetObject`: leer datos).

La policy también **niega explícitamente** lo que sí lee contenido aunque parezca metadato: valores de API keys de API Gateway, variables de entorno de task definitions de ECS y de Lambda (`GetFunctionConfiguration`), UserData (launch templates, launch configurations, spot), secretos, parámetros SSM, objetos S3, eventos de logs y outputs de CloudFormation. Esos `Deny` siguen valiendo aunque AWS amplíe `ViewOnlyAccess` en el futuro. Riesgo residual que no se puede cerrar sin romper el inventario: `lambda:ListFunctions`/`ListVersionsByFunction` devuelven las variables de entorno de Lambda, `glue:GetJobs` los `DefaultArguments` de los jobs y los stages de API Gateway sus stage variables. Los colectores descartan esos campos, pero quien tenga el rol puede verlos: no guardes secretos ahí.

Opcional: el chequeo de memoria de Lambda de aws-doctor usa Logs Insights (`logs:StartQuery`, `logs:GetQueryResults`), que lee el **contenido** de los logs de las funciones. No viene en la policy; agrégalo solo si aceptas ese acceso.

Elige según lo que tenga tu organización:

| Tu org tiene… | Qué hacer |
|---|---|
| **IAM Identity Center (SSO)** | `bash setup/crear_permission_set.sh --profile <management> --user <tu-usuario>` (dry-run), luego con `--apply`. Después `python setup/generar_perfiles.py`. Es re-ejecutable: si cambias la policy, vuelve a correrlo. Reversa: `setup/eliminar_permission_set.sh`. |
| **Organizations sin SSO** | Despliega un rol `DIVE_ReadOnly` con un StackSet en todas las cuentas (misma policy) y usa `access_mode: assume_role`. |
| **Solo `OrganizationAccountAccessRole` / `AWSControlTowerExecution`** | Esos roles son **admin**: úsalos para *desplegar* el rol de lectura, nunca para escanear. |

Comprobación de que el rol no puede escribir (no modifica nada, `--dry-run` solo evalúa el permiso):

```bash
AWS_CONFIG_FILE=~/.aws/dive-config aws ec2 create-tags --dry-run --resources <vpc-id> \
  --tags Key=dive,Value=test --profile dive-<account_id>
# → UnauthorizedOperation
```

## Correr las fases

```bash
export DIVE_CONFIG=dive.config.yaml            # o la ruta a la config de cada organización
.venv/bin/python 1-descubrir/descubrir.py       # ~10 s
.venv/bin/python 2-inventariar/inventariar.py   # ~2 min para ~20 cuentas
.venv/bin/python 3-validar/validar.py           # ~20 s (usa --sin-doctor si no tienes aws-doctor)
.venv/bin/python 4-esquematizar/esquematizar.py
.venv/bin/python 4-esquematizar/reporte.py
.venv/bin/python anonimizar/anonimizar.py salida/<corrida>   # sale ≠ 0 si detecta cualquier fuga; exige anonymize.replace_terms
```

Las salidas quedan en `salida/<corrida>/` (ignorada por git), una carpeta por corrida (`2026-10-18T101500Z`, UTC). La fase 1 la crea y la anota en `salida/ULTIMA`; las demás fases usan esa. Si trabajas para un cliente, apunta `output_dir` fuera del repo, y pon `org_alias` en el config de cada organización: una fase aborta si la corrida es de otra.

Para ver el resultado sin correr nada, abre `ejemplos/`: una organización sintética de 4 cuentas con su `reporte.html` (funciona offline) y los diagramas `.drawio`.

## Cobertura: qué tan completo es el inventario

`3-validar/cobertura.py` compara el inventario contra la Tagging API, un oráculo independiente de los colectores, y genera `tareas_extra` para volver a correr la fase 2 sobre lo que el billing no mostró (típicamente lo que está en free tier).

> **Salvedad:** ningún inventario automático es 100% exhaustivo. El oráculo solo ve recursos que tienen o tuvieron tags. Algunos subrecursos se resumen a propósito (recovery points, revisiones de task definitions, ENIs). Y siempre se puede traspapelar algún recurso de un servicio poco común o recién lanzado. El método no promete "todo": **mide su cobertura, muestra qué falta y dice qué colector escribir después**.

## Agregar un colector

Cada colector es un archivo en `2-inventariar/colectores/` con `NAME`, `TRIGGERS` (nombres exactos de servicio de Cost Explorer), `ALWAYS`, `GLOBAL` y `collect(session, region, account_id)`. Mira [ec2.py](2-inventariar/colectores/ec2.py) como referencia y la interfaz en [CONTRATO.md](CONTRATO.md). Si un servicio aparece en `findings.md` como "servicio sin colector", ese es el siguiente a escribir.

## Tests

```bash
.venv/bin/python -m pytest -q 2-inventariar/tests 3-validar/tests 4-esquematizar/tests anonimizar/tests
```

Todos usan `botocore.stub.Stubber` o fixtures sintéticas: ningún test llama a AWS.
