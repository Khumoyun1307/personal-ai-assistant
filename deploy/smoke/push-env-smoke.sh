#!/usr/bin/env bash
# Structural settings-push regression; temporary synthetic inputs only, no SSH.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
mkdir -p "$tmp/cloud"
export WORK="$tmp" CLOUD_DIR="$tmp/cloud" PROD_ENV="$tmp/production-input"
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
bash "$root/deploy/oci/push-env.sh" --write-to "$CLOUD_DIR/rendered" > "$tmp/log" 2>&1
while IFS= read -r key || [ -n "$key" ]; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  [ "$(grep -c "^$key=" "$CLOUD_DIR/rendered")" -eq 1 ]
  grep -Fxq "$key=https://synthetic.example/$key" "$CLOUD_DIR/rendered"
done < "$root/deploy/settings-manifest.txt"
# An explicit blank clears a default effort; an absent optional stays absent.
printf 'LLM_DRAFT_REPLY_EFFORT=\nLLM_ROUTE_OWNER_REQUEST_MODEL=\n' >> "$PROD_ENV"
sed -i '/^TRIAGE_DEBOUNCE_SECONDS=/d' "$PROD_ENV"
bash "$root/deploy/oci/push-env.sh" --write-to "$CLOUD_DIR/rendered" >> "$tmp/log" 2>&1
grep -Fxq 'LLM_DRAFT_REPLY_EFFORT=' "$CLOUD_DIR/rendered"
grep -Fxq 'LLM_ROUTE_OWNER_REQUEST_MODEL=' "$CLOUD_DIR/rendered"
if grep -q '^TRIAGE_DEBOUNCE_SECONDS=' "$CLOUD_DIR/rendered"; then
  printf 'FAIL: absent optional setting was rendered\n' >&2
  exit 1
fi
# Values (including non-secrets) never reach the push log.
if grep -Eq 'synthetic|sk-|age1|postgresql\+psycopg://' "$tmp/log"; then
  printf 'FAIL: a setting value reached the push log\n' >&2
  exit 1
fi
[ "$(stat -c '%a' "$CLOUD_DIR/rendered")" = 600 ]
printf 'ok: settings manifest survives production rendering; logs contain names only\n'
