#!/usr/bin/env bash
# Método DIVE de punta a punta: comprueba el entorno, corre las 5 fases sobre una corrida nueva y
# abre el dashboard. Cada fase escribe su JSON (ver CONTRATO.md); si una falla, se para ahí.
#
# Uso:
#   bash dive.sh --config ~/clientes/acme/dive.config.yaml
#   bash dive.sh --config ... --sin-doctor        # fase V sin aws-doctor
#   bash dive.sh --config ... --sin-dashboard     # no abre el dashboard al final
#   bash dive.sh --config ... --sin-navegador     # abre el dashboard pero solo imprime la URL
#
# Sin --config usa DIVE_CONFIG, o dive.config.yaml de la raíz del repo, o la única config de DIVE que
# encuentre en tu carpeta personal (si hay varias, las lista).

set -euo pipefail

RAIZ="$(CDPATH= cd "$(dirname "$0")" && pwd)"
export PYTHONUNBUFFERED=1   # la fase 1 va por tee: sin esto su salida aparece toda al final
PY="$RAIZ/.venv/bin/python"
CONFIG="${DIVE_CONFIG:-}"
SIN_DOCTOR=false; SIN_DASHBOARD=false; SIN_NAVEGADOR=false

ayuda() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 && "$2" != --* ]] || { echo "--config necesita una ruta (ver --help)" >&2; exit 2; }
      CONFIG="$2"; shift 2 ;;
    --sin-doctor) SIN_DOCTOR=true; shift ;;
    --sin-dashboard) SIN_DASHBOARD=true; shift ;;
    --sin-navegador) SIN_NAVEGADOR=true; shift ;;
    -h|--help) ayuda; exit 0 ;;
    *) echo "Argumento desconocido: $1 (ver --help)" >&2; exit 2 ;;
  esac
done

falla() { echo "✗ $*" >&2; exit 1; }
paso() { printf '\n▸ %s\n' "$*"; }

# ---------- comprobaciones (nada toca AWS salvo una llamada sts de lectura) ----------

paso "Comprobando el entorno"
[[ -x "$PY" ]] || falla "No existe $PY. Instala el entorno:
    uv venv .venv && uv pip install -p .venv --require-hashes -r requirements.lock"
# Configs de DIVE en la carpeta personal (no en ~/.aws: ahí van los perfiles de AWS, otra cosa).
buscar_configs() {
  # Sin Documents/Desktop/Downloads/Library…: macOS pide permiso al leerlas, y una config no vive ahí.
  find "$HOME" -maxdepth 4 \( -path "$HOME/Library" -o -path "$HOME/.Trash" -o -path "$HOME/Documents" \
    -o -path "$HOME/Desktop" -o -path "$HOME/Downloads" -o -path "$HOME/Pictures" -o -path "$HOME/Movies" \
    -o -path "$HOME/Music" -o -name node_modules -o -name .git -o -name .venv \) -prune \
    -o \( -type f -o -type l \) -name 'dive.config.yaml' -print 2>/dev/null | sort || true
}
sangrar() { printf '%s\n' "$1" | sed 's/^/    /'; }
if [[ -z "$CONFIG" && -f "$RAIZ/dive.config.yaml" ]]; then
  CONFIG="$RAIZ/dive.config.yaml"
elif [[ -z "$CONFIG" ]]; then
  ENCONTRADAS="$(buscar_configs)"
  CUANTAS=0
  [[ -n "$ENCONTRADAS" ]] && CUANTAS="$(printf '%s\n' "$ENCONTRADAS" | wc -l | tr -d ' ')"
  if [[ "$CUANTAS" == 1 ]]; then
    # La config decide output_dir y el perfil de AWS: una encontrada sola se confirma (sin terminal, no se usa).
    if [[ -t 0 && -t 1 ]]; then
      printf '  Sin --config encontré %s. ¿La uso? [S/n] ' "$ENCONTRADAS"
      read -r RESPUESTA || RESPUESTA="n"
      [[ -z "$RESPUESTA" || "$RESPUESTA" =~ ^[SsYy] ]] || falla "Pasa la config con --config <ruta>."
    else
      falla "Sin --config y sin terminal no elijo config por ti. Encontré $ENCONTRADAS: pásala con --config."
    fi
    CONFIG="$ENCONTRADAS"
  elif [[ "$CUANTAS" == 0 ]]; then
    falla "No encuentro ninguna config de DIVE. Copia dive.config.example.yaml (por ejemplo a ~/dive-data/mi-org/dive.config.yaml), ajústala y pasa --config <ruta>."
  else
    LISTA="$(sangrar "$ENCONTRADAS")"
    falla "Hay varias configs de DIVE; elige una con --config:
$LISTA"
  fi
fi
[[ -f "$CONFIG" ]] || falla "No existe la config $CONFIG. Copia dive.config.example.yaml y ajústala, o pasa --config."
# Error típico: pasar el archivo de perfiles de AWS (~/.aws/config, ~/.aws/dive-config) en vez del de DIVE.
# Un YAML de DIVE no tiene secciones [nombre] ni claves aws_*: si las tiene, es un archivo de AWS (y no se
# parsea, porque el error de YAML mostraría la línea, que puede ser una access key).
if grep -qE -- '^[[:space:]]*\[[^]]+\][[:space:]]*$|^[[:space:]]*aws_(access_key_id|secret_access_key|session_token)[[:space:]]*=' "$CONFIG"; then
  SUGERIDAS="$(buscar_configs)"
  MENSAJE="$CONFIG es un archivo de perfiles de AWS, no la config de DIVE (management_profile, output_dir, org_alias...)."
  if [[ -n "$SUGERIDAS" ]]; then
    LISTA="$(sangrar "$SUGERIDAS")"
    MENSAJE="$MENSAJE
  Configs de DIVE encontradas:
$LISTA"
  fi
  falla "$MENSAJE"
fi
CONFIG="$(CDPATH= cd -- "$(dirname -- "$CONFIG")" && pwd)/$(basename -- "$CONFIG")"
export DIVE_CONFIG="$CONFIG"

# Valores del config leídos con el mismo cargador que usan las fases (valida también los IDs de cuenta).
# -I: modo aislado (sin el directorio actual en sys.path): un json.py o yaml.py en la carpeta desde la que se
# corre el script no se importa en lugar del módulo real.
LEIDO="$("$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1])
from common import cargar_config
try:
    c = cargar_config()
except SystemExit:
    raise                      # mensajes propios de cargar_config (ej. IDs de cuenta sin comillas)
except Exception as e:         # error de YAML: solo tipo y línea, nunca el contenido (podría ser un secreto)
    marca = getattr(e, "problem_mark", None)
    sys.exit(f"{type(e).__name__}" + (f" en la línea {marca.line + 1}" if marca else ""))
if not isinstance(c, dict):
    sys.exit("el archivo no es un mapa YAML de DIVE")
print(c.get("access_mode", "sso")); print(c.get("management_profile") or "")' "$RAIZ")" \
  || falla "La config no es válida (ver el mensaje de arriba)."
MODO="$(printf '%s\n' "$LEIDO" | sed -n 1p)"; PERFIL_MGMT="$(printf '%s\n' "$LEIDO" | sed -n 2p)"

if [[ "$MODO" == "sso" ]]; then
  [[ -f "$HOME/.aws/dive-config" ]] || falla "Faltan los perfiles dive-*. Corre: $PY setup/generar_perfiles.py"
  export AWS_CONFIG_FILE="$HOME/.aws/dive-config"
  PERFIL="dive-management"
  LOGIN_CMD=(aws sso login --profile "$PERFIL")   # usa el sso-session de ~/.aws/dive-config (AWS_CONFIG_FILE)
  LOGIN="AWS_CONFIG_FILE=$(printf %q "$AWS_CONFIG_FILE") aws sso login --profile $PERFIL"
else
  [[ -n "$PERFIL_MGMT" ]] || falla "Falta management_profile en la config (necesario con access_mode: $MODO)."
  [[ "$PERFIL_MGMT" =~ ^[A-Za-z0-9_.@+-]+$ ]] || falla "management_profile tiene caracteres no válidos para un perfil de AWS."
  PERFIL="$PERFIL_MGMT"
  LOGIN_CMD=()   # assume_role: las credenciales del perfil de management no siempre son SSO
  LOGIN="revisa las credenciales del perfil $PERFIL (aws configure / aws sso login --profile $(printf %q "$PERFIL"))"
fi
command -v aws >/dev/null || falla "Falta AWS CLI v2 (https://aws.amazon.com/cli/)."
ERR_STS="$(mktemp)"
trap 'rm -f "$ERR_STS" ${SALIDA_F1:+"$SALIDA_F1"}' EXIT
identidad() { aws sts get-caller-identity --profile "$PERFIL" --query Arn --output text 2>"$ERR_STS"; }
causa() { [[ -s "$ERR_STS" ]] && printf '\n  Detalle de AWS: %s' "$(tail -n 1 "$ERR_STS" | cut -c1-200)"; return 0; }
if ! IDENTIDAD="$(identidad)"; then
  # En una terminal se ofrece hacer el login aquí mismo; sin terminal (otro script, CI) solo se indica el comando.
  if [[ ${#LOGIN_CMD[@]} -gt 0 && -t 0 && -t 1 ]]; then
    printf '  La sesión de AWS expiró o no existe. ¿Hago el login ahora (se abre el navegador)? [S/n] '
    read -r RESPUESTA || RESPUESTA="n"
    if [[ -z "$RESPUESTA" || "$RESPUESTA" =~ ^[SsYy] ]]; then
      "${LOGIN_CMD[@]}" || falla "El login no se completó. Puedes hacerlo a mano:
    $LOGIN"
      IDENTIDAD="$(identidad)" || falla "Después del login la sesión sigue sin ser válida. Revisa el perfil $PERFIL.$(causa)"
    else
      falla "Sin sesión no se puede seguir. Haz login y vuelve a correr:
    $LOGIN"
    fi
  else
    falla "La sesión de AWS no es válida o expiró. Haz login y vuelve a correr:
    $LOGIN$(causa)"
  fi
fi
echo "  config:    $CONFIG"
echo "  acceso:    $MODO ($PERFIL) → ${IDENTIDAD%%/*}/…"   # sin el nombre de la sesión ni el email
if ! $SIN_DOCTOR && ! command -v aws-doctor >/dev/null; then
  echo "  aws-doctor no está instalado: la fase V corre sin él (instálalo o usa --sin-doctor para silenciar este aviso)."
  SIN_DOCTOR=true
fi

# ---------- fases ----------

fase() {  # fase <título> <script> [args...]
  local titulo="$1"; shift
  paso "$titulo"
  local inicio=$SECONDS
  "$PY" "$RAIZ/$@" || falla "Falló: $titulo. La corrida queda incompleta en ${CORRIDA:-salida/}.
  Para retomar solo esta fase (sin repetir las anteriores ni pagar de nuevo Cost Explorer):
    DIVE_CONFIG=\"$CONFIG\" $PY $RAIZ/$*"
  echo "  ✓ $titulo ($((SECONDS - inicio)) s)"
}

# La carpeta de la corrida sale de lo que imprime la fase 1 ("→ <carpeta>/discovery.json"), no de ULTIMA:
# así otra corrida empezada en paralelo en el mismo output_dir no se mezcla con esta.
SALIDA_F1="$(mktemp)"   # se borra en el trap de salida (y antes del exec del dashboard)
paso "1/5 Descubrir (Cost Explorer)"
INICIO=$SECONDS
"$PY" "$RAIZ/1-descubrir/descubrir.py" | tee "$SALIDA_F1" || falla "Falló: 1/5 Descubrir. No se creó ninguna corrida completa."
echo "  ✓ 1/5 Descubrir ($((SECONDS - INICIO)) s)"
CORRIDA="$(sed -n 's#^→ \(.*\)/discovery\.json$#\1#p' "$SALIDA_F1" | tail -n 1)"
[[ -n "$CORRIDA" && -f "$CORRIDA/discovery.json" ]] || falla "No encuentro la carpeta de la corrida en la salida de la fase 1."

fase "2/5 Inventariar" 2-inventariar/inventariar.py --discovery "$CORRIDA/discovery.json"
VALIDAR_ARGS=(--inventory "$CORRIDA/inventory.json")
$SIN_DOCTOR && VALIDAR_ARGS+=(--sin-doctor)
fase "3/5 Validar" 3-validar/validar.py "${VALIDAR_ARGS[@]}"
fase "4/5 Esquematizar (diagramas)" 4-esquematizar/esquematizar.py --inventory "$CORRIDA/inventory.json"
fase "5/5 Reporte" 4-esquematizar/reporte.py --inventory "$CORRIDA/inventory.json"

paso "Corrida lista: $CORRIDA"
echo "  findings.md, reporte.html, diagramas/ y los JSON de cada fase."
echo "  Para compartirla: $PY \"$RAIZ/anonimizar/anonimizar.py\" \"$CORRIDA\""

# ---------- dashboard ----------

if $SIN_DASHBOARD; then
  echo "  Dashboard: $PY \"$RAIZ/5-dashboard/dashboard.py\""
  exit 0
fi
DASH_ARGS=()
$SIN_NAVEGADOR && DASH_ARGS+=(--sin-navegador)
paso "Dashboard (Ctrl+C para cerrar)"
rm -f "$ERR_STS" "$SALIDA_F1"   # exec no pasa por el trap de salida: la salida de la fase 1 tiene IDs y costos
exec "$PY" "$RAIZ/5-dashboard/dashboard.py" ${DASH_ARGS[@]+"${DASH_ARGS[@]}"}
