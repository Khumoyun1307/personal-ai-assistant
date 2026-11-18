#!/usr/bin/env bash
# A4 restore and privacy-reconciliation rehearsal (stabilization PLAN, A4).
#
#   bash deploy/smoke/restore-rehearsal.sh      (or: just rehearse-a4)
#
# The production Compose stack under a separate project with placeholder
# secrets, a throwaway age key and a local rclone remote standing in for B2,
# like backup-smoke.sh. Synthetic owner state (deploy/smoke/rehearsal_state.py)
# is backed up; owner decisions and a completed send happen after the dump;
# then the database is replaced with the dump using the same commands, in the
# same order, as `deploy/server.sh restore`, followed by post-restore and:
#   1. journal replay  — final journal upload acknowledged, replay opens the gate
#   2. stale tail      — final head upload fails, replay refuses, complete purge
# Each check is timed; REHEARSAL_RESULTS (if set) receives content-free TSV rows
# `scenario  check  PASS|FAIL  milliseconds`. The app process (poller, workers)
# never starts: no Telegram or provider call is possible. Everything is
# removed afterwards (down -v, images).
set -euo pipefail

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
unset POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB DATABASE_URL DATA_DIR \
  AGE_RECIPIENT BACKUP_REMOTE BACKUP_HOUR_UTC BACKUP_TEST_NOW ASSISTANT_IMAGE BACKUP_IMAGE

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"
project="${REHEARSAL_PROJECT:-alviro-restore-rehearsal}"
results="${REHEARSAL_RESULTS:-/dev/null}"
tmp="$(mktemp -d)"
host_path() { if command -v cygpath > /dev/null; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
SMOKE_ENV_FILE="$(host_path "$tmp/smoke.env")"
export SMOKE_ENV_FILE
app_image="assistant:$project"
backup_image="assistant-backup:$project"

dc() {
  docker compose -p "$project" --env-file "$SMOKE_ENV_FILE" \
    -f deploy/compose.prod.yml -f deploy/smoke/compose.smoke.yml "$@"
}
cleanup() {
  dc down -v --remove-orphans > /dev/null 2>&1 || true
  docker image rm "$app_image" "$backup_image" > /dev/null 2>&1 || true
  rm -rf -- "$tmp"
}
trap cleanup EXIT

failures=0
ms() { date +%s%3N; }
step() { printf '\n== %s\n' "$*"; }
fail() { printf 'REHEARSAL FAIL: %s\n' "$*" >&2; exit 1; }
# record SCENARIO CHECK PASS|FAIL START_MS — one content-free measurement row.
record() {
  local elapsed=$(($(ms) - $4))
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$elapsed" >> "$results"
  printf 'measured: %s / %s: %s (%s ms)\n' "$1" "$2" "$3" "$elapsed"
  if [ "$3" != PASS ]; then failures=$((failures + 1)); fi
}
# expect SCENARIO CHECK START_MS COMMAND...: record PASS iff COMMAND succeeds.
expect() {
  local scenario="$1" check="$2" started="$3"
  shift 3
  if "$@"; then record "$scenario" "$check" PASS "$started"; else record "$scenario" "$check" FAIL "$started"; fi
}
migrate_pw="$(printf 'a%.0s' {1..64})"
bk() { dc run --rm -T --no-deps --entrypoint bash backup -c "$1" | tr -d '\r'; }
state() {
  dc run --rm --no-deps -T -v "$(host_path "$root/deploy/smoke"):/rehearsal:ro" \
    --entrypoint python app /rehearsal/rehearsal_state.py "$@" | tr -d '\r'
}
privacy_status() { dc run --rm --no-deps -T app python -m assistant.privacy.recovery_cli status | tr -d '\r'; }
gate_closed() { privacy_status | grep -qx privacy_reconciliation_required; }
gate_open() { ! gate_closed; }
# journal_ship [docker run options]: a forced shipping attempt, as server.sh does.
journal_ship() { dc run --rm --no-deps -T "$@" --entrypoint privacy-journal.sh backup ship force; }
journal_status() { bk 'cat /backups/journal_status'; }
backup_once() {
  dc run --rm -T --no-deps backup once > /dev/null && bk 'cat /backups/last_success_file'
}
stamp_iso() { local s="${1%.dump.age}"; printf '%s-%s-%sT%s:%s:%sZ' "${s:0:4}" "${s:4:2}" "${s:6:2}" "${s:9:2}" "${s:11:2}" "${s:13:2}"; }

# The source side of `deploy/server.sh restore`: the app is already stopped
# (it never runs here). Barrier + full-precision cutoff, then the final upload.
cutoff() {
  local value
  # shellcheck disable=SC2016  # expanded inside the db container
  value="$(dc exec -T db sh -c \
    'exec psql -XqAt -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' << 'SQL'
BEGIN;
DO $$ BEGIN
  IF to_regclass('app.privacy_journal_heads') IS NOT NULL THEN
    EXECUTE 'LOCK TABLE app.privacy_journal_heads IN SHARE MODE';
  END IF;
END $$;
SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
COMMIT;
SQL
  )"
  value="${value//$'\r'/}"
  [ "${#value}" -eq 27 ] || fail "recovery cutoff unavailable"
  printf '%s\n' "$value"
}
# The replacement side of `deploy/server.sh restore` (sidecar stopped first).
replace_database() {
  local file="$1" recovery_cutoff="$2"
  # shellcheck disable=SC2016  # expanded inside the db container
  dc exec -T db sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O assistant_migrate "$POSTGRES_DB"'
  # shellcheck disable=SC2016  # expanded inside the restore container
  dc run --rm --no-deps -T -e PGUSER=assistant_migrate -e "PGPASSWORD=$migrate_pw" \
    -e "RESTORE_RECOVERY_CUTOFF=$recovery_cutoff" --entrypoint sh restore \
    -c 'exec restore.sh "$1" "$PGDATABASE"' sh "/backups/daily/$file" < "$tmp/identity.txt" > /dev/null
  dc run --rm --no-deps -T db-roles python -m assistant.db.production_roles setup > /dev/null
}
post_restore() { dc run --rm --no-deps -T app python -m assistant post-restore --backup-taken-at "$1" > /dev/null; }
replay() {
  dc run --rm --no-deps -T --entrypoint privacy-journal.sh backup fetch < "$tmp/identity.txt" \
    | dc run --rm --no-deps -T app python -m assistant.privacy.recovery_cli replay 2>&1 | tr -d '\r'
}
restore_verify() {
  dc run --rm --no-deps -T -v "$(host_path "$root/tools/acceptance"):/app/tools/acceptance:ro" \
    app python -m tools.acceptance.restore_verify | tr -d '\r' | grep -x 'restore_verification=PASS' > /dev/null
}

step "build images, throwaway key, placeholder settings"
started="$(ms)"
docker build -q -t "$backup_image" deploy/backup > /dev/null
docker run --rm --entrypoint age-keygen "$backup_image" 2> /dev/null > "$tmp/identity.txt"
recipient="$(awk '/^# public key: age1/ { print $4; exit }' "$tmp/identity.txt")"
[[ "$recipient" == age1* ]] || fail "could not create a throwaway age key"
password="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
cat > "$tmp/smoke.env" << EOF
APP_ENV=prod
LOG_FORMAT=json
BOT_TOKEN=123456789:AA-REHEARSAL-PLACEHOLDER-NOT-A-REAL-TOKEN
OWNER_TELEGRAM_IDS=7000000001,7000000002
OPENAI_API_KEY=sk-rehearsal-placeholder-not-a-real-key
POSTGRES_USER=assistant
POSTGRES_PASSWORD=$password
POSTGRES_DB=assistant
DATABASE_URL=postgresql+psycopg://assistant:$password@db:5432/assistant
MIGRATION_DB_PASSWORD=$migrate_pw
APP_DB_PASSWORD=$(printf 'b%.0s' {1..64})
BACKUP_DB_PASSWORD=$(printf 'c%.0s' {1..64})
PGHOST=db
PGUSER=assistant_backup
PGPASSWORD=$(printf 'c%.0s' {1..64})
PGDATABASE=assistant
DATA_DIR=/rehearsal-unused
AGE_RECIPIENT=$recipient
BACKUP_STATUS_PATH=/backups/last_success
BACKUP_REMOTE=rehearsal:/backups/remote
RCLONE_CONFIG_REHEARSAL_TYPE=local
ASSISTANT_IMAGE=$app_image
BACKUP_IMAGE=$backup_image
EOF
dc build migrate backup > /dev/null
dc up -d --wait db > /dev/null
dc run --rm --no-deps -T db-roles python -m assistant.db.production_roles setup > /dev/null
dc run --rm -T -e "DATABASE_URL=postgresql+psycopg://assistant_migrate:$migrate_pw@db:5432/assistant" migrate > /dev/null
dc run --rm --no-deps -T db-roles > /dev/null
record setup "stack built and migrated" PASS "$started"

# ---------------------------------------------------------------- scenario 1
scenario="restore+journal replay"
step "$scenario: synthetic state, backup, journal acknowledged"
state seed --user 7000000001
started="$(ms)"
dump="$(backup_once)"
expect "$scenario" "backup with off-site sync" "$started" test -n "$dump"
started="$(ms)"
journal_ship > /dev/null
expect "$scenario" "journal shipped and acknowledged" "$started" test "$(journal_status)" = acknowledged

step "$scenario: after the dump — managed consent revoked, chat deleted, card approved and sent"
state after-backup --user 7000000001
failure_at="$(ms)"
# Data written after the dump and lost by a restore (journalled privacy
# decisions excepted). Production bound: the daily schedule plus catch-up.
record "$scenario" "RPO dump-to-failure (local)" PASS "$(($(date -u -d "$(stamp_iso "$dump")" +%s) * 1000))"

step "$scenario: server.sh restore sequence"
started="$(ms)"
recovery_cutoff="$(cutoff)"
journal_ship > /dev/null
expect "$scenario" "final journal upload before replacement" "$started" test "$(journal_status)" = acknowledged
started="$(ms)"
replace_database "$dump" "$recovery_cutoff"
expect "$scenario" "database replaced from dump; gate closed" "$started" gate_closed
started="$(ms)"
post_restore "$(stamp_iso "$dump")"
expect "$scenario" "post-restore done; gate still closed" "$started" gate_closed
started="$(ms)"
out="$(replay)"
printf '%s\n' "$out"
expect "$scenario" "journal replay opens the gate" "$started" gate_open
started="$(ms)"
expect "$scenario" "restored-state invariants" "$started" state check --user 7000000001 --mode replay
started="$(ms)"
expect "$scenario" "restore_verify gate (pending actions/jobs, notice)" "$started" restore_verify
record "$scenario" "RTO failure-to-verified (local, synthetic size)" PASS "$failure_at"
started="$(ms)"
out="$(replay)"
expect "$scenario" "repeated replay is a no-op" "$started" grep -q privacy_replay_not_required <<< "$out"
started="$(ms)"
journal_ship > /dev/null
expect "$scenario" "journal shipping resumes after replay" "$started" test "$(journal_status)" = acknowledged

# ---------------------------------------------------------------- scenario 2
scenario="stale journal tail+complete purge"
step "$scenario: second account, backup, decisions after the dump"
state seed --user 7000000002
started="$(ms)"
dump="$(backup_once)"
expect "$scenario" "backup with off-site sync" "$started" test -n "$dump"
state after-backup --user 7000000002
failure_at="$(ms)"
record "$scenario" "RPO dump-to-failure (local)" PASS "$(($(date -u -d "$(stamp_iso "$dump")" +%s) * 1000))"

step "$scenario: final head upload fails (rclone stub)"
bk 'mkdir -p /backups/rehearsal-bin
    cat > /backups/rehearsal-bin/rclone << '\''STUB'\''
#!/usr/bin/env bash
case "$*" in *head.age*) exit 1 ;; esac
exec /usr/bin/rclone "$@"
STUB
    chmod +x /backups/rehearsal-bin/rclone'
started="$(ms)"
recovery_cutoff="$(cutoff)"
rc=0
journal_ship -e PATH=/backups/rehearsal-bin:/usr/local/bin:/usr/bin:/bin > /dev/null 2>&1 || rc=$?
head_unacknowledged() { [ "$rc" != 0 ] && [ "$(journal_status)" = head_upload_failed ]; }
expect "$scenario" "failed head upload exits non-zero, not acknowledged" "$started" head_unacknowledged
started="$(ms)"
replace_database "$dump" "$recovery_cutoff"
post_restore "$(stamp_iso "$dump")"
expect "$scenario" "restore + post-restore; gate closed" "$started" gate_closed
started="$(ms)"
out="$(replay || true)"
printf '%s\n' "$out"
stale_refused() { grep -q journal_stale_tail <<< "$out" && gate_closed; }
expect "$scenario" "replay refuses the stale tail; gate stays closed" "$started" stale_refused
started="$(ms)"
restore_id="$(privacy_status | sed -n 's/^restore_id: //p')"
dc run --rm --no-deps -T app python -m assistant.privacy.recovery_cli purge --restore-id "$restore_id" \
  | tr -d '\r'
expect "$scenario" "complete purge opens the gate" "$started" gate_open
started="$(ms)"
expect "$scenario" "restored-state invariants (account 2)" "$started" state check --user 7000000002 --mode purge
expect "$scenario" "restored-state invariants (account 1)" "$started" state check --user 7000000001 --mode purge
started="$(ms)"
expect "$scenario" "restore_verify gate" "$started" restore_verify
record "$scenario" "RTO failure-to-verified (local, synthetic size)" PASS "$failure_at"
started="$(ms)"
journal_ship > /dev/null || true
expect "$scenario" "shipping stays blocked (old chain not overwritten)" "$started" \
  test "$(journal_status)" = recovery_blocked

if [ "$failures" -ne 0 ]; then
  printf '\nREHEARSAL FAIL: %s check(s) failed\n' "$failures" >&2
  exit 1
fi
printf '\nREHEARSAL OK\n'
