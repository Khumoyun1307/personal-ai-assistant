#!/usr/bin/env bash
# Backup smoke test of the production Compose stack (S02.6; CI job `deploy`).
#
#   bash deploy/smoke/backup-smoke.sh        (or: just smoke-backup)
#
# Brings up db (+ migrate) from deploy/compose.prod.yml under the separate
# project `alviro-smoke` with placeholder secrets and a throwaway age key,
# then checks: backup.sh once produces an encrypted dump in the postgres-owned
# volume; it decrypts and pg_restores into a scratch database with the same
# tables; a failing pg_dump records nothing; off-site sync mirrors rotation
# (deletes included), honours the --max-delete guard, and an upload failure
# leaves no upload marker. Everything is removed afterwards (down -v).
# Nothing here is real data; the only "secrets" are generated placeholders.
set -euo pipefail

# Git Bash would rewrite container paths such as /backups into C:/...
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
# Shell variables win over --env-file in Compose interpolation; the smoke
# stack must see only its own placeholder file (CI sets some of these).
unset POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB DATABASE_URL DATA_DIR \
  AGE_RECIPIENT BACKUP_REMOTE BACKUP_HOUR_UTC

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
project="${SMOKE_PROJECT:-alviro-smoke}"
tmp="$(mktemp -d)"
host_path() { if command -v cygpath > /dev/null; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
SMOKE_ENV_FILE="$(host_path "$tmp/smoke.env")"
export SMOKE_ENV_FILE

dc() {
  docker compose -p "$project" --env-file "$SMOKE_ENV_FILE" \
    -f deploy/compose.prod.yml -f deploy/smoke/compose.smoke.yml "$@"
}
cleanup() {
  dc down -v --remove-orphans > /dev/null 2>&1 || true
  rm -rf -- "$tmp"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'SMOKE FAIL: %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok: %s\n' "$*"; }
# bk <shell snippet>: run inside a one-off backup container (as postgres).
bk() { dc run --rm -T --no-deps --entrypoint bash backup -c "$1" | tr -d '\r'; }
psql_db() { dc exec -T db psql -U assistant -d "$1" -tAc "$2" | tr -d '\r[:space:]'; }

step "build images"
docker build -q -t assistant-backup:prod deploy/backup > /dev/null
docker run --rm --entrypoint age-keygen assistant-backup:prod 2> /dev/null > "$tmp/identity.txt"
recipient="$(awk '/^# public key: age1/ { print $4; exit }' "$tmp/identity.txt")"
[[ "$recipient" == age1* ]] || fail "could not create a throwaway age key"
password="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
cat > "$tmp/smoke.env" << EOF
APP_ENV=prod
LOG_FORMAT=json
BOT_TOKEN=123456789:AA-SMOKE-PLACEHOLDER-NOT-A-REAL-TOKEN
OWNER_TELEGRAM_IDS=7000000001
OPENAI_API_KEY=sk-smoke-placeholder-not-a-real-key
POSTGRES_USER=assistant
POSTGRES_PASSWORD=$password
POSTGRES_DB=assistant
DATABASE_URL=postgresql+psycopg://assistant:$password@db:5432/assistant
DATA_DIR=/smoke-unused
AGE_RECIPIENT=$recipient
BACKUP_STATUS_PATH=/backups/last_success
BACKUP_REMOTE=
EOF
dc build migrate backup > /dev/null
ok "app (prod target) and backup images built"

step "database and migrations"
dc up -d --wait db > /dev/null
dc run --rm -T migrate > /dev/null
tables_sql="SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')"
source_tables="$(psql_db assistant "$tables_sql")"
[ "$source_tables" -gt 10 ] || fail "only $source_tables tables after migrate"
ok "migrated: $source_tables tables"

step "backup.sh once"
dc run --rm -T backup once
owner="$(bk 'stat -c %U /backups')"
[ "$owner" = "postgres" ] || fail "/backups is owned by $owner, expected postgres"
dump="$(bk 'ls -1 /backups/daily' | grep -E '^[0-9]{8}T[0-9]{6}Z\.dump\.age$' | tail -n 1)"
[ -n "$dump" ] || fail "no dump in /backups/daily"
bk 'test -s /backups/last_success && ! test -e /backups/last_upload_success' \
  || fail "markers wrong after a local-only backup"
bk "head -c 22 /backups/daily/$dump" | grep -q '^age-encryption.org/v1' || fail "dump is not age-encrypted"
ok "encrypted dump $dump in a postgres-owned volume; last_success written, no upload marker"

step "decrypt + pg_restore into a scratch database"
dc exec -T db createdb -U assistant restore_smoke
dc run --rm -T --no-deps --entrypoint restore.sh backup "/backups/daily/$dump" restore_smoke < "$tmp/identity.txt"
restored_tables="$(psql_db restore_smoke "$tables_sql")"
[ "$restored_tables" = "$source_tables" ] || fail "restored $restored_tables tables, expected $source_tables"
[ "$(psql_db restore_smoke "SELECT count(*) FROM app.system_state WHERE key = 'restore_pending'")" = "1" ] \
  || fail "restore_pending not set"
[ "$(psql_db restore_smoke "SELECT count(*) FROM app.alembic_version")" = "1" ] \
  || fail "alembic_version not restored"
ok "restored $restored_tables tables; restore_pending set; identity only ever on stdin"

step "a failing pg_dump records nothing"
before="$(bk 'cat /backups/last_success; ls -1 /backups/daily | wc -l')"
rc=0
dc run --rm -T --no-deps -e PGDATABASE=does_not_exist backup once > /dev/null 2>&1 || rc=$?
[ "$rc" -eq 1 ] || fail "failed dump exited $rc, expected 1"
after="$(bk 'cat /backups/last_success; ls -1 /backups/daily | wc -l')"
[ "$before" = "$after" ] || fail "failed dump changed last_success or the daily directory"
bk '! ls /backups/daily/*.part 2>/dev/null' || fail ".part left behind"
ok "exit 1, no .part, last_success and daily/ unchanged"

step "off-site sync mirrors rotation (local rclone remote)"
remote_env=(-e RCLONE_CONFIG_SMOKE_TYPE=local -e BACKUP_REMOTE=smoke:/backups/remote)
# shellcheck disable=SC2016  # expanded by the container's shell
bk 'for d in 20200101 20200102 20200103; do head -c 2048 /dev/urandom > /backups/daily/${d}T000000Z.dump.age; done
    mkdir -p /backups/remote/daily && head -c 2048 /dev/urandom > /backups/remote/daily/20190101T000000Z.dump.age'
dc run --rm -T --no-deps "${remote_env[@]}" -e KEEP_DAILY=2 backup once
local_list="$(bk 'ls -1 /backups/daily')"
remote_list="$(bk 'ls -1 /backups/remote/daily')"
[ "$(printf '%s\n' "$local_list" | wc -l)" -eq 2 ] || fail "rotation kept $(printf '%s\n' "$local_list" | wc -l) daily dumps, expected 2"
[ "$local_list" = "$remote_list" ] || fail "remote daily/ does not mirror local daily/"
bk 'test -s /backups/last_upload_success' || fail "no upload marker after a successful sync"
ok "local keeps 2, remote mirrors them (stale remote file deleted), upload marker written"

step "delete guard (a rebuilt host must not wipe the off-site history)"
# shellcheck disable=SC2016  # expanded by the container's shell
bk 'rm -f /backups/last_upload_success
    for n in 1 2 3 4 5 6 7 8 9; do head -c 2048 /dev/urandom > /backups/remote/daily/2019010${n}T120000Z.dump.age; done'
rc=0
dc run --rm -T --no-deps "${remote_env[@]}" backup upload > /dev/null 2>&1 || rc=$?
[ "$rc" -ne 0 ] || fail "a sync needing 9 deletes succeeded"
bk '! test -e /backups/last_upload_success' || fail "upload marker written despite the refused sync"
[ "$(bk 'ls -1 /backups/remote/daily | wc -l')" -eq 11 ] || fail "the refused sync deleted remote files"
ok "sync refused (exit $rc), nothing deleted off-site, no upload marker"

step "an upload failure keeps the dump but writes no upload marker"
rc=0
dc run --rm -T --no-deps -e BACKUP_REMOTE=nosuchremote:bucket backup once > /dev/null 2>&1 || rc=$?
[ "$rc" -eq 3 ] || fail "upload failure exited $rc, expected 3"
bk '! test -e /backups/last_upload_success' || fail "upload marker written despite the failure"
ok "exit 3, dump recorded, no upload marker"

printf '\nSMOKE OK\n'
