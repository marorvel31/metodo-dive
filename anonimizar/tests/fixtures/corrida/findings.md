# Hallazgos · ACME

| Cuenta | Recurso | Costo |
|---|---|---|
| Acme Prod (111122223333) | vol-0abc12345678def90 | $1,234.56 |
| ACMEPAY-Shared-Services (444455556666) | AcmePay-api-prod | 765.44 USD |

- La función `arn:aws:lambda:us-east-2:444455556666:function:AcmePay-api-prod` responde en https://api.pagos.acme.com
- El ALB internal-acme-alb-1234567890.us-east-1.elb.amazonaws.com sale por 34.201.18.9.
- Cuenta de pruebas Dev sin costo. Archivo fuente: findings.json y reporte.html.
- Un número que parece cuenta: 999900001111.
