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
#   deploy/server.sh verify-r0 ARGS  tools/acceptance/verify_r0.py in a one-off
#                                    app container (prints numbers/states only)
#
# Every compose call passes `--env-file .env`: the project directory is
# deploy/, so the root .env would otherwise not be used for interpolation.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "ERROR: $(pwd)/.env is missing (deploy/oci/push-env.sh)" >&2; exit 1; }
compose() { docker compose --env-file .env -f deploy/compose.prod.yml "$@"; }

action="${1:-}"
shift || true
case "$action" in
  deploy)
    git pull --ff-only --quiet
    echo "at $(git log -1 --format='%h %cs')"
    # Re-exec so the steps below come from the version just pulled.
    exec bash deploy/server.sh _up
    ;;
  _up)
    compose up -d --build --remove-orphans
    compose ps
    ;;
  status)
    compose ps
    compose exec -T app python -m assistant healthcheck
    for marker in last_success last_upload_success; do
      printf '%s: %s\n' "$marker" \
        "$(compose exec -T backup cat "/backups/$marker" 2> /dev/null || echo never)"
    done
    printf 'data dir: %s\n' "$(findmnt -no SOURCE,TARGET --target /mnt/data/postgres)"
    # Oracle may reclaim an Always Free A1 instance whose 7-day p95 CPU,
    # network and memory use are all below 20% (runbooks/provision.md §C).
    free -m | awk '/^Mem:/ { printf "memory: %d of %d MiB used (%d%%)\n", $3, $2, 100 * $3 / $2 }'
    printf 'load: %s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
    ;;
  logs)
    compose logs --no-color --tail="${1:-200}" app
    ;;
  backup)
    compose exec -T backup backup.sh once
    ;;
  offsite-list)
    # shellcheck disable=SC2016  # expanded inside the backup container
    compose exec -T backup bash -c \
      'for d in daily weekly; do echo "$d/:"; rclone lsf "$BACKUP_REMOTE/$d" --files-only; done'
    ;;
  restore-test)
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
    compose exec -T db dropdb -U assistant --if-exists restore_test
    compose exec -T db createdb -U assistant restore_test
    printf '%s\n' "$identity" | compose exec -T backup restore.sh "$dump" restore_test
    unset identity
    # post-restore against the scratch database: the URL is derived inside
    # the container from its own DATABASE_URL, so the password never passes
    # through this shell. --no-deps: no migrate run, no poller, no sends.
    # shellcheck disable=SC2016  # expanded inside the app container
    compose run --rm --no-deps -T app sh -c \
      'DATABASE_URL="${DATABASE_URL%/*}/restore_test" exec python -m assistant post-restore --backup-taken-at "$0"' \
      "$taken_at"
    echo "dump: $(basename "$dump") ($(compose exec -T backup stat -c %s "$dump" | tr -d '\r') bytes), taken $taken_at"
    for table in messages conversations accounts coverage_gaps; do
      printf 'restore_test app.%s rows: %s\n' "$table" \
        "$(compose exec -T db psql -U assistant -d restore_test -tAc "SELECT count(*) FROM app.$table" | tr -d '\r[:space:]')"
    done
    printf 'restore gap row: %s\n' \
      "$(compose exec -T db psql -U assistant -d restore_test -tAc "SELECT kind || '/' || certainty FROM app.coverage_gaps ORDER BY id DESC LIMIT 1" | tr -d '\r[:space:]')"
    compose exec -T db dropdb -U assistant restore_test
    echo "restore_test dropped"
    ;;
  restore)
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
    compose exec -T db dropdb -U assistant --if-exists --force assistant
    compose exec -T db createdb -U assistant assistant
    printf '%s\n' "$identity" | compose exec -T backup restore.sh "/backups/daily/$file" assistant
    unset identity
    stamp="${file%.dump.age}"
    echo "next: post-restore --backup-taken-at ${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T${stamp:9:2}:${stamp:11:2}:${stamp:13:2}Z"
    ;;
  post-restore)
    compose run --rm --no-deps -T app python -m assistant post-restore --backup-taken-at "${1:?timestamp}"
    ;;
  app-stop)
    compose stop app
    ;;
  app-start)
    compose up -d app
    ;;
  restart-timing)
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
    # tools/ is not in the prod image (it stays outside src/, AGENTS §5): the
    # checkout's copy is mounted read-only into a one-off app container.
    compose run --rm --no-deps -T \
      -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" \
      app python tools/acceptance/verify_r0.py "$@"
    ;;
  *)
    echo "usage: deploy/server.sh deploy|status|logs [N]|backup|verify-r0 ARGS" >&2
    exit 2
    ;;
esac
