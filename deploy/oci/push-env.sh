#!/usr/bin/env bash
# Assemble separate server environment files (all mode 600).
#
#   push-env.sh <ip> [--owner-ids <id[,id]>]      validate, then push over SSH
#   push-env.sh --check [--owner-ids <id[,id]>]   validate only (no SSH)
#
# Sources (git-ignored, never printed):
#   var/prod.env           BOT_TOKEN, OPENAI_API_KEY, HEARTBEAT_URL (owner);
#                          OWNER_TELEGRAM_IDS (written by `just dev-bot-switch`;
#                          --owner-ids overrides it);
#                          optional: deploy/settings-manifest.txt (non-secret
#                          tuning; explicit empty values are preserved)
#   var/cloud/b2.env       B2_BUCKET, B2_KEY_ID, B2_APP_KEY (B2_ENDPOINT is
#                          accepted but unused: rclone's native b2 backend
#                          discovers its API endpoint when it authorizes)
#   var/cloud/age-identity.txt   only its "# public key:" line is read; once
#                                the key is off the laptop (A8), the public
#                                key is read from the server's .env.backup
#                                (legacy .env on the first upgrade)
#   var/cloud/prod-generated.env POSTGRES_PASSWORD, MIGRATION_DB_PASSWORD,
#                                APP_DB_PASSWORD, BACKUP_DB_PASSWORD; generated
#                                once and reused (the database keeps them)
#
# Assembled files exist only in this process's memory and on the server;
# it is never written to the laptop's disk, and only setting NAMES are
# printed (AGENTS §9).
set -euo pipefail

# shellcheck source=deploy/oci/lib.sh
. "$(dirname "$0")/lib.sh"

PROD_ENV="${PROD_ENV:-$WORK/var/prod.env}"
B2_ENV="$CLOUD_DIR/b2.env"
AGE_IDENTITY="$CLOUD_DIR/age-identity.txt"
GENERATED_ENV="$CLOUD_DIR/prod-generated.env"
# Resolve from the script, not WORK: tests use an isolated WORK for inputs.
manifest="$(dirname "$0")/../settings-manifest.txt"
[ -r "$manifest" ] || die "deploy/settings-manifest.txt is missing"
OPTIONAL_KEYS=()
while IFS= read -r key || [ -n "$key" ]; do
  key="${key%$'\r'}"
  [[ -z "$key" || "$key" == \#* ]] && continue
  [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "invalid setting name in settings manifest"
  [[ " ${OPTIONAL_KEYS[*]} " != *" $key "* ]] || die "duplicate setting name in settings manifest"
  OPTIONAL_KEYS+=("$key")
done < "$manifest"
[ "${#OPTIONAL_KEYS[@]}" -gt 0 ] || die "settings manifest is empty"

ip=""
owner_ids=""
check_only=0
write_to=""
while [ $# -gt 0 ]; do
  case "$1" in
    --owner-ids) [ $# -ge 2 ] || die "--owner-ids needs a value"; owner_ids="$2"; shift 2 ;;
    --check) check_only=1; shift ;;
    # Test hook: output-prefix for service files under var/cloud, no SSH.
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

# Presence is separate from value: an explicit empty effort means no effort.
has_env() {
  awk -v k="$2" '
    /^[[:space:]]*#/ { next }
    {
      sub(/^[[:space:]]*(export[[:space:]]+)?/, "")
      if (index($0, k "=") == 1) found = 1
    }
    END { exit !found }
  ' "$1"
}

[ -r "$PROD_ENV" ] || die "var/prod.env is missing (owner step A6 in runbooks/provision.md)"
[ -r "$B2_ENV" ] || die "var/cloud/b2.env is missing (owner step A4)"
# After A8 the identity is off the laptop: the recipient (a public key) is then
# read back from the server's backup settings, which needs the server ip.
if [ ! -r "$AGE_IDENTITY" ] && [ -z "$ip" ]; then
  die "var/cloud/age-identity.txt is missing: pass the server ip to reuse its AGE_RECIPIENT (or run: just prod-age-key)"
fi
if [ -z "$owner_ids" ]; then
  owner_ids="$(read_env "$PROD_ENV" OWNER_TELEGRAM_IDS)"
fi
[[ "$owner_ids" =~ ^[0-9]+(,[0-9]+)*$ ]] ||
  die "OWNER_TELEGRAM_IDS (the main account) is missing: run just dev-bot-switch, or pass --owner-ids"

# --- generated values ------------------------------------------------------------
for key in POSTGRES_PASSWORD MIGRATION_DB_PASSWORD APP_DB_PASSWORD BACKUP_DB_PASSWORD; do
  if [ -z "$(read_env "$GENERATED_ENV" "$key")" ]; then
    # Capture separately: printf would otherwise mask a failed generator and
    # persist an empty password, leaving only a later validation error.
    password="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" ||
      die "could not generate $key (python3 is required)"
    [[ "$password" =~ ^[0-9a-f]{64}$ ]] || die "invalid generated $key"
    (
      umask 077
      # Hex: URL-safe, so it can sit inside DATABASE_URL unescaped.
      printf '%s=%s\n' "$key" "$password" >> "$GENERATED_ENV"
    )
    own_like_checkout "$GENERATED_ENV"
    log "generated $key into var/cloud/prod-generated.env"
  fi
done
chmod 600 "$GENERATED_ENV"

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
# The published policy page (public; docs/legal/privacy-policy.md, edited in
# place for each version). var/prod.env may override it.
v[PRIVACY_POLICY_URL]="https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19"
for key in "${OPTIONAL_KEYS[@]}"; do
  if has_env "$PROD_ENV" "$key"; then
    v[$key]="$(read_env "$PROD_ENV" "$key")"
  fi
done
v[POSTGRES_USER]=assistant
v[POSTGRES_DB]=assistant
v[POSTGRES_PASSWORD]="$(read_env "$GENERATED_ENV" POSTGRES_PASSWORD)"
for key in MIGRATION_DB_PASSWORD APP_DB_PASSWORD BACKUP_DB_PASSWORD; do
  v[$key]="$(read_env "$GENERATED_ENV" "$key")"
done
v[PGHOST]=db
v[PGDATABASE]="${v[POSTGRES_DB]}"
v[DATA_DIR]=/mnt/data
v[BACKUP_STATUS_PATH]=/backups/last_success
if [ -r "$AGE_IDENTITY" ]; then
  v[AGE_RECIPIENT]="$(awk '/^# public key: age1/ { print $4; exit }' "$AGE_IDENTITY")"
else
  prepare_ssh
  v[AGE_RECIPIENT]="$(remote "$ip" "sed -n 's/^AGE_RECIPIENT=//p' $PROD_DIR/.env.backup 2>/dev/null || sed -n 's/^AGE_RECIPIENT=//p' $PROD_DIR/.env" | head -n 1)"
fi
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
for key in MIGRATION_DB_PASSWORD APP_DB_PASSWORD BACKUP_DB_PASSWORD; do
  need "$key" '^[0-9a-f]{64}$' "the generated hex password"
done
need AGE_RECIPIENT '^age1[0-9a-z]{58}$' "an age public key"
need RCLONE_CONFIG_OFFSITE_ACCOUNT '^[0-9A-Za-z]{12,}$' "a B2 application key id"
need RCLONE_CONFIG_OFFSITE_KEY '^[0-9A-Za-z/+]{20,}$' "a B2 application key"
need BACKUP_REMOTE '^offsite:[A-Za-z0-9-]{6,63}$' "offsite:<B2 bucket name>"
need PRIVACY_POLICY_URL '^https://[^[:space:]]+$' "an https URL"
if [ "${#problems[@]}" -gt 0 ]; then
  for p in "${problems[@]}"; do log "invalid: $p"; done
  die "server environment files not pushed"
fi

# Exact service inventories: never pass the combined settings into a service.
app_keys=(APP_ENV LOG_FORMAT DATABASE_URL BOT_TOKEN OWNER_TELEGRAM_IDS OPENAI_API_KEY)
for key in "${OPTIONAL_KEYS[@]}"; do
  [ "$key" = BACKUP_HOUR_UTC ] || app_keys+=("$key")
done
app_keys+=(HEARTBEAT_URL BACKUP_STATUS_PATH BACKUP_REMOTE)
stack_keys=(POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB DATA_DIR)
migrate_keys=(DATABASE_URL)
backup_keys=(PGHOST PGUSER PGPASSWORD PGDATABASE AGE_RECIPIENT BACKUP_HOUR_UTC
  BACKUP_REMOTE RCLONE_CONFIG_OFFSITE_TYPE RCLONE_CONFIG_OFFSITE_ACCOUNT
  RCLONE_CONFIG_OFFSITE_KEY RCLONE_CONFIG_OFFSITE_HARD_DELETE)
roles_keys=(DATABASE_URL MIGRATION_DB_PASSWORD APP_DB_PASSWORD BACKUP_DB_PASSWORD)
restore_keys=(PGHOST PGUSER PGPASSWORD PGDATABASE)
select_keys() {
  case "$1" in
    stack) order=("${stack_keys[@]}") ;;
    app) order=("${app_keys[@]}") ;;
    migrate) order=("${migrate_keys[@]}") ;;
    backup) order=("${backup_keys[@]}") ;;
    roles) order=("${roles_keys[@]}") ;;
    restore) order=("${restore_keys[@]}") ;;
  esac
}
render() {
  local service="$1" key value
  local -a order
  select_keys "$service"
  printf '# Production settings, written by deploy/oci/push-env.sh. Mode 600.\n'
  printf '# Edit through push-env (var/prod.env, var/cloud/*), not by hand.\n'
  for key in "${order[@]}"; do
    value="${v[$key]-}"
    case "$service:$key" in
      app:DATABASE_URL) value="postgresql+psycopg://assistant_app:${v[APP_DB_PASSWORD]}@db:5432/${v[POSTGRES_DB]}" ;;
      migrate:DATABASE_URL) value="postgresql+psycopg://assistant_migrate:${v[MIGRATION_DB_PASSWORD]}@db:5432/${v[POSTGRES_DB]}" ;;
      roles:DATABASE_URL) value="postgresql+psycopg://${v[POSTGRES_USER]}:${v[POSTGRES_PASSWORD]}@db:5432/${v[POSTGRES_DB]}" ;;
      backup:PGUSER) value=assistant_backup ;;
      backup:PGPASSWORD) value="${v[BACKUP_DB_PASSWORD]}" ;;
      restore:PGUSER) value=assistant_migrate ;;
      restore:PGPASSWORD) value="${v[MIGRATION_DB_PASSWORD]}" ;;
      app:BACKUP_REMOTE) value=enabled ;;
      *) [ -n "${v[$key]+set}" ] || continue ;;
    esac
    printf '%s=%s\n' "$key" "$value"
  done
}
for service in stack app migrate backup roles restore; do
  select_keys "$service"
  log "valid $service keys: ${order[*]}"
done

if [ "$check_only" -eq 1 ]; then
  log "check only: nothing pushed"
  exit 0
fi
if [ -n "$write_to" ]; then
  for service in stack app migrate backup roles restore; do
    target="$write_to.$service"
    (umask 077 && render "$service" > "$target")
    chmod 600 "$target"
  done
  log "written service files (test hook)"
  exit 0
fi

[ -n "$SSH_KEY_COPY" ] || prepare_ssh
# Stage every file before publishing any. Do not restart services during push.
remote "$ip" "set -e; umask 077; test -d $PROD_DIR; touch $PROD_DIR/.env.pending; chmod 600 $PROD_DIR/.env.pending"
for service in stack app migrate backup roles restore; do
  filename=".env.$service"
  [ "$service" != stack ] || filename=.env
  render "$service" | remote "$ip" "set -e; umask 077; test -d $PROD_DIR; cat > $PROD_DIR/$filename.new; chmod 600 $PROD_DIR/$filename.new"
done
remote "$ip" "set -e; cd $PROD_DIR; for name in .env .env.app .env.migrate .env.backup .env.roles .env.restore; do mv \"\$name.new\" \"\$name\"; done; rm .env.pending"
log "pushed; restart to apply: just deploy $ip"
