#!/usr/bin/env bash
# Fase 0 del Método DIVE: crea un permission set de solo lectura en IAM Identity Center
# y lo asigna a un usuario en todas las cuentas ACTIVE de la organización.
#
# Uso:
#   bash crear_permission_set.sh --profile <perfil-management> --user <username-sso>           # dry-run
#   bash crear_permission_set.sh --profile <perfil-management> --user <username-sso> --apply   # aplica
#
# Reversa: bash eliminar_permission_set.sh --profile ... --user ...

set -euo pipefail

PS_NAME="DIVE_ReadOnly"
SESSION="PT4H"
PROFILE=""; USERNAME=""; APPLY=false
HERE="$(cd "$(dirname "$0")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --user)    USERNAME="$2"; shift 2 ;;
    --apply)   APPLY=true; shift ;;
    *) echo "Argumento desconocido: $1"; exit 1 ;;
  esac
done
[[ -z "$PROFILE" || -z "$USERNAME" ]] && { echo "Faltan --profile y --user"; exit 1; }

aws_() { aws --profile "$PROFILE" "$@"; }
# Ejecuta (o muestra en dry-run). ConflictException en Identity Center significa "otra escritura aún
# no se propagó; reintenta", no "ya existe": se reintenta con espera creciente. Solo en
# attach-managed-policy un Conflict es "ya estaba adjunta". Cualquier otro error aborta, para no
# dejar el permission set a medias (ej. sin la inline policy con los Deny).
run() {
  if ! $APPLY; then echo "[dry-run] $*"; return 0; fi
  local err intento
  for intento in 1 2 3 4 5; do
    if err=$("$@" 2>&1 >/dev/null); then return 0; fi
    if ! grep -q "ConflictException" <<<"$err"; then echo "ERROR en $5: $err" >&2; exit 1; fi
    if [[ "$5" == "attach-managed-policy-to-permission-set" ]]; then echo "  (ViewOnlyAccess ya estaba adjunta)"; return 0; fi
    sleep $((intento * 3))
  done
  echo "ERROR en $5 tras 5 intentos: $err" >&2; exit 1
}

INSTANCE=$(aws_ sso-admin list-instances --query 'Instances[0].InstanceArn' --output text)
[[ "$INSTANCE" == "None" || -z "$INSTANCE" ]] && { echo "No hay instancia de IAM Identity Center"; exit 1; }
STORE=$(aws_ sso-admin list-instances --query 'Instances[0].IdentityStoreId' --output text)
USER_ID=$(aws_ identitystore list-users --identity-store-id "$STORE" \
  --filters AttributePath=UserName,AttributeValue="$USERNAME" --query 'Users[0].UserId' --output text)
[[ "$USER_ID" == "None" ]] && { echo "Usuario $USERNAME no existe"; exit 1; }
ACCOUNTS=$(aws_ organizations list-accounts --query "Accounts[?Status=='ACTIVE'].Id" --output text)

echo "Instancia: $INSTANCE | Usuario: $USERNAME ($USER_ID) | Cuentas: $(wc -w <<<"$ACCOUNTS")"

PS_ARN=""
for ps in $(aws_ sso-admin list-permission-sets --instance-arn "$INSTANCE" --query 'PermissionSets[]' --output text); do
  name=$(aws_ sso-admin describe-permission-set --instance-arn "$INSTANCE" --permission-set-arn "$ps" \
    --query 'PermissionSet.Name' --output text)
  [[ "$name" == "$PS_NAME" ]] && PS_ARN="$ps"
done

if [[ -z "$PS_ARN" ]]; then
  if $APPLY; then
    PS_ARN=$(aws_ sso-admin create-permission-set --instance-arn "$INSTANCE" --name "$PS_NAME" \
      --session-duration "$SESSION" --description "Metodo DIVE: inventario de solo lectura" \
      --query 'PermissionSet.PermissionSetArn' --output text)
    echo "Creado $PS_NAME: $PS_ARN"
  else
    echo "[dry-run] crear permission set $PS_NAME (sesión $SESSION)"; PS_ARN="<nuevo>"
  fi
else
  echo "Ya existe $PS_NAME: $PS_ARN"
fi

run aws --profile "$PROFILE" sso-admin attach-managed-policy-to-permission-set --instance-arn "$INSTANCE" \
  --permission-set-arn "$PS_ARN" --managed-policy-arn arn:aws:iam::aws:policy/job-function/ViewOnlyAccess
run aws --profile "$PROFILE" sso-admin put-inline-policy-to-permission-set --instance-arn "$INSTANCE" \
  --permission-set-arn "$PS_ARN" --inline-policy "file://$HERE/readonly-policy.json"
if $APPLY; then
  # Sin la inline policy el rol queda solo con ViewOnlyAccess, sin los Deny: se comprueba que quedó igual al archivo.
  aws_ sso-admin get-inline-policy-for-permission-set --instance-arn "$INSTANCE" --permission-set-arn "$PS_ARN" \
    --query InlinePolicy --output text | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin) != json.load(open(sys.argv[1])))' \
    "$HERE/readonly-policy.json" || { echo "ERROR: la inline policy aplicada no coincide con readonly-policy.json" >&2; exit 1; }
fi

# Re-ejecutable: si el permission set ya existía, empuja la policy actualizada a las cuentas ya asignadas.
if [[ "$PS_ARN" != "<nuevo>" ]] && $APPLY; then
  REQ=$(aws_ sso-admin provision-permission-set --instance-arn "$INSTANCE" --permission-set-arn "$PS_ARN" \
    --target-type ALL_PROVISIONED_ACCOUNTS --query 'PermissionSetProvisioningStatus.RequestId' --output text)
  for _ in $(seq 1 30); do
    estado=$(aws_ sso-admin describe-permission-set-provisioning-status --instance-arn "$INSTANCE" \
      --provision-permission-set-request-id "$REQ" --query 'PermissionSetProvisioningStatus.Status' --output text)
    [[ "$estado" == "IN_PROGRESS" ]] || break
    sleep 3
  done
  [[ "$estado" == "SUCCEEDED" ]] || { echo "ERROR: aprovisionamiento $REQ quedó en $estado" >&2; exit 1; }
  echo "Policy actualizada en las cuentas ya asignadas."
elif [[ "$PS_ARN" != "<nuevo>" ]]; then
  echo "[dry-run] provision-permission-set en las cuentas ya asignadas"
fi

for acct in $ACCOUNTS; do
  run aws --profile "$PROFILE" sso-admin create-account-assignment --instance-arn "$INSTANCE" \
    --target-id "$acct" --target-type AWS_ACCOUNT --permission-set-arn "$PS_ARN" \
    --principal-type USER --principal-id "$USER_ID"
done

# create-account-assignment es asíncrono: se confirma que cada cuenta quedó asignada.
if $APPLY; then
  echo "Esperando el aprovisionamiento (hasta ~2 min)..."
  faltan="$ACCOUNTS"
  for _ in $(seq 1 24); do
    pendientes=""
    for acct in $faltan; do
      n=$(aws_ sso-admin list-account-assignments --instance-arn "$INSTANCE" --account-id "$acct" \
        --permission-set-arn "$PS_ARN" --query "length(AccountAssignments[?PrincipalId=='$USER_ID'])" --output text)
      [[ "$n" == "1" ]] || pendientes="$pendientes $acct"
    done
    faltan="$pendientes"
    [[ -z "${faltan// /}" ]] && break
    sleep 5
  done
  if [[ -n "${faltan// /}" ]]; then
    echo "Sin asignación tras la espera:$faltan. Revisa la consola y vuelve a correr el script." >&2
    exit 1
  fi
  echo "Asignado en todas las cuentas."
fi

if $APPLY; then
  echo "Listo. Genera los perfiles con: python setup/generar_perfiles.py (usa management_profile y scan_role_name de dive.config.yaml)"
fi
