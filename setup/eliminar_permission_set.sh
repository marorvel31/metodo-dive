#!/usr/bin/env bash
# Reversa de crear_permission_set.sh: quita las asignaciones y borra el permission set DIVE_ReadOnly.
# Uso: bash eliminar_permission_set.sh --profile <perfil-management> --user <username-sso> [--apply]

set -euo pipefail

PS_NAME="DIVE_ReadOnly"
PROFILE=""; USERNAME=""; APPLY=false
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

INSTANCE=$(aws_ sso-admin list-instances --query 'Instances[0].InstanceArn' --output text)
[[ "$INSTANCE" == "None" || -z "$INSTANCE" ]] && { echo "No hay instancia de IAM Identity Center"; exit 1; }
STORE=$(aws_ sso-admin list-instances --query 'Instances[0].IdentityStoreId' --output text)
USER_ID=$(aws_ identitystore list-users --identity-store-id "$STORE" \
  --filters AttributePath=UserName,AttributeValue="$USERNAME" --query 'Users[0].UserId' --output text)
[[ "$USER_ID" == "None" || -z "$USER_ID" ]] && { echo "Usuario $USERNAME no existe"; exit 1; }

PS_ARN=""
for ps in $(aws_ sso-admin list-permission-sets --instance-arn "$INSTANCE" --query 'PermissionSets[]' --output text); do
  name=$(aws_ sso-admin describe-permission-set --instance-arn "$INSTANCE" --permission-set-arn "$ps" \
    --query 'PermissionSet.Name' --output text)
  [[ "$name" == "$PS_NAME" ]] && PS_ARN="$ps"
done
[[ -z "$PS_ARN" ]] && { echo "$PS_NAME no existe, nada que hacer"; exit 0; }

PEDIDOS=()
for acct in $(aws_ sso-admin list-accounts-for-provisioned-permission-set --instance-arn "$INSTANCE" \
    --permission-set-arn "$PS_ARN" --query 'AccountIds[]' --output text); do
  if $APPLY; then
    PEDIDOS+=("$(aws_ sso-admin delete-account-assignment --instance-arn "$INSTANCE" \
      --target-id "$acct" --target-type AWS_ACCOUNT --permission-set-arn "$PS_ARN" \
      --principal-type USER --principal-id "$USER_ID" --query 'AccountAssignmentDeletionStatus.RequestId' --output text)")
  else
    echo "[dry-run] borrar asignación de $USERNAME en $acct"
  fi
done

# Las eliminaciones son asíncronas: borrar el permission set antes de que terminen da ConflictException.
for req in "${PEDIDOS[@]+"${PEDIDOS[@]}"}"; do
  for _ in $(seq 1 30); do
    estado=$(aws_ sso-admin describe-account-assignment-deletion-status --instance-arn "$INSTANCE" \
      --account-assignment-deletion-request-id "$req" --query 'AccountAssignmentDeletionStatus.Status' --output text)
    [[ "$estado" == "IN_PROGRESS" ]] || break
    sleep 2
  done
  [[ "$estado" == "SUCCEEDED" ]] || { echo "La eliminación $req quedó en $estado; revisa la consola" >&2; exit 1; }
done

if $APPLY; then
  quedan=0
  for acct in $(aws_ organizations list-accounts --query "Accounts[?Status=='ACTIVE'].Id" --output text); do
    n=$(aws_ sso-admin list-account-assignments --instance-arn "$INSTANCE" --account-id "$acct" \
      --permission-set-arn "$PS_ARN" --query 'length(AccountAssignments)' --output text)
    quedan=$((quedan + n))
  done
  if [[ "$quedan" != "0" ]]; then
    echo "$PS_NAME sigue asignado a $quedan principal(es) (otros usuarios o grupos): no se borra." >&2
    exit 1
  fi
  aws_ sso-admin delete-permission-set --instance-arn "$INSTANCE" --permission-set-arn "$PS_ARN"
  echo "Borrado $PS_NAME."
else
  echo "[dry-run] borrar permission set $PS_NAME"
fi
