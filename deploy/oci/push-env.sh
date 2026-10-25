#!/usr/bin/env bash
# Assemble the server .env and stream it to /opt/assistant/.env (mode 600).
#
#   push-env.sh <ip> [--owner-ids <id[,id]>]      validate, then push over SSH
#   push-env.sh --check [--owner-ids <id[,id]>]   validate only (no SSH)
#
# Sources (git-ignored, never printed):
#   var/prod.env           BOT_TOKEN, OPENAI_API_KEY, HEARTBEAT_URL (owner);
#                          OWNER_TELEGRAM_IDS (written by `just dev-bot-switch`;
#                          --owner-ids overrides it);
#                          optional: PRIVACY_POLICY_URL, LLM_MONTHLY_BUDGET_USD,
#                          FEATURE_FLAGS, LLM_DEFAULT_MODEL,
#                          LLM_OWNER_REQUEST_EFFORT, LOG_LEVEL, BACKUP_HOUR_UTC
#   var/cloud/b2.env       B2_BUCKET, B2_KEY_ID, B2_APP_KEY (B2_ENDPOINT is
#                          accepted but unused: rclone's native b2 backend
#                          discovers its API endpoint when it authorizes)
#   var/cloud/age-identity.txt   only its "# public key:" line is read
#   var/cloud/prod-generated.env POSTGRES_PASSWORD, generated on first run
#                                and reused afterwards (the database keeps it)
#
# The assembled file exists only in this process's memory and on the server;
# it is never written to the laptop's disk, and only setting NAMES are
# printed (AGENTS §9).
set -euo pipefail

# shellcheck source=deploy/oci/lib.sh
. "$(dirname "$0")/lib.sh"

PROD_ENV="${PROD_ENV:-$WORK/var/prod.env}"
B2_ENV="$CLOUD_DIR/b2.env"
AGE_IDENTITY="$CLOUD_DIR/age-identity.txt"
GENERATED_ENV="$CLOUD_DIR/prod-generated.env"
OPTIONAL_KEYS=(PRIVACY_POLICY_URL LLM_MONTHLY_BUDGET_USD FEATURE_FLAGS LLM_DEFAULT_MODEL
  LLM_OWNER_REQUEST_EFFORT LOG_LEVEL BACKUP_HOUR_UTC)

ip=""
owner_ids=""
check_only=0
write_to=""
while [ $# -gt 0 ]; do
  case "$1" in
    --owner-ids) owner_ids="${2:-}"; shift 2 ;;
    --check) check_only=1; shift ;;
    # Test hook: write the result to a file under var/cloud instead of SSH.
    --write-to) write_to="${2:-}"; shift 2 ;;
    -*) die "unknown option $1" ;;
    *) ip="$1"; shift ;;
  esac
done
if [ "$check_only" -eq 0 ] && [ -z "$write_to" ]; then
  require_ip "$ip"
fi
if [ -n "$write_to" ]; then
  case "$(realpath -m "$write_to")" in
    "$(realpath -m "$CLOUD_DIR")"/*) ;;
    *) die "--write-to must be a path under var/cloud" ;;
  esac
fi

# read_env <file> <KEY>: the value of KEY (last assignment wins), without
# surrounding quotes. The file is parsed, never sourced or printed.
read_env() {
  local file="$1" key="$2"
  [ -r "$file" ] || return 0
  awk -v k="$key" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*(export[[:space:]]+)?/, "", line)
      if (index(line, k "=") != 1) next
      v = substr(line, length(k) + 2)
      sub(/[[:space:]]+$/, "", v)
      if (v ~ /^".*"$/ || v ~ /^\047.*\047$/) v = substr(v, 2, length(v) - 2)
      val = v
    }
    END { printf "%s", val }
  ' "$file"
}

[ -r "$PROD_ENV" ] || die "var/prod.env is missing (owner step A6 in runbooks/provision.md)"
[ -r "$B2_ENV" ] || die "var/cloud/b2.env is missing (owner step A4)"
[ -r "$AGE_IDENTITY" ] || die "var/cloud/age-identity.txt is missing (run: just prod-age-key)"
if [ -z "$owner_ids" ]; then
  owner_ids="$(read_env "$PROD_ENV" OWNER_TELEGRAM_IDS)"
fi
[[ "$owner_ids" =~ ^[0-9]+(,[0-9]+)*$ ]] ||
  die "OWNER_TELEGRAM_IDS (the main account) is missing: run just dev-bot-switch, or pass --owner-ids"

# --- generated values ------------------------------------------------------------
if [ -z "$(read_env "$GENERATED_ENV" POSTGRES_PASSWORD)" ]; then
  (
    umask 077
    # Hex: URL-safe, so it can sit inside DATABASE_URL unescaped.
    printf 'POSTGRES_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
      >> "$GENERATED_ENV"
  )
  own_like_checkout "$GENERATED_ENV"
  log "generated POSTGRES_PASSWORD into var/cloud/prod-generated.env"
fi

declare -A v
v[APP_ENV]=prod
v[LOG_FORMAT]=json
v[LOG_LEVEL]=INFO
v[BOT_TOKEN]="$(read_env "$PROD_ENV" BOT_TOKEN)"
v[OWNER_TELEGRAM_IDS]="$owner_ids"
v[OPENAI_API_KEY]="$(read_env "$PROD_ENV" OPENAI_API_KEY)"
v[HEARTBEAT_URL]="$(read_env "$PROD_ENV" HEARTBEAT_URL)"
v[LLM_DEFAULT_MODEL]=gpt-6-luna
v[BACKUP_HOUR_UTC]=02
v[PRIVACY_POLICY_URL]=""
for key in "${OPTIONAL_KEYS[@]}"; do
  value="$(read_env "$PROD_ENV" "$key")"
  if [ -n "$value" ]; then v[$key]="$value"; fi
done
v[POSTGRES_USER]=assistant
v[POSTGRES_DB]=assistant
v[POSTGRES_PASSWORD]="$(read_env "$GENERATED_ENV" POSTGRES_PASSWORD)"
v[DATABASE_URL]="postgresql+psycopg://${v[POSTGRES_USER]}:${v[POSTGRES_PASSWORD]}@db:5432/${v[POSTGRES_DB]}"
v[DATA_DIR]=/mnt/data
v[BACKUP_STATUS_PATH]=/backups/last_success
v[AGE_RECIPIENT]="$(awk '/^# public key: age1/ { print $4; exit }' "$AGE_IDENTITY")"
bucket="$(read_env "$B2_ENV" B2_BUCKET)"
v[BACKUP_REMOTE]="offsite:$bucket"
v[RCLONE_CONFIG_OFFSITE_TYPE]=b2
v[RCLONE_CONFIG_OFFSITE_ACCOUNT]="$(read_env "$B2_ENV" B2_KEY_ID)"
v[RCLONE_CONFIG_OFFSITE_KEY]="$(read_env "$B2_ENV" B2_APP_KEY)"
# Deleting a file removes it for good instead of leaving a hidden version
# (rclone b2 `hard_delete`): rotated-out backups must not survive off-site.
v[RCLONE_CONFIG_OFFSITE_HARD_DELETE]=true

# --- validation (names only) ------------------------------------------------------
problems=()
need() { # need KEY REGEX DESCRIPTION
  local key="$1" re="$2" what="$3" value="${v[$1]:-}"
  if [ -z "$value" ]; then
    problems+=("$key is empty")
  elif [[ "$value" == *REPLACE* || "$value" == *CHANGE_ME* || "$value" == *"<"* ]]; then
    problems+=("$key still holds a placeholder")
  elif ! [[ "$value" =~ $re ]]; then
    problems+=("$key does not look like $what")
  fi
}
need BOT_TOKEN '^[0-9]+:[A-Za-z0-9_-]{30,}$' "a BotFather token"
need OPENAI_API_KEY '^sk-[A-Za-z0-9_-]{20,}$' "an OpenAI API key"
need HEARTBEAT_URL '^https://[^[:space:]]+$' "an https ping URL"
need OWNER_TELEGRAM_IDS '^[0-9]+(,[0-9]+)*$' "Telegram user ids"
need POSTGRES_PASSWORD '^[0-9a-f]{64}$' "the generated hex password"
need AGE_RECIPIENT '^age1[0-9a-z]{58}$' "an age public key"
need RCLONE_CONFIG_OFFSITE_ACCOUNT '^[0-9A-Za-z]{12,}$' "a B2 application key id"
need RCLONE_CONFIG_OFFSITE_KEY '^[0-9A-Za-z/+]{20,}$' "a B2 application key"
need BACKUP_REMOTE '^offsite:[A-Za-z0-9-]{6,63}$' "offsite:<B2 bucket name>"
if [ -z "${v[PRIVACY_POLICY_URL]}" ]; then
  log "note: PRIVACY_POLICY_URL is empty (set it in var/prod.env; /privacy needs it)"
fi
if [ "${#problems[@]}" -gt 0 ]; then
  for p in "${problems[@]}"; do log "invalid: $p"; done
  die "server .env not pushed"
fi

order=(APP_ENV LOG_LEVEL LOG_FORMAT DATABASE_URL BOT_TOKEN OWNER_TELEGRAM_IDS
  OPENAI_API_KEY LLM_DEFAULT_MODEL LLM_MONTHLY_BUDGET_USD LLM_OWNER_REQUEST_EFFORT
  FEATURE_FLAGS PRIVACY_POLICY_URL HEARTBEAT_URL BACKUP_STATUS_PATH DATA_DIR
  AGE_RECIPIENT BACKUP_REMOTE BACKUP_HOUR_UTC RCLONE_CONFIG_OFFSITE_TYPE
  RCLONE_CONFIG_OFFSITE_ACCOUNT RCLONE_CONFIG_OFFSITE_KEY
  RCLONE_CONFIG_OFFSITE_HARD_DELETE POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB)
render() {
  printf '# Production settings, written by deploy/oci/push-env.sh. Mode 600.\n'
  printf '# Edit through push-env (var/prod.env, var/cloud/*), not by hand.\n'
  local key
  for key in "${order[@]}"; do
    if [ -n "${v[$key]+set}" ]; then printf '%s=%s\n' "$key" "${v[$key]}"; fi
  done
}
present=()
for key in "${order[@]}"; do
  if [ -n "${v[$key]+set}" ]; then present+=("$key"); fi
done
log "valid; will write: ${present[*]}"

if [ "$check_only" -eq 1 ]; then
  log "check only: nothing pushed"
  exit 0
fi
if [ -n "$write_to" ]; then
  (umask 077 && render > "$write_to")
  log "written to $(basename "$write_to") (test hook)"
  exit 0
fi

prepare_ssh
# Stream over SSH stdin into a 600 file, then atomically replace .env.
render | remote "$ip" "set -e; umask 077; test -d $PROD_DIR; cat > $PROD_DIR/.env.new; chmod 600 $PROD_DIR/.env.new; mv $PROD_DIR/.env.new $PROD_DIR/.env"
remote "$ip" "stat -c '%n %a %U' $PROD_DIR/.env" | sed 's/^/server: /' >&2
log "pushed; restart to apply: just deploy $ip"
