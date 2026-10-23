#!/usr/bin/env bash
# Encrypted database backups (ARCHITECTURE §14.2, §15.4, §10.4).
#
#   backup.sh once     — one dump now (then the off-site sync, if configured)
#   backup.sh upload   — only the off-site sync (retry after a failed upload)
#   backup.sh loop     — one dump per day at BACKUP_HOUR_UTC, forever
#
# pg_dump (custom format) | age -r AGE_RECIPIENT  →  /backups/daily/<ts>.dump.age
# Rotation: 14 daily + 4 weekly (Sunday) copies locally. When BACKUP_REMOTE is
# set, the remote is made an exact mirror of the local daily/ and weekly/
# directories with `rclone sync`: files rotated out locally are deleted
# off-site too (the B2 remote is configured with hard_delete, so no hidden
# versions survive). Deleted data must not outlive the retention window
# anywhere (ARCHITECTURE §10.4).
# The private age key never exists on this host: only the recipient (public
# key) is here, so a stolen backup is useless without the owner's offline key.
#
# Markers (UTC ISO-8601 timestamps), read by the app's backup_check job:
#   /backups/last_success         — the last dump that completed and verified
#   /backups/last_upload_success  — the last complete off-site sync
# A marker is only ever written after the step it records succeeded.
#
# Exit codes of `once`: 0 dump (and sync) ok; 1 dump failed; 3 dump ok but the
# off-site sync failed (the local copy is kept; `loop` retries hourly).
# Output is content-free: file names, sizes, step names.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DAILY="$BACKUP_DIR/daily"
WEEKLY="$BACKUP_DIR/weekly"
KEEP_DAILY="${KEEP_DAILY:-14}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-1024}"
UPLOAD_RETRY_SECONDS="${UPLOAD_RETRY_SECONDS:-3600}"
MAX_REMOTE_DELETE="${BACKUP_MAX_REMOTE_DELETE:-7}"
EXIT_UPLOAD_FAILED=3

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*"; }

write_marker() {
  # Atomic: a reader never sees a half-written timestamp.
  local name="$1"
  date -u +%FT%TZ > "$BACKUP_DIR/$name.part"
  mv "$BACKUP_DIR/$name.part" "$BACKUP_DIR/$name"
}

# Keep the newest $2 dumps in $1. Names are UTC timestamps, so a reverse name
# sort is newest first and does not depend on file mtimes.
rotate() {
  local dir="$1" keep="$2"
  find "$dir" -maxdepth 1 -type f -name '*.dump.age' -printf '%f\n' \
    | sort -r | tail -n +"$((keep + 1))" \
    | while IFS= read -r old; do rm -f -- "${dir:?}/$old"; done
}

dump() {
  : "${AGE_RECIPIENT:?AGE_RECIPIENT (age public key) is required}"
  if [ ! -w "$BACKUP_DIR" ]; then
    log "backup directory is not writable by uid $(id -u); not dumping"
    return 1
  fi
  mkdir -p "$DAILY" "$WEEKLY"
  # A .part left by an interrupted run is never a valid backup.
  find "$DAILY" -maxdepth 1 -name '*.part' -delete

  local stamp file tmp size
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="$DAILY/$stamp.dump.age"
  tmp="$file.part"
  # `if !` because this function may run where errexit is suspended; the
  # pipeline status (pipefail) is checked explicitly so a failed or truncated
  # pg_dump can never be recorded as a success.
  # --no-owner/--no-acl: restores into any role; custom format for pg_restore.
  if ! pg_dump --format=custom --no-owner --no-acl | age -r "$AGE_RECIPIENT" > "$tmp"; then
    rm -f -- "$tmp"
    log "dump failed (pg_dump or age exited non-zero); no backup recorded"
    return 1
  fi
  size="$(stat -c %s "$tmp")"
  if [ "$size" -lt "$MIN_DUMP_BYTES" ]; then
    rm -f -- "$tmp"
    log "dump suspiciously small ($size bytes); discarded, no backup recorded"
    return 1
  fi
  mv -- "$tmp" "$file"
  log "wrote $(basename "$file") ($size bytes)"
  # Sunday's dump is also the weekly copy.
  if [ "$(date -u +%u)" = "7" ]; then
    cp -- "$file" "$WEEKLY/"
  fi
  rotate "$DAILY" "$KEEP_DAILY"
  rotate "$WEEKLY" "$KEEP_WEEKLY"
  write_marker last_success
}

# Mirror daily/ and weekly/ to BACKUP_REMOTE. rclone is configured through
# RCLONE_CONFIG_* variables from .env (no config file on disk).
upload() {
  if [ -z "${BACKUP_REMOTE:-}" ]; then
    return 0
  fi
  local sub local_files remote_files stale
  for sub in daily weekly; do
    mkdir -p "$BACKUP_DIR/$sub"
    # Guard: a rebuilt host with a fresh, nearly empty /backups must not wipe
    # the off-site history. Normal rotation deletes one or two remote files
    # per run; when more would go, nothing is deleted, no upload marker is
    # written and the owner is alerted (runbooks/restore.md §C).
    local_files="$(find "$BACKUP_DIR/$sub" -maxdepth 1 -type f -name '*.dump.age' -printf '%f\n' | sort)"
    remote_files="$(rclone lsf "$BACKUP_REMOTE/$sub" --files-only --include '*.dump.age' 2> /dev/null | sort)" \
      || remote_files=""
    stale="$(comm -13 <(printf '%s\n' "$local_files") <(printf '%s\n' "$remote_files") | grep -c . || true)"
    if [ "$stale" -gt "$MAX_REMOTE_DELETE" ]; then
      log "off-site sync of $sub refused: it would delete $stale remote dumps (limit $MAX_REMOTE_DELETE)"
      return 1
    fi
    # Only finished dumps are mirrored; a remote file that no longer exists
    # locally is deleted (sync semantics; deletes run after the transfers).
    # --max-delete is a second guard inside rclone itself.
    if ! rclone sync "$BACKUP_DIR/$sub" "$BACKUP_REMOTE/$sub" \
         --include '*.dump.age' --max-delete "$MAX_REMOTE_DELETE" --quiet; then
      log "off-site sync of $sub failed (local copies kept)"
      return 1
    fi
  done
  write_marker last_upload_success
  log "off-site sync complete"
}

once() {
  # Called as a plain command (never in a condition), so errexit applies in
  # dump(): any failing step aborts before last_success is written.
  dump
  if ! upload; then
    return "$EXIT_UPLOAD_FAILED"
  fi
}

loop() {
  local hour today rc next_retry=0 upload_pending=0
  # Fail at start-up (visible in `ps`/logs), not at the first 02:00 run.
  : "${AGE_RECIPIENT:?AGE_RECIPIENT (age public key) is required}"
  hour="${BACKUP_HOUR_UTC:-02}"
  log "scheduled daily at ${hour}:00 UTC"
  while true; do
    today="$(date -u +%F)"
    if [ "$(date -u +%H)" = "$hour" ] && [ ! -f "$BACKUP_DIR/.done-$today" ]; then
      # A separate process, so errexit applies inside it as written.
      rc=0
      "$0" once || rc=$?
      if [ "$rc" -eq 0 ] || [ "$rc" -eq "$EXIT_UPLOAD_FAILED" ]; then
        touch "$BACKUP_DIR/.done-$today"
        find "$BACKUP_DIR" -maxdepth 1 -name '.done-*' -mtime +2 -delete
      fi
      if [ "$rc" -eq "$EXIT_UPLOAD_FAILED" ]; then
        upload_pending=1
        next_retry=$(($(date +%s) + UPLOAD_RETRY_SECONDS))
      else
        upload_pending=0
      fi
    elif [ "$upload_pending" -eq 1 ] && [ "$(date +%s)" -ge "$next_retry" ]; then
      if "$0" upload; then
        upload_pending=0
      else
        next_retry=$(($(date +%s) + UPLOAD_RETRY_SECONDS))
      fi
    fi
    sleep 300
  done
}

case "${1:-loop}" in
  once) once ;;
  upload) upload ;;
  loop) loop ;;
  *)
    echo "usage: backup.sh [once|upload|loop]" >&2
    exit 2
    ;;
esac
