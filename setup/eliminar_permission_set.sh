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
run() { if $APPLY; then "$@"; else echo "[dry-run] $*"; fi; }

INSTANCE=$(aws_ sso-admin list-instances --query 'Instances[0].InstanceArn' --output text)
STORE=$(aws_ sso-admin list-instances --query 'Instances[0].IdentityStoreId' --output text)
USER_ID=$(aws_ identitystore list-users --identity-store-id "$STORE" \
  --filters AttributePath=UserName,AttributeValue="$USERNAME" --query 'Users[0].UserId' --output text)

PS_ARN=""
for ps in $(aws_ sso-admin list-permission-sets --instance-arn "$INSTANCE" --query 'PermissionSets[]' --output text); do
  name=$(aws_ sso-admin describe-permission-set --instance-arn "$INSTANCE" --permission-set-arn "$ps" \
    --query 'PermissionSet.Name' --output text)
  [[ "$name" == "$PS_NAME" ]] && PS_ARN="$ps"
done
[[ -z "$PS_ARN" ]] && { echo "$PS_NAME no existe, nada que hacer"; exit 0; }

for acct in $(aws_ sso-admin list-accounts-for-provisioned-permission-set --instance-arn "$INSTANCE" \
    --permission-set-arn "$PS_ARN" --query 'AccountIds[]' --output text); do
  run aws --profile "$PROFILE" sso-admin delete-account-assignment --instance-arn "$INSTANCE" \
    --target-id "$acct" --target-type AWS_ACCOUNT --permission-set-arn "$PS_ARN" \
    --principal-type USER --principal-id "$USER_ID" --query 'AccountAssignmentDeletionStatus.Status' --output text
done

echo "Espera ~1 min a que terminen las eliminaciones y luego borra el permission set:"
run aws --profile "$PROFILE" sso-admin delete-permission-set --instance-arn "$INSTANCE" --permission-set-arn "$PS_ARN"
