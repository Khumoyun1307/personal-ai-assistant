#!/usr/bin/env bash
# Encrypted database backups (ARCHITECTURE §14.2, §15.4, §10.4).
#
#   backup.sh once   — one dump now
#   backup.sh loop   — one dump per day at BACKUP_HOUR_UTC, forever
#
# pg_dump (custom format) | age -r AGE_RECIPIENT  →  /backups/daily/<ts>.dump.age
# Rotation: 14 daily + 4 weekly (Sunday) copies locally; every new file is
# also uploaded with rclone to BACKUP_REMOTE when it is set (off-site copy).
# The private age key never exists on this host: only the recipient (public
# key) is here, so a stolen backup is useless without the owner's offline key.
#
# On success, /backups/last_success holds the UTC timestamp; the app's
# backup_check job alerts the owner when it is older than ~26 h.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DAILY="$BACKUP_DIR/daily"
WEEKLY="$BACKUP_DIR/weekly"
KEEP_DAILY="${KEEP_DAILY:-14}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT (age public key) is required}"

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*"; }

dump_once() {
  mkdir -p "$DAILY" "$WEEKLY"
  local stamp file tmp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="$DAILY/$stamp.dump.age"
  tmp="$file.part"
  # --no-owner/--no-acl: restores into any role; custom format for pg_restore.
  pg_dump --format=custom --no-owner --no-acl \
    | age -r "$AGE_RECIPIENT" > "$tmp"
  mv "$tmp" "$file"
  local size
  size="$(stat -c %s "$file")"
  if [ "$size" -lt 1024 ]; then
    log "dump suspiciously small ($size bytes); not recording success"
    return 1
  fi
  log "wrote $(basename "$file") ($size bytes)"
  # Sunday's dump is also the weekly copy.
  if [ "$(date -u +%u)" = "7" ]; then
    cp "$file" "$WEEKLY/"
  fi
  rotate "$DAILY" "$KEEP_DAILY"
  rotate "$WEEKLY" "$KEEP_WEEKLY"
  if [ -n "${BACKUP_REMOTE:-}" ]; then
    # rclone is configured through RCLONE_CONFIG_* variables in .env (no
    # config file on disk). Failure to upload keeps the local copy and is
    # logged; the local success still counts, the next run retries the sync.
    if rclone copy "$file" "$BACKUP_REMOTE/daily/" --quiet \
       && rclone sync "$WEEKLY" "$BACKUP_REMOTE/weekly/" --quiet; then
      log "uploaded to remote"
    else
      log "remote upload failed (local copy kept)"
    fi
  fi
  date -u +%FT%TZ > "$BACKUP_DIR/last_success.part"
  mv "$BACKUP_DIR/last_success.part" "$BACKUP_DIR/last_success"
}

rotate() {
  local dir="$1" keep="$2"
  # shellcheck disable=SC2012
  ls -1t "$dir"/*.dump.age 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm -f --
}

case "${1:-loop}" in
  once)
    dump_once
    ;;
  loop)
    hour="${BACKUP_HOUR_UTC:-02}"
    log "scheduled daily at ${hour}:00 UTC"
    while true; do
      if [ "$(date -u +%H)" = "$hour" ] && [ ! -f "$BACKUP_DIR/.done-$(date -u +%F)" ]; then
        if dump_once; then
          touch "$BACKUP_DIR/.done-$(date -u +%F)"
          find "$BACKUP_DIR" -maxdepth 1 -name '.done-*' -mtime +2 -delete
        fi
      fi
      sleep 300
    done
    ;;
  *)
    echo "usage: backup.sh [once|loop]" >&2
    exit 2
    ;;
esac
