#!/usr/bin/env bash
# Server-side operations, run in /opt/assistant by the `just deploy` and
# `just prod-*` recipes over SSH (deploy/oci/ssh.sh). Keeping them in one
# script avoids quoting compose commands through PowerShell, docker and ssh.
#
#   deploy/server.sh deploy          pull main (ff-only), rebuild, migrate, restart
#   deploy/server.sh status          containers, app healthcheck, backup markers
#   deploy/server.sh logs [N]        last N (default 200) app log lines (content-free)
#   deploy/server.sh backup          a backup now (exit 3: dump ok, upload failed)
#   deploy/server.sh offsite-list    file names in the off-site daily/ and weekly/
#   deploy/server.sh restore-test    restore the newest dump into a scratch DB;
#                                    the age identity arrives on stdin only
#   deploy/server.sh app-stop|app-start   for the heartbeat-alert check
#   deploy/server.sh restart-timing  restart app, seconds until healthy again
#   deploy/server.sh reboot          reboot the host (then: status)
#   deploy/server.sh reindex-search  re-key search after a normalizer change
#                                    (idempotent; counts only)
#   deploy/server.sh nfr04 [DAYS]    NFR-04: p50/p95 answer times of owner
#                                    requests per class, last DAYS (30) days
#                                    (deploy/sql/nfr04.sql; timings only)
#   deploy/server.sh verify-r0 ARGS  tools/acceptance/verify_r0.py in a one-off
#                                    app container (prints numbers/states only)
#   deploy/server.sh p2-verify ARGS  P2 read-only views (ids/states/times only)
#   deploy/server.sh trial-check ARGS  P2 daily send audit (ids/states/counts only)
#
# Every compose call passes `--env-file .env`: the project directory is
# deploy/, so the root .env would otherwise not be used for interpolation.
# Every action that starts, inspects or restores the stack first checks the
# data volume (require_data_volume) and refuses, exit 1, without it; logs,
# offsite-list, app-stop and reboot work regardless.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "ERROR: $(pwd)/.env is missing (deploy/oci/push-env.sh)" >&2; exit 1; }
compose() { docker compose --env-file .env -f deploy/compose.prod.yml "$@"; }
# db_client <dropdb|createdb|psql> ARGS: a client inside the db container,
# connecting as the role the server .env names ($POSTGRES_USER reaches the
# container through compose.prod.yml), so no role is hard-coded here. The
# arguments are passed through as positional parameters, never re-parsed.
# shellcheck disable=SC2016  # expanded inside the db container
db_client() { compose exec -T db sh -c 'c="$1"; shift; exec "$c" -U "$POSTGRES_USER" "$@"' sh "$@"; }

# Mount guard (ARCHITECTURE §15.4): nothing starts, backs up, restores or
# reports a healthy stack unless the data volume is mounted at DATA_DIR.
# DATA_DIR is the only value this shell reads from .env (it is no secret).
# The sentinel is written on the volume by bootstrap-host.sh, so a bare
# mountpoint directory on the boot disk never passes.
data_dir="$(sed -n 's/^DATA_DIR=//p' .env | tail -n 1)"
data_dir="${data_dir%\"}"
data_dir="${data_dir#\"}"
data_volume_problem() {
  if [ -z "$data_dir" ]; then
    echo "DATA_DIR is not set in .env"
  elif [ "$(findmnt -no TARGET --target "$data_dir" 2> /dev/null)" = "/" ]; then
    echo "$data_dir is on the root filesystem: the data volume is not mounted"
  elif [ ! -f "$data_dir/.alviro-data-volume" ]; then
    echo "$data_dir/.alviro-data-volume is missing: $data_dir is not the data volume"
  fi
}
require_data_volume() {
  local problem
  problem="$(data_volume_problem)"
  if [ -n "$problem" ]; then
    echo "ERROR: $problem; refusing to run '$action' (runbooks/provision.md: data volume)" >&2
    exit 1
  fi
}

action="${1:-}"
shift || true
case "$action" in
  deploy)
    require_data_volume
    git pull --ff-only --quiet
    echo "at $(git log -1 --format='%h %cs')"
    # Re-exec so the steps below come from the version just pulled.
    exec bash deploy/server.sh _up
    ;;
  _up)
    require_data_volume
    compose up -d --build --remove-orphans
    compose ps
    ;;
  status)
    # First, because nothing below means anything without the volume.
    require_data_volume
    printf 'data dir: %s (sentinel present)\n' "$(findmnt -no SOURCE,TARGET --target "$data_dir/postgres")"
    compose ps
    compose exec -T app python -m assistant healthcheck
    for marker in last_success last_upload_success; do
      printf '%s: %s\n' "$marker" \
        "$(compose exec -T backup cat "/backups/$marker" 2> /dev/null || echo never)"
    done
    # Oracle may reclaim an Always Free A1 instance whose 7-day p95 CPU,
    # network and memory use are all below 20% (runbooks/provision.md §C).
    free -m | awk '/^Mem:/ { printf "memory: %d of %d MiB used (%d%%)\n", $3, $2, 100 * $3 / $2 }'
    printf 'load: %s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
    ;;
  logs)
    compose logs --no-color --tail="${1:-200}" app
    ;;
  backup)
    require_data_volume
    compose exec -T backup backup.sh once
    ;;
  offsite-list)
    # shellcheck disable=SC2016  # expanded inside the backup container
    compose exec -T backup bash -c \
      'for d in daily weekly; do echo "$d/:"; rclone lsf "$BACKUP_REMOTE/$d" --files-only; done'
    ;;
  restore-test)
    require_data_volume
    # runbooks/restore.md §A. The identity is read from this script's stdin
    # and passed straight through to age (restore.sh, AGE_IDENTITY_FILE=-):
    # it is never written to the server's disk or into a container.
    [ ! -t 0 ] || { echo "ERROR: pipe the age identity on stdin" >&2; exit 2; }
    # Held in memory only, and read before any other command can consume
    # stdin (docker compose exec forwards stdin).
    identity="$(cat)"
    case "$identity" in
      *AGE-SECRET-KEY-1*) ;;
      *) echo "ERROR: stdin holds no age identity" >&2; exit 2 ;;
    esac
    dump="$(compose exec -T backup bash -c 'ls -1 /backups/daily/*.dump.age | sort | tail -n 1' | tr -d '\r')"
    [ -n "$dump" ] || { echo "ERROR: no dump in /backups/daily" >&2; exit 1; }
    stamp="$(basename "$dump" .dump.age)" # 20261022T020000Z
    taken_at="${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T${stamp:9:2}:${stamp:11:2}:${stamp:13:2}Z"
    db_client dropdb --if-exists restore_test
    # Also remove the scratch database if decrypt, post-restore or a gate fails.
    trap 'db_client dropdb --if-exists restore_test; echo "restore_test dropped"' EXIT
    db_client createdb restore_test
    printf '%s\n' "$identity" | compose exec -T backup restore.sh "$dump" restore_test
    unset identity
    # post-restore against the scratch database: the URL is derived inside
    # the container from its own DATABASE_URL, so the password never passes
    # through this shell. --no-deps: no migrate run, no poller, no sends.
    # shellcheck disable=SC2016  # expanded inside the app container
    compose run --rm --no-deps -T app sh -c \
      'DATABASE_URL="${DATABASE_URL%/*}/restore_test" exec python -m assistant post-restore --backup-taken-at "$0"' \
      "$taken_at"
    since="$(date -u -d "$taken_at 2 days ago" +%Y-%m-%dT%H:%M:%SZ)"
    # Only read-only verification code is mounted. No app/poller/worker starts;
    # every URL is derived inside the one-off container and targets scratch.
    # shellcheck disable=SC2016
    compose run --rm --no-deps -T \
      -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" app sh -c \
      'DATABASE_URL="${DATABASE_URL%/*}/restore_test" exec python tools/acceptance/p2_verify.py release'
    # shellcheck disable=SC2016
    compose run --rm --no-deps -T \
      -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" app sh -c \
      'DATABASE_URL="${DATABASE_URL%/*}/restore_test" exec python tools/acceptance/p2_verify.py actions --since "$0"' \
      "$since"
    # shellcheck disable=SC2016
    compose run --rm --no-deps -T \
      -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" app sh -c \
      'DATABASE_URL="${DATABASE_URL%/*}/restore_test" exec python -m tools.acceptance.restore_verify'
    echo "dump: $(basename "$dump") ($(compose exec -T backup stat -c %s "$dump" | tr -d '\r') bytes), taken $taken_at"
    for table in messages conversations accounts coverage_gaps; do
      printf 'restore_test app.%s rows: %s\n' "$table" \
        "$(db_client psql -d restore_test -tAc "SELECT count(*) FROM app.$table" | tr -d '\r[:space:]')"
    done
    printf 'restore gap row: %s\n' \
      "$(db_client psql -d restore_test -tAc "SELECT kind || '/' || certainty FROM app.coverage_gaps ORDER BY id DESC LIMIT 1" | tr -d '\r[:space:]')"
    echo "restore verification passed"
    ;;
  restore)
    require_data_volume
    # runbooks/restore.md §B: replace the production database with one dump.
    # The identity arrives on stdin (as in restore-test); the app must be
    # stopped first so nothing sends while the database is replaced.
    file="${1:?usage: restore <dump file name in /backups/daily>}"
    [[ "$file" =~ ^[0-9]{8}T[0-9]{6}Z\.dump\.age$ ]] || { echo "ERROR: not a dump file name" >&2; exit 2; }
    identity="$(cat)"
    case "$identity" in
      *AGE-SECRET-KEY-1*) ;;
      *) echo "ERROR: stdin holds no age identity" >&2; exit 2 ;;
    esac
    if [ -n "$(compose ps --status running -q app)" ]; then
      echo "ERROR: the app is running; run app-stop first" >&2
      exit 1
    fi
    compose exec -T backup test -f "/backups/daily/$file" \
      || { echo "ERROR: /backups/daily/$file not found" >&2; exit 1; }
    # The production database is the one the server .env names: $POSTGRES_DB
    # in the db container, $PGDATABASE (the same value) in the backup one.
    # shellcheck disable=SC2016  # expanded inside the db container
    compose exec -T db sh -c \
      'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
    # shellcheck disable=SC2016  # expanded inside the backup container
    printf '%s\n' "$identity" \
      | compose exec -T backup sh -c 'exec restore.sh "$1" "$PGDATABASE"' sh "/backups/daily/$file"
    unset identity
    stamp="${file%.dump.age}"
    echo "next: post-restore --backup-taken-at ${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T${stamp:9:2}:${stamp:11:2}:${stamp:13:2}Z"
    ;;
  post-restore)
    require_data_volume
    compose run --rm --no-deps -T app python -m assistant post-restore --backup-taken-at "${1:?timestamp}"
    ;;
  reindex-search)
    require_data_volume
    # ARCHITECTURE §11.2: re-key retained messages after a normalizer change.
    # Idempotent and safe beside the running app (an edit always wins); a
    # no-op when the stored version is current. Prints counts only.
    compose run --rm --no-deps -T app python -m assistant reindex-search
    ;;
  nfr04)
    require_data_volume
    # NFR-04 (M03 acceptance): answer times of owner requests from
    # agent_runs. The query reads timings, run types, statuses and tool names
    # only, never a content column, in a read-only transaction; DAYS reaches
    # it as a psql variable after this check, never spliced into the SQL.
    days="${1:-30}"
    [[ "$days" =~ ^[1-9][0-9]{0,3}$ ]] || { echo "usage: nfr04 [days, 1-9999; default 30]" >&2; exit 2; }
    # shellcheck disable=SC2016  # expanded inside the db container
    compose exec -T db sh -c \
      'export PGOPTIONS="-c default_transaction_read_only=on"; exec psql -X -q -v ON_ERROR_STOP=1 -v days="$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -' \
      sh "$days" < deploy/sql/nfr04.sql
    ;;
  app-stop)
    compose stop app
    ;;
  app-start)
    require_data_volume
    compose up -d app
    ;;
  restart-timing)
    require_data_volume
    start="$(date +%s)"
    compose restart app > /dev/null
    until compose exec -T app python -m assistant healthcheck > /dev/null 2>&1; do
      [ $(($(date +%s) - start)) -lt 180 ] || { echo "ERROR: not healthy after 180 s" >&2; exit 1; }
      sleep 2
    done
    echo "app healthy again $(($(date +%s) - start)) s after restart"
    ;;
  reboot)
    echo "rebooting; reconnect in ~2 min and run: status"
    sudo systemctl reboot
    ;;
  verify-r0)
    require_data_volume
    # tools/ is not in the prod image (it stays outside src/, AGENTS §5): the
    # checkout's copy is mounted read-only into a one-off app container.
    compose run --rm --no-deps -T \
      -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" \
      app python tools/acceptance/verify_r0.py "$@"
    ;;
  p2-verify)
    require_data_volume
    compose run --rm --no-deps -T \
      -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" \
      app python tools/acceptance/p2_verify.py "$@"
    ;;
  trial-check)
    require_data_volume
    compose run --rm --no-deps -T \
      -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" \
      app python tools/acceptance/p2_verify.py trial-check "$@"
    ;;
  *)
    echo "usage: deploy/server.sh deploy|status|logs [N]|backup|nfr04 [DAYS]|verify-r0 ARGS|p2-verify ARGS|trial-check ARGS" >&2
    exit 2
    ;;
esac
