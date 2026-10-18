<div align="right">
  <a href="#es">🇪🇸 Español</a> &nbsp;|&nbsp;
  <a href="#en">🇺🇸 English</a> &nbsp;|&nbsp;
  <a href="#pt">🇧🇷 Português</a>
</div>

---

<a id="es"></a>
## Español

### Método DIVE

Inventario de una AWS Organization con muchas cuentas, en una tarde, con IA como copiloto y sin darle a nadie (ni a la IA) permisos de escritura.

| Fase | Pregunta | Fuente de verdad | Entregable |
|---|---|---|---|
| **0 · Preparar** | ¿Cómo entro a todas las cuentas sin riesgo? | IAM Identity Center | permission set `DIVE_ReadOnly` + perfiles `dive-*` |
| **D · Descubrir** | ¿Dónde hay algo? ¿Cuánto pago? | Billing (Cost Explorer, 6 meses) | `discovery.json` |
| **I · Inventariar** | ¿Qué hay exactamente? | APIs de AWS, solo donde el billing dice que hay costo | `inventory.json` |
| **V · Validar** | ¿Está sano? ¿Cuadra con lo que pago? | AWS Doctor + cruce billing ↔ inventario + reglas | `findings.json` / `findings.md` |
| **E · Esquematizar** | ¿Cómo se ve? | `inventory.json` | diagramas `.drawio` + `reporte.html` |
| **5 · Dashboard** | ¿Qué cambió entre corridas? ¿Qué ya revisé? | todas las corridas | dashboard local en 127.0.0.1 |
| Anonimizar | ¿Lo puedo mostrar? | todas las salidas | carpeta `-anon` verificada |

#### Principios

1. **La factura es el primer mapa.** En vez de escanear 30+ servicios × todas las regiones × todas las cuentas, solo se escanea donde hubo uso facturado. Menos llamadas, menos throttling, ninguna llamada de más a producción.
2. **Contratos JSON entre fases.** Cada fase lee un archivo y escribe otro (ver [CONTRATO.md](CONTRATO.md)). Nada se parsea desde texto de consola, y cualquier fase se puede repetir sola.
3. **La IA escribe código y analiza, nunca inventa datos.** Todo lo que está en los JSON viene de AWS. Los prompts de cada fase (`*/prompt.md`) están versionados y exigen citar IDs.
4. **El agente usa tu identidad, no tus permisos.** Aunque seas admin, el escaneo corre con un rol de solo lectura de metadatos: puede listar un secreto, nunca leer su valor.
5. **Sin secretos en la salida.** Los colectores descartan variables de entorno, UserData, task definitions, PSKs, policies y endpoints de suscripción.
6. **Checkpoint humano entre fases.** Se revisa cada entregable antes de seguir.

#### Requisitos

- Python 3.11+ (el anonimizador usa cuantificadores posesivos de `re`) y [uv](https://github.com/astral-sh/uv) (o pip)
- AWS CLI v2 con acceso a la cuenta management
- [aws-doctor](https://github.com/elC0mpa/aws-doctor) (opcional, fase V)

#### Instalación

```bash
uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock   # versiones exactas con hash
# (sin uv: python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock)
cp dive.config.example.yaml dive.config.yaml   # y ajusta management_profile, org_alias y replace_terms
```

#### Configuración

| Clave de `dive.config.yaml` | Para qué |
|---|---|
| `management_profile` | Perfil de la cuenta management (billing y lista de cuentas) |
| `access_mode` / `scan_role_name` | `sso` (perfiles `dive-*`) o `assume_role`; nombre del rol de lectura |
| `billing_months` / `threshold_usd` | Ventana de Cost Explorer y umbral por servicio |
| `include_accounts` / `exclude_accounts` | IDs **entre comillas** (`["012345678901"]`): sin comillas YAML los lee como número (y con un 0 adelante, en octal) |
| `output_dir` | Dónde quedan las corridas. Para un cliente, fuera del repo |
| `org_alias` | Nombre corto de la organización: identifica sus corridas y una fase aborta si la corrida es de otra |
| `anonymize.replace_terms` | Marcas del cliente a reemplazar. **Obligatorio** para anonimizar (o `--sin-terminos`) |
| `anonymize.costs` | `exact`, `rounded` o `percent` en la salida anonimizada |

#### Fase 0 · Preparar el acceso

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

#### Uso: todo en un comando

```bash
bash dive.sh --config ~/clientes/acme/dive.config.yaml            # 5 fases + dashboard
bash dive.sh --config ~/clientes/acme/dive.config.yaml --sin-doctor --sin-dashboard
bash dive.sh                                   # sin --config: busca dive.config.yaml en tu carpeta personal y pregunta
```

Opciones: `--sin-doctor`, `--sin-dashboard`, `--sin-navegador` (abre el dashboard sin abrir el navegador). Comprueba antes el entorno (venv, config, perfiles `dive-*`, sesión de AWS) y, si falta algo, dice el comando exacto para arreglarlo (si la sesión de AWS expiró, ofrece hacer el `aws sso login` ahí mismo). Corre las fases sobre una corrida nueva, se para en la primera que falle (indicando cómo retomar solo esa fase) y al final abre el dashboard.

#### Uso: correr las fases

```bash
export DIVE_CONFIG=dive.config.yaml            # o la ruta a la config de cada organización
.venv/bin/python 1-descubrir/descubrir.py       # ~15 s
.venv/bin/python 2-inventariar/inventariar.py   # ~1-2 min para ~5-20 cuentas
.venv/bin/python 3-validar/validar.py           # ~2 min con aws-doctor (--sin-doctor si no lo tienes)
.venv/bin/python 4-esquematizar/esquematizar.py
.venv/bin/python 4-esquematizar/reporte.py
.venv/bin/python anonimizar/anonimizar.py salida/<corrida>   # sale ≠ 0 si detecta cualquier fuga
```

Las salidas quedan en `salida/<corrida>/` (ignorada por git), una carpeta por corrida (`2026-10-18T101500Z`, UTC). La fase 1 la crea y la anota en `salida/ULTIMA`; las demás fases usan esa, así que dos corridas el mismo día no se pisan. Cada JSON lleva un bloque `run` (organización, moneda, versión, opciones de cada fase) y cada hallazgo un `fingerprint` estable entre corridas: ver [CONTRATO.md](CONTRATO.md).

Además del uso por servicio, el descubrimiento trae los **tipos de uso** (`USAGE_TYPE`: NAT, transferencia, almacenamiento, métricas) y los **cargos que no son uso** (impuestos, cuotas de Savings Plans y RIs, su descuento, créditos): con ellos se arma la factura real del mes.

Para ver el resultado sin correr nada, abre `ejemplos/`: una organización sintética de 4 cuentas con su `reporte.html` (funciona offline) y los diagramas `.drawio`.

#### Uso: dashboard (fase 5)

```bash
.venv/bin/python 5-dashboard/dashboard.py                 # corridas del output_dir de DIVE_CONFIG
.venv/bin/python 5-dashboard/dashboard.py --runs ~/clientes/a/runs --runs ~/clientes/b/runs   # varias organizaciones
.venv/bin/python 5-dashboard/dashboard.py --sin-navegador # solo imprime la URL
```

Abre el navegador solo, en `http://127.0.0.1:8765/#t=<token>` (si el puerto está ocupado, usa otro y lo imprime). Las corridas nuevas aparecen al recargar la página, sin reiniciar.

| Pestaña | Qué muestra (todo con filtro por cuenta) |
|---|---|
| Resumen | Cuentas, regiones y recursos; factura, uso e impuestos de la ventana y del último mes; factura por concepto y mes a mes; uso mensual por cuenta; servicios y tipos de uso más caros; evolución entre corridas |
| Recursos | Inventario completo con filtros y detalle (tags, atributos, relaciones, hallazgos) |
| Hallazgos | Contador por severidad; historia entre corridas (nuevo, resuelto, no verificado); estado editable (abierto, aceptado, ignorado, resuelto) que se guarda por fingerprint |
| Corridas | Cada corrida con su tipo, errores y su `reporte.html` |

#### Cobertura: qué tan completo es el inventario

`3-validar/cobertura.py` compara el inventario contra la Tagging API, un oráculo independiente de los colectores, y genera `tareas_extra` para volver a correr la fase 2 sobre lo que el billing no mostró (típicamente lo que está en free tier).

> **Salvedad:** ningún inventario automático es 100% exhaustivo. El oráculo solo ve recursos que tienen o tuvieron tags. Algunos subrecursos se resumen a propósito (recovery points, revisiones de task definitions, ENIs). Y siempre se puede traspapelar algún recurso de un servicio poco común o recién lanzado. El método no promete "todo": **mide su cobertura, muestra qué falta y dice qué colector escribir después**.

#### Agregar un colector

Cada colector es un archivo en `2-inventariar/colectores/` con `NAME`, `TRIGGERS` (nombres exactos de servicio de Cost Explorer), `ALWAYS`, `GLOBAL`, `TAGS` (`False` si no lee tags) y `collect(session, region, account_id)`. Mira [ec2.py](2-inventariar/colectores/ec2.py) como referencia y la interfaz en [CONTRATO.md](CONTRATO.md). Si un servicio aparece en `findings.md` como "servicio sin colector", ese es el siguiente a escribir.

#### Seguridad

- El escaneo usa un rol de solo lectura de metadatos con `Deny` explícitos (ver Fase 0); nunca tus credenciales de admin.
- Las salidas son el reconocimiento completo de la organización: se crean con permisos solo para tu usuario (umask 077) y los errores no guardan tu sesión ni tu email.
- El dashboard solo escucha en 127.0.0.1, valida el `Host` y pide un token por header en cada llamada (sin cookies: una cookie de 127.0.0.1 viaja a cualquier otro puerto local). El `reporte.html` de cada corrida se abre en un iframe aislado.
- Para compartir, usa la salida anonimizada (`anonimizar.py`), nunca el dashboard ni `salida/`.

#### Tests

```bash
.venv/bin/python -m pytest -q 2-inventariar/tests 3-validar/tests 4-esquematizar/tests anonimizar/tests 5-dashboard/tests
```

Todos usan `botocore.stub.Stubber` o fixtures sintéticas: ningún test llama a AWS.

---

<a id="en"></a>
## English

### DIVE Method

Inventory of an AWS Organization with many accounts, in one afternoon, with AI as a copilot and without giving anyone (not even the AI) write permissions.

| Phase | Question | Source of truth | Deliverable |
|---|---|---|---|
| **0 · Prepare** | How do I get into every account safely? | IAM Identity Center | `DIVE_ReadOnly` permission set + `dive-*` profiles |
| **D · Discover** | Where is something? How much do I pay? | Billing (Cost Explorer, 6 months) | `discovery.json` |
| **I · Inventory** | What exactly is there? | AWS APIs, only where billing shows cost | `inventory.json` |
| **V · Validate** | Is it healthy? Does it match what I pay? | AWS Doctor + billing ↔ inventory cross-check + rules | `findings.json` / `findings.md` |
| **E · Diagram** | What does it look like? | `inventory.json` | `.drawio` diagrams + `reporte.html` |
| **5 · Dashboard** | What changed between runs? What did I already review? | all runs | local dashboard on 127.0.0.1 |
| Anonymize | Can I show it? | all outputs | verified `-anon` folder |

#### Principles

1. **The bill is the first map.** Instead of scanning 30+ services × every region × every account, only what was billed gets scanned. Fewer calls, less throttling, no unnecessary calls to production.
2. **JSON contracts between phases.** Each phase reads one file and writes another (see [CONTRATO.md](CONTRATO.md)). Nothing is parsed from console text, and any phase can be rerun on its own.
3. **The AI writes code and analyzes; it never makes up data.** Everything in the JSON files comes from AWS. Each phase's prompts (`*/prompt.md`) are versioned and require citing IDs.
4. **The agent uses your identity, not your permissions.** Even if you are an admin, the scan runs with a metadata read-only role: it can list a secret, never read its value.
5. **No secrets in the output.** Collectors drop environment variables, UserData, task definitions, PSKs, policies and subscription endpoints.
6. **Human checkpoint between phases.** Each deliverable is reviewed before moving on.

#### Prerequisites

- Python 3.11+ (the anonymizer uses possessive quantifiers from `re`) and [uv](https://github.com/astral-sh/uv) (or pip)
- AWS CLI v2 with access to the management account
- [aws-doctor](https://github.com/elC0mpa/aws-doctor) (optional, phase V)

#### Installation / Setup

```bash
uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock   # exact versions with hashes
# (without uv: python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock)
cp dive.config.example.yaml dive.config.yaml   # and set management_profile, org_alias and replace_terms
```

#### Configuration

| `dive.config.yaml` key | Purpose |
|---|---|
| `management_profile` | Management account profile (billing and account list) |
| `access_mode` / `scan_role_name` | `sso` (`dive-*` profiles) or `assume_role`; name of the read-only role |
| `billing_months` / `threshold_usd` | Cost Explorer window and per-service threshold |
| `include_accounts` / `exclude_accounts` | IDs **quoted** (`["012345678901"]`): unquoted, YAML reads them as numbers (and with a leading 0, as octal) |
| `output_dir` | Where runs are written. For a client, outside the repo |
| `org_alias` | Short name of the organization: identifies its runs, and a phase aborts if the run belongs to another one |
| `anonymize.replace_terms` | Client brand names to replace. **Required** to anonymize (or `--sin-terminos`) |
| `anonymize.costs` | `exact`, `rounded` or `percent` in the anonymized output |

#### Phase 0 · Prepare access

The scan role is `ViewOnlyAccess` + [a minimal policy](setup/readonly-policy.json) with exactly what the collectors and aws-doctor need. `ViewOnlyAccess` alone falls short (no Cost Explorer, no Transit Gateway), and `ReadOnlyAccess` is too much (it includes `s3:GetObject`: reading data).

The policy also **explicitly denies** what does read content even if it looks like metadata: API Gateway API key values, ECS task definition and Lambda environment variables (`GetFunctionConfiguration`), UserData (launch templates, launch configurations, spot), secrets, SSM parameters, S3 objects, log events and CloudFormation outputs. Those `Deny` statements keep working even if AWS widens `ViewOnlyAccess` in the future. Residual risk that cannot be closed without breaking the inventory: `lambda:ListFunctions`/`ListVersionsByFunction` return Lambda environment variables, `glue:GetJobs` returns the jobs' `DefaultArguments`, and API Gateway stages return their stage variables. Collectors drop those fields, but whoever holds the role can see them: don't keep secrets there.

Optional: aws-doctor's Lambda memory check uses Logs Insights (`logs:StartQuery`, `logs:GetQueryResults`), which reads the **content** of the functions' logs. It is not in the policy; add it only if you accept that access.

Pick based on what your organization has:

| Your org has… | What to do |
|---|---|
| **IAM Identity Center (SSO)** | `bash setup/crear_permission_set.sh --profile <management> --user <your-user>` (dry-run), then with `--apply`. Then `python setup/generar_perfiles.py`. It can be rerun: if you change the policy, run it again. Rollback: `setup/eliminar_permission_set.sh`. |
| **Organizations without SSO** | Deploy a `DIVE_ReadOnly` role with a StackSet to every account (same policy) and use `access_mode: assume_role`. |
| **Only `OrganizationAccountAccessRole` / `AWSControlTowerExecution`** | Those roles are **admin**: use them to *deploy* the read role, never to scan. |

Check that the role cannot write (changes nothing; `--dry-run` only evaluates the permission):

```bash
AWS_CONFIG_FILE=~/.aws/dive-config aws ec2 create-tags --dry-run --resources <vpc-id> \
  --tags Key=dive,Value=test --profile dive-<account_id>
# → UnauthorizedOperation
```

#### Usage: everything in one command

```bash
bash dive.sh --config ~/clientes/acme/dive.config.yaml            # 5 phases + dashboard
bash dive.sh --config ~/clientes/acme/dive.config.yaml --sin-doctor --sin-dashboard
bash dive.sh                                   # without --config: looks for dive.config.yaml in your home folder and asks
```

Options: `--sin-doctor`, `--sin-dashboard`, `--sin-navegador` (starts the dashboard without opening the browser). It first checks the environment (venv, config, `dive-*` profiles, AWS session) and, if something is missing, prints the exact command to fix it (if the AWS session expired, it offers to run `aws sso login` right there). It runs the phases on a new run, stops at the first one that fails (showing how to resume only that phase), and opens the dashboard at the end.

#### Usage: running the phases

```bash
export DIVE_CONFIG=dive.config.yaml            # or the config path of each organization
.venv/bin/python 1-descubrir/descubrir.py       # ~15 s
.venv/bin/python 2-inventariar/inventariar.py   # ~1-2 min for ~5-20 accounts
.venv/bin/python 3-validar/validar.py           # ~2 min with aws-doctor (--sin-doctor if you don't have it)
.venv/bin/python 4-esquematizar/esquematizar.py
.venv/bin/python 4-esquematizar/reporte.py
.venv/bin/python anonimizar/anonimizar.py salida/<corrida>   # exits ≠ 0 if it detects any leak
```

Outputs go to `salida/<corrida>/` (ignored by git), one folder per run (`2026-10-18T101500Z`, UTC). Phase 1 creates it and records it in `salida/ULTIMA`; the other phases use that one, so two runs on the same day don't overwrite each other. Each JSON carries a `run` block (organization, currency, version, options of each phase) and each finding a `fingerprint` that is stable across runs: see [CONTRATO.md](CONTRATO.md).

Besides usage per service, discovery brings the **usage types** (`USAGE_TYPE`: NAT, data transfer, storage, metrics) and the **charges that are not usage** (taxes, Savings Plans and RI fees, their discount, credits): together they make up the actual monthly bill.

To see the result without running anything, open `ejemplos/`: a synthetic 4-account organization with its `reporte.html` (works offline) and the `.drawio` diagrams.

#### Usage: dashboard (phase 5)

```bash
.venv/bin/python 5-dashboard/dashboard.py                 # runs in the output_dir of DIVE_CONFIG
.venv/bin/python 5-dashboard/dashboard.py --runs ~/clientes/a/runs --runs ~/clientes/b/runs   # several organizations
.venv/bin/python 5-dashboard/dashboard.py --sin-navegador # only prints the URL
```

It opens the browser by itself at `http://127.0.0.1:8765/#t=<token>` (if the port is busy, it uses another one and prints it). New runs show up when you reload the page, no restart needed.

| Tab | What it shows (all with an account filter) |
|---|---|
| Resumen | Accounts, regions and resources; bill, usage and taxes for the window and the last month; bill by item and month by month; monthly usage per account; most expensive services and usage types; evolution between runs |
| Recursos | Full inventory with filters and detail (tags, attributes, relations, findings) |
| Hallazgos | Count by severity; history between runs (new, resolved, not verified); editable state (open, accepted, ignored, resolved) saved per fingerprint |
| Corridas | Each run with its type, errors and its `reporte.html` |

#### Coverage: how complete is the inventory

`3-validar/cobertura.py` compares the inventory against the Tagging API, an oracle independent of the collectors, and produces `tareas_extra` to rerun phase 2 on what billing didn't show (typically free-tier resources).

> **Caveat:** no automated inventory is 100% exhaustive. The oracle only sees resources that have or had tags. Some sub-resources are summarized on purpose (recovery points, task definition revisions, ENIs). And a resource from an unusual or newly launched service can always slip through. The method doesn't promise "everything": **it measures its coverage, shows what is missing and says which collector to write next**.

#### Adding a collector

Each collector is a file in `2-inventariar/colectores/` with `NAME`, `TRIGGERS` (exact Cost Explorer service names), `ALWAYS`, `GLOBAL`, `TAGS` (`False` if it doesn't read tags) and `collect(session, region, account_id)`. Use [ec2.py](2-inventariar/colectores/ec2.py) as a reference and the interface in [CONTRATO.md](CONTRATO.md). If a service shows up in `findings.md` as "servicio sin colector", that is the next one to write.

#### Security

- The scan uses a metadata read-only role with explicit `Deny` statements (see Phase 0); never your admin credentials.
- Outputs are the full reconnaissance of the organization: they are created readable only by your user (umask 077), and errors don't store your session or email.
- The dashboard only listens on 127.0.0.1, validates `Host` and requires a token in a header on every call (no cookies: a 127.0.0.1 cookie travels to any other local port). Each run's `reporte.html` opens in an isolated iframe.
- To share, use the anonymized output (`anonimizar.py`), never the dashboard or `salida/`.

#### Tests

```bash
.venv/bin/python -m pytest -q 2-inventariar/tests 3-validar/tests 4-esquematizar/tests anonimizar/tests 5-dashboard/tests
```

All of them use `botocore.stub.Stubber` or synthetic fixtures: no test calls AWS.

---

<a id="pt"></a>
## Português

### Método DIVE

Inventário de uma AWS Organization com muitas contas, numa tarde, com IA como copiloto e sem dar a ninguém (nem à IA) permissões de escrita.

| Fase | Pergunta | Fonte de verdade | Entregável |
|---|---|---|---|
| **0 · Preparar** | Como entro em todas as contas sem risco? | IAM Identity Center | permission set `DIVE_ReadOnly` + perfis `dive-*` |
| **D · Descobrir** | Onde há alguma coisa? Quanto pago? | Billing (Cost Explorer, 6 meses) | `discovery.json` |
| **I · Inventariar** | O que existe exatamente? | APIs da AWS, só onde o billing mostra custo | `inventory.json` |
| **V · Validar** | Está saudável? Bate com o que pago? | AWS Doctor + cruzamento billing ↔ inventário + regras | `findings.json` / `findings.md` |
| **E · Esquematizar** | Como fica? | `inventory.json` | diagramas `.drawio` + `reporte.html` |
| **5 · Dashboard** | O que mudou entre execuções? O que já revisei? | todas as execuções | dashboard local em 127.0.0.1 |
| Anonimizar | Posso mostrar? | todas as saídas | pasta `-anon` verificada |

#### Princípios

1. **A fatura é o primeiro mapa.** Em vez de varrer 30+ serviços × todas as regiões × todas as contas, só se varre onde houve uso faturado. Menos chamadas, menos throttling, nenhuma chamada a mais à produção.
2. **Contratos JSON entre fases.** Cada fase lê um arquivo e escreve outro (veja [CONTRATO.md](CONTRATO.md)). Nada é extraído de texto do console, e qualquer fase pode ser repetida sozinha.
3. **A IA escreve código e analisa, nunca inventa dados.** Tudo o que está nos JSON vem da AWS. Os prompts de cada fase (`*/prompt.md`) são versionados e exigem citar IDs.
4. **O agente usa a sua identidade, não as suas permissões.** Mesmo que você seja admin, a varredura roda com um role de somente leitura de metadados: pode listar um secret, nunca ler o valor.
5. **Sem segredos na saída.** Os coletores descartam variáveis de ambiente, UserData, task definitions, PSKs, policies e endpoints de assinatura.
6. **Checkpoint humano entre fases.** Cada entregável é revisado antes de seguir.

#### Pré-requisitos

- Python 3.11+ (o anonimizador usa quantificadores possessivos de `re`) e [uv](https://github.com/astral-sh/uv) (ou pip)
- AWS CLI v2 com acesso à conta management
- [aws-doctor](https://github.com/elC0mpa/aws-doctor) (opcional, fase V)

#### Instalação / Configuração inicial

```bash
uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock   # versões exatas com hash
# (sem uv: python3.11 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock)
cp dive.config.example.yaml dive.config.yaml   # e ajuste management_profile, org_alias e replace_terms
```

#### Configuração

| Chave de `dive.config.yaml` | Para quê |
|---|---|
| `management_profile` | Perfil da conta management (billing e lista de contas) |
| `access_mode` / `scan_role_name` | `sso` (perfis `dive-*`) ou `assume_role`; nome do role de leitura |
| `billing_months` / `threshold_usd` | Janela do Cost Explorer e limite por serviço |
| `include_accounts` / `exclude_accounts` | IDs **entre aspas** (`["012345678901"]`): sem aspas o YAML lê como número (e, com um 0 na frente, em octal) |
| `output_dir` | Onde ficam as execuções. Para um cliente, fora do repositório |
| `org_alias` | Nome curto da organização: identifica as execuções dela, e uma fase aborta se a execução for de outra |
| `anonymize.replace_terms` | Marcas do cliente a substituir. **Obrigatório** para anonimizar (ou `--sin-terminos`) |
| `anonymize.costs` | `exact`, `rounded` ou `percent` na saída anonimizada |

#### Fase 0 · Preparar o acesso

O role de varredura é `ViewOnlyAccess` + [uma policy mínima](setup/readonly-policy.json) com exatamente o que os coletores e o aws-doctor pedem. `ViewOnlyAccess` sozinha não basta (não tem Cost Explorer nem Transit Gateway), e `ReadOnlyAccess` é demais (inclui `s3:GetObject`: ler dados).

A policy também **nega explicitamente** o que lê conteúdo, mesmo parecendo metadado: valores de API keys do API Gateway, variáveis de ambiente de task definitions do ECS e do Lambda (`GetFunctionConfiguration`), UserData (launch templates, launch configurations, spot), secrets, parâmetros SSM, objetos S3, eventos de logs e outputs do CloudFormation. Esses `Deny` continuam valendo mesmo que a AWS amplie o `ViewOnlyAccess` no futuro. Risco residual que não dá para fechar sem quebrar o inventário: `lambda:ListFunctions`/`ListVersionsByFunction` devolvem as variáveis de ambiente do Lambda, `glue:GetJobs` os `DefaultArguments` dos jobs e os stages do API Gateway as stage variables. Os coletores descartam esses campos, mas quem tiver o role consegue vê-los: não guarde segredos aí.

Opcional: o check de memória de Lambda do aws-doctor usa Logs Insights (`logs:StartQuery`, `logs:GetQueryResults`), que lê o **conteúdo** dos logs das funções. Não vem na policy; adicione só se aceitar esse acesso.

Escolha conforme o que a sua organização tem:

| Sua org tem… | O que fazer |
|---|---|
| **IAM Identity Center (SSO)** | `bash setup/crear_permission_set.sh --profile <management> --user <seu-usuario>` (dry-run), depois com `--apply`. Em seguida `python setup/generar_perfiles.py`. Pode rodar de novo: se mudar a policy, rode outra vez. Reverter: `setup/eliminar_permission_set.sh`. |
| **Organizations sem SSO** | Implante um role `DIVE_ReadOnly` com um StackSet em todas as contas (mesma policy) e use `access_mode: assume_role`. |
| **Só `OrganizationAccountAccessRole` / `AWSControlTowerExecution`** | Esses roles são **admin**: use-os para *implantar* o role de leitura, nunca para varrer. |

Verificação de que o role não consegue escrever (não altera nada, `--dry-run` só avalia a permissão):

```bash
AWS_CONFIG_FILE=~/.aws/dive-config aws ec2 create-tags --dry-run --resources <vpc-id> \
  --tags Key=dive,Value=test --profile dive-<account_id>
# → UnauthorizedOperation
```

#### Uso: tudo num comando

```bash
bash dive.sh --config ~/clientes/acme/dive.config.yaml            # 5 fases + dashboard
bash dive.sh --config ~/clientes/acme/dive.config.yaml --sin-doctor --sin-dashboard
bash dive.sh                                   # sem --config: procura dive.config.yaml na sua pasta pessoal e pergunta
```

Opções: `--sin-doctor`, `--sin-dashboard`, `--sin-navegador` (abre o dashboard sem abrir o navegador). Antes verifica o ambiente (venv, config, perfis `dive-*`, sessão da AWS) e, se faltar algo, mostra o comando exato para resolver (se a sessão da AWS expirou, oferece fazer o `aws sso login` ali mesmo). Roda as fases numa execução nova, para na primeira que falhar (mostrando como retomar só essa fase) e no fim abre o dashboard.

#### Uso: rodar as fases

```bash
export DIVE_CONFIG=dive.config.yaml            # ou o caminho da config de cada organização
.venv/bin/python 1-descubrir/descubrir.py       # ~15 s
.venv/bin/python 2-inventariar/inventariar.py   # ~1-2 min para ~5-20 contas
.venv/bin/python 3-validar/validar.py           # ~2 min com aws-doctor (--sin-doctor se não tiver)
.venv/bin/python 4-esquematizar/esquematizar.py
.venv/bin/python 4-esquematizar/reporte.py
.venv/bin/python anonimizar/anonimizar.py salida/<corrida>   # sai ≠ 0 se detectar qualquer vazamento
```

As saídas ficam em `salida/<corrida>/` (ignorada pelo git), uma pasta por execução (`2026-10-18T101500Z`, UTC). A fase 1 cria a pasta e a anota em `salida/ULTIMA`; as outras fases usam essa, então duas execuções no mesmo dia não se sobrescrevem. Cada JSON leva um bloco `run` (organização, moeda, versão, opções de cada fase) e cada achado um `fingerprint` estável entre execuções: veja [CONTRATO.md](CONTRATO.md).

Além do uso por serviço, a descoberta traz os **tipos de uso** (`USAGE_TYPE`: NAT, transferência, armazenamento, métricas) e os **encargos que não são uso** (impostos, mensalidades de Savings Plans e RIs, o desconto deles, créditos): com eles se monta a fatura real do mês.

Para ver o resultado sem rodar nada, abra `ejemplos/`: uma organização sintética de 4 contas com o `reporte.html` (funciona offline) e os diagramas `.drawio`.

#### Uso: dashboard (fase 5)

```bash
.venv/bin/python 5-dashboard/dashboard.py                 # execuções do output_dir de DIVE_CONFIG
.venv/bin/python 5-dashboard/dashboard.py --runs ~/clientes/a/runs --runs ~/clientes/b/runs   # várias organizações
.venv/bin/python 5-dashboard/dashboard.py --sin-navegador # só imprime a URL
```

Abre o navegador sozinho em `http://127.0.0.1:8765/#t=<token>` (se a porta estiver ocupada, usa outra e a imprime). Execuções novas aparecem ao recarregar a página, sem reiniciar.

| Aba | O que mostra (tudo com filtro por conta) |
|---|---|
| Resumen | Contas, regiões e recursos; fatura, uso e impostos da janela e do último mês; fatura por item e mês a mês; uso mensal por conta; serviços e tipos de uso mais caros; evolução entre execuções |
| Recursos | Inventário completo com filtros e detalhe (tags, atributos, relações, achados) |
| Hallazgos | Contador por severidade; histórico entre execuções (novo, resolvido, não verificado); estado editável (aberto, aceito, ignorado, resolvido) salvo por fingerprint |
| Corridas | Cada execução com o tipo, os erros e o `reporte.html` |

#### Cobertura: quão completo é o inventário

`3-validar/cobertura.py` compara o inventário com a Tagging API, um oráculo independente dos coletores, e gera `tareas_extra` para rodar de novo a fase 2 sobre o que o billing não mostrou (normalmente o que está no free tier).

> **Ressalva:** nenhum inventário automático é 100% exaustivo. O oráculo só vê recursos que têm ou tiveram tags. Alguns sub-recursos são resumidos de propósito (recovery points, revisões de task definitions, ENIs). E sempre pode escapar algum recurso de um serviço pouco comum ou recém-lançado. O método não promete "tudo": **mede a cobertura, mostra o que falta e diz qual coletor escrever depois**.

#### Adicionar um coletor

Cada coletor é um arquivo em `2-inventariar/colectores/` com `NAME`, `TRIGGERS` (nomes exatos de serviço do Cost Explorer), `ALWAYS`, `GLOBAL`, `TAGS` (`False` se não lê tags) e `collect(session, region, account_id)`. Use [ec2.py](2-inventariar/colectores/ec2.py) como referência e a interface em [CONTRATO.md](CONTRATO.md). Se um serviço aparecer em `findings.md` como "servicio sin colector", é o próximo a escrever.

#### Segurança

- A varredura usa um role de somente leitura de metadados com `Deny` explícitos (veja a Fase 0); nunca suas credenciais de admin.
- As saídas são o reconhecimento completo da organização: são criadas com permissão só para o seu usuário (umask 077), e os erros não guardam a sua sessão nem o seu email.
- O dashboard só escuta em 127.0.0.1, valida o `Host` e exige um token por header em cada chamada (sem cookies: um cookie de 127.0.0.1 viaja para qualquer outra porta local). O `reporte.html` de cada execução abre num iframe isolado.
- Para compartilhar, use a saída anonimizada (`anonimizar.py`), nunca o dashboard nem `salida/`.

#### Testes

```bash
.venv/bin/python -m pytest -q 2-inventariar/tests 3-validar/tests 4-esquematizar/tests anonimizar/tests 5-dashboard/tests
```

Todos usam `botocore.stub.Stubber` ou fixtures sintéticas: nenhum teste chama a AWS.
