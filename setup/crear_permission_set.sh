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
run() { if $APPLY; then "$@"; else echo "[dry-run] $*"; fi; }

INSTANCE=$(aws_ sso-admin list-instances --query 'Instances[0].InstanceArn' --output text)
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
  --permission-set-arn "$PS_ARN" --managed-policy-arn arn:aws:iam::aws:policy/job-function/ViewOnlyAccess \
  || echo "  ViewOnlyAccess ya estaba adjunta"
run aws --profile "$PROFILE" sso-admin put-inline-policy-to-permission-set --instance-arn "$INSTANCE" \
  --permission-set-arn "$PS_ARN" --inline-policy "file://$HERE/readonly-policy.json"

# Re-ejecutable: si el permission set ya existía, empuja la policy actualizada a las cuentas ya asignadas.
if [[ "$PS_ARN" != "<nuevo>" ]]; then
  run aws --profile "$PROFILE" sso-admin provision-permission-set --instance-arn "$INSTANCE" \
    --permission-set-arn "$PS_ARN" --target-type ALL_PROVISIONED_ACCOUNTS \
    --query 'PermissionSetProvisioningStatus.Status' --output text
fi

for acct in $ACCOUNTS; do
  run aws --profile "$PROFILE" sso-admin create-account-assignment --instance-arn "$INSTANCE" \
    --target-id "$acct" --target-type AWS_ACCOUNT --permission-set-arn "$PS_ARN" \
    --principal-type USER --principal-id "$USER_ID" --query 'AccountAssignmentCreationStatus.Status' --output text \
    || echo "  $acct: ya asignada"
done

$APPLY && echo "Listo. Genera los perfiles con: python setup/generar_perfiles.py (usa management_profile y scan_role_name de dive.config.yaml)"
