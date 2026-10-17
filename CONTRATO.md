# Contrato entre fases

Cada fase lee el archivo de la anterior y escribe el suyo en `salida/<fecha>/`. Nada se parsea desde texto de consola.

```
setup/  →  1-descubrir → discovery.json
                          ↓
           2-inventariar → inventory.json
                          ↓
           3-validar     → findings.json + findings.md
                          ↓
           4-esquematizar → diagramas/*.drawio + reporte.html
           anonimizar    → cualquier archivo de salida → versión presentable
```

## Reglas para todo el código

1. **Solo lectura.** Solo se llaman operaciones `Describe*`, `List*`, `Get*` de metadatos. Nunca `GetObject`, `GetSecretValue`, `Decrypt` ni nada que lea contenido.
2. **Sin secretos en la salida.** Los colectores nunca guardan variables de entorno, `UserData`, `environment`/`secrets` de task definitions, parámetros de conexión, passwords ni certificados. Si un campo puede traer eso, no se copia.
3. **Nada hardcodeado.** Cuentas, regiones y nombres salen de la API o de `dive.config.yaml`.
4. **Los errores no detienen la corrida.** Un `AccessDenied` o throttling en una cuenta/región se registra en `errors` y se sigue.

## discovery.json (fase 1)

```json
{
  "generated_at": "2026-09-26T15:00:00Z",
  "period": {"start": "2026-03-01", "end": "2026-09-01", "months": 6},
  "threshold_usd": 0.01,
  "accounts": {
    "123456789012": {
      "name": "Produccion",
      "total_usd": 1234.56,
      "regions": {
        "us-east-1": {
          "total_usd": 1000.0,
          "services": {"Amazon Elastic Compute Cloud - Compute": 600.0, "Amazon Relational Database Service": 400.0},
          "monthly_usd": {"2026-03": 160.0, "2026-04": 170.0},
          "services_monthly_usd": {"Amazon Relational Database Service": {"2026-03": 70.0, "2026-04": 70.0}}
        },
        "global": {"total_usd": 5.0, "services": {"Amazon Route 53": 5.0}, "monthly_usd": {}}
      }
    }
  }
}
```

- La región `global` agrupa lo que Cost Explorer reporta como `NoRegion`/`global`.
- `services` usa el nombre exacto de la dimensión `SERVICE` de Cost Explorer.
- Solo cuenta registros de **uso** (`Usage`, `DiscountedUsage`, `SavingsPlanCoveredUsage`). Las cuotas de RIs/Savings Plans no son recursos y se excluyen.
- `services_monthly_usd` permite saber si un servicio se sigue cobrando en el último mes o dejó de cobrarse (recurso ya borrado).

## inventory.json (fase 2)

```json
{
  "generated_at": "2026-09-26T15:20:00Z",
  "accounts": {"123456789012": {"name": "Produccion"}},
  "scanned": [{"account_id": "123456789012", "region": "us-east-1", "collector": "ec2", "count": 4, "seconds": 1.2}],
  "resources": [
    {
      "account_id": "123456789012",
      "region": "us-east-1",
      "service": "ec2",
      "type": "instance",
      "id": "i-0abc...",
      "arn": "arn:aws:ec2:us-east-1:123456789012:instance/i-0abc...",
      "name": "api-prod-1",
      "state": "running",
      "tags": {"Environment": "prod"},
      "attrs": {"instance_type": "t3.large", "private_ip": "10.0.1.10", "public_ip": null},
      "relations": {"vpc_id": "vpc-1", "subnet_ids": ["subnet-1"], "security_group_ids": ["sg-1"]}
    }
  ],
  "errors": [{"account_id": "123456789012", "region": "eu-west-1", "collector": "rds", "error": "AccessDenied: ..."}]
}
```

- `service`: nombre corto del colector (`ec2`, `rds`, `vpc`...). `type`: subtipo (`instance`, `volume`, `security_group`...).
- `name`: tag `Name` o el nombre propio del recurso; `null` si no tiene.
- `relations` solo lleva IDs, y solo las claves que apliquen: `vpc_id`, `subnet_ids`, `security_group_ids`, `target_ids`, `attached_to`.

## Interfaz de un colector (`2-inventariar/colectores/<nombre>.py`)

```python
NAME = "rds"                       # nombre corto, igual al del archivo
TRIGGERS = ["Amazon Relational Database Service"]   # servicios de Cost Explorer que lo activan
ALWAYS = False                     # True = corre en toda cuenta/región con costo (ej. vpc, que no factura)
GLOBAL = False                     # True = corre una sola vez por cuenta (en us-east-1), ej. s3, cloudfront, route53

def collect(session, region, account_id):
    """Devuelve una lista de dicts con la forma de `resources` de inventory.json."""
```

- Usar paginadores (`client.get_paginator`) siempre que existan.
- Crear clientes con `common.aws.client(session, "<servicio>", region)`: ya trae reintentos adaptativos para throttling.
- Construir cada recurso con `common.recurso(...)` para que todos tengan las mismas claves.

## findings.json (fase 3)

```json
{
  "generated_at": "...",
  "findings": [
    {
      "id": "F001",
      "category": "costo | desperdicio | riesgo | inventario",
      "severity": "alta | media | baja",
      "account_id": "123456789012",
      "region": "us-east-1",
      "resource_id": "vol-0abc",
      "title": "Volumen EBS sin adjuntar",
      "detail": "...",
      "monthly_usd": 12.5,
      "source": "aws-doctor | cruce-billing | regla"
    }
  ]
}
```
