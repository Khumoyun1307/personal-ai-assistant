#!/usr/bin/env bash
# Structural settings-push regression; temporary synthetic inputs only, no SSH.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
cleanup() {
  local status=$?
  if [ "$status" -ne 0 ] && [ -f "$tmp/log" ]; then
    printf 'FAIL: push-env smoke; names-only renderer log follows\n' >&2
    cat "$tmp/log" >&2
  fi
  rm -rf -- "$tmp"
  exit "$status"
}
trap cleanup EXIT
mkdir -p "$tmp/cloud" "$tmp/bin"
export WORK="$tmp" CLOUD_DIR="$tmp/cloud" PROD_ENV="$tmp/production-input"
# Exercise missing-password generation without requiring Python on the host.
# The production OCI container provides Python; this fixture returns distinct,
# deterministic hex placeholders and counts calls to check reuse below.
export SYNTHETIC_PASSWORD_COUNTER="$tmp/password-counter"
printf '0\n' > "$SYNTHETIC_PASSWORD_COUNTER"
cat > "$tmp/bin/python3" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ] || [ "$1" != -c ] || [ "$2" != 'import secrets; print(secrets.token_hex(32))' ]; then
  printf 'FAIL: unexpected password generator invocation\n' >&2
  exit 1
fi
[ "${SYNTHETIC_GENERATOR_FAIL:-0}" -eq 0 ] || exit 1
count="$(cat "$SYNTHETIC_PASSWORD_COUNTER")"
count=$((count + 1))
printf '%s\n' "$count" > "$SYNTHETIC_PASSWORD_COUNTER"
printf '%064x\n' "$count"
SH
chmod +x "$tmp/bin/python3"
cat > "$PROD_ENV" <<'ENV'
BOT_TOKEN=123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
OPENAI_API_KEY=sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
HEARTBEAT_URL=https://synthetic.example/ping-private
OWNER_TELEGRAM_IDS=123456789
ENV
cat > "$CLOUD_DIR/b2.env" <<'ENV'
B2_BUCKET=synthetic-bucket
B2_KEY_ID=aaaaaaaaaaaa
B2_APP_KEY=aaaaaaaaaaaaaaaaaaaa
ENV
printf '# public key: age1%s\n' "$(printf 'a%.0s' {1..58})" > "$CLOUD_DIR/age-identity.txt"
printf 'POSTGRES_PASSWORD=%s\n' "$(printf 'a%.0s' {1..64})" > "$CLOUD_DIR/prod-generated.env"
while IFS= read -r key || [ -n "$key" ]; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  # The renderer validates the privacy URL; other tuning is validated by Settings.
  printf '%s=https://synthetic.example/%s\n' "$key" "$key" >> "$PROD_ENV"
done < "$root/deploy/settings-manifest.txt"
PATH="$tmp/bin:$PATH" bash "$root/deploy/oci/push-env.sh" --write-to "$CLOUD_DIR/rendered" > "$tmp/log" 2>&1
[ "$(cat "$SYNTHETIC_PASSWORD_COUNTER")" -eq 3 ]
while IFS= read -r key || [ -n "$key" ]; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  service=app
  [ "$key" != BACKUP_HOUR_UTC ] || service=backup
  [ "$(grep -c "^$key=" "$CLOUD_DIR/rendered.$service")" -eq 1 ]
  grep -Fxq "$key=https://synthetic.example/$key" "$CLOUD_DIR/rendered.$service"
done < "$root/deploy/settings-manifest.txt"
# Exact inventories catch both credential leaks and silently dropped settings.
allowed_app='APP_ENV LOG_FORMAT DATABASE_URL BOT_TOKEN OWNER_TELEGRAM_IDS OPENAI_API_KEY HEARTBEAT_URL BACKUP_STATUS_PATH BACKUP_REMOTE'
while IFS= read -r key || [ -n "$key" ]; do
  [[ -z "$key" || "$key" == \#* || "$key" = BACKUP_HOUR_UTC ]] && continue
  allowed_app+=" $key"
done < "$root/deploy/settings-manifest.txt"
allowed_stack='POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB DATA_DIR'
allowed_migrate='DATABASE_URL'
allowed_backup='PGHOST PGUSER PGPASSWORD PGDATABASE AGE_RECIPIENT BACKUP_HOUR_UTC BACKUP_REMOTE RCLONE_CONFIG_OFFSITE_TYPE RCLONE_CONFIG_OFFSITE_ACCOUNT RCLONE_CONFIG_OFFSITE_KEY RCLONE_CONFIG_OFFSITE_HARD_DELETE'
allowed_roles='DATABASE_URL MIGRATION_DB_PASSWORD APP_DB_PASSWORD BACKUP_DB_PASSWORD'
allowed_restore='PGHOST PGUSER PGPASSWORD PGDATABASE'
for service in stack app migrate backup roles restore; do
  case "$service" in
    stack) allowed="$allowed_stack" ;;
    app) allowed="$allowed_app" ;;
    migrate) allowed="$allowed_migrate" ;;
    backup) allowed="$allowed_backup" ;;
    roles) allowed="$allowed_roles" ;;
    restore) allowed="$allowed_restore" ;;
  esac
  printf '%s\n' "$allowed" | tr ' ' '\n' | sort > "$tmp/expected"
  sed -n 's/^\([A-Z_0-9]*\)=.*/\1/p' "$CLOUD_DIR/rendered.$service" | sort > "$tmp/actual"
  diff -u "$tmp/expected" "$tmp/actual"
  [ "$(stat -c '%a' "$CLOUD_DIR/rendered.$service")" = 600 ]
done
grep -q '^DATABASE_URL=postgresql+psycopg://assistant_app:' "$CLOUD_DIR/rendered.app"
grep -q '^DATABASE_URL=postgresql+psycopg://assistant_migrate:' "$CLOUD_DIR/rendered.migrate"
grep -Fxq 'PGUSER=assistant_backup' "$CLOUD_DIR/rendered.backup"
app_password="$(sed -n 's/^APP_DB_PASSWORD=//p' "$CLOUD_DIR/prod-generated.env")"
migration_password="$(sed -n 's/^MIGRATION_DB_PASSWORD=//p' "$CLOUD_DIR/prod-generated.env")"
backup_password="$(sed -n 's/^BACKUP_DB_PASSWORD=//p' "$CLOUD_DIR/prod-generated.env")"
grep -Fxq "DATABASE_URL=postgresql+psycopg://assistant_app:$app_password@db:5432/assistant" "$CLOUD_DIR/rendered.app"
grep -Fxq "DATABASE_URL=postgresql+psycopg://assistant_migrate:$migration_password@db:5432/assistant" "$CLOUD_DIR/rendered.migrate"
grep -Fxq "PGPASSWORD=$backup_password" "$CLOUD_DIR/rendered.backup"
grep -Fxq "PGPASSWORD=$migration_password" "$CLOUD_DIR/rendered.restore"
[ "$(cut -d= -f2 "$CLOUD_DIR/prod-generated.env" | sort -u | wc -l)" -eq 4 ]
cp "$CLOUD_DIR/prod-generated.env" "$tmp/generated-before"
# An explicit blank clears a default effort; an absent optional stays absent.
printf 'LLM_DRAFT_REPLY_EFFORT=\nLLM_ROUTE_OWNER_REQUEST_MODEL=\n' >> "$PROD_ENV"
sed -i '/^TRIAGE_DEBOUNCE_SECONDS=/d' "$PROD_ENV"
PATH="$tmp/bin:$PATH" bash "$root/deploy/oci/push-env.sh" --write-to "$CLOUD_DIR/rendered" >> "$tmp/log" 2>&1
[ "$(cat "$SYNTHETIC_PASSWORD_COUNTER")" -eq 3 ]
grep -Fxq 'LLM_DRAFT_REPLY_EFFORT=' "$CLOUD_DIR/rendered.app"
grep -Fxq 'LLM_ROUTE_OWNER_REQUEST_MODEL=' "$CLOUD_DIR/rendered.app"
if grep -q '^TRIAGE_DEBOUNCE_SECONDS=' "$CLOUD_DIR/rendered.app"; then
  printf 'FAIL: absent optional setting was rendered\n' >&2
  exit 1
fi
cmp "$CLOUD_DIR/prod-generated.env" "$tmp/generated-before"
# A failed generator must neither append an empty password nor publish files.
cp "$CLOUD_DIR/rendered.app" "$tmp/app-before-failure"
sed -i '/^APP_DB_PASSWORD=/d' "$CLOUD_DIR/prod-generated.env"
if SYNTHETIC_GENERATOR_FAIL=1 PATH="$tmp/bin:$PATH" \
  bash "$root/deploy/oci/push-env.sh" --write-to "$CLOUD_DIR/rendered" >> "$tmp/log" 2>&1; then
  printf 'FAIL: failed password generator was accepted\n' >&2
  exit 1
fi
grep -q 'could not generate APP_DB_PASSWORD' "$tmp/log"
if grep -q '^APP_DB_PASSWORD=' "$CLOUD_DIR/prod-generated.env"; then
  printf 'FAIL: failed generation persisted a password\n' >&2
  exit 1
fi
cmp "$CLOUD_DIR/rendered.app" "$tmp/app-before-failure"
cp "$tmp/generated-before" "$CLOUD_DIR/prod-generated.env"
# Values (including non-secrets) never reach the push log.
if grep -Eq 'synthetic|sk-|age1|postgresql\+psycopg://' "$tmp/log"; then
  printf 'FAIL: a setting value reached the push log\n' >&2
  exit 1
fi
while IFS='=' read -r key value; do
  if grep -Fq "$value" "$tmp/log"; then
    printf 'FAIL: a generated password reached the push log\n' >&2
    exit 1
  fi
done < "$CLOUD_DIR/prod-generated.env"
cmp "$CLOUD_DIR/prod-generated.env" "$tmp/generated-before"
[ "$(stat -c '%a' "$CLOUD_DIR/prod-generated.env")" = 600 ]
printf 'ok: settings manifest survives production rendering; logs contain names only\n'
