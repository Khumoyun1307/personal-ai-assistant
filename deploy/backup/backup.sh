#!/usr/bin/env bash
# Encrypted database backups (ARCHITECTURE §14.2, §15.4, §10.4).
#
#   backup.sh once     — one dump now (then the off-site sync, if configured)
#   backup.sh upload   — only the off-site sync (retry after a failed upload)
#   backup.sh loop     — daily at BACKUP_HOUR_UTC, catch up after downtime
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
# Markers (UTC ISO-8601 timestamps); backup_check reads the success markers:
#   /backups/last_success         — the last dump that completed and verified
#   /backups/last_upload_success  — the last complete off-site sync
#   /backups/last_upload_attempt  — the last off-site attempt (retry throttle)
# Success markers are only written after the recorded step succeeded.
# /backups/last_success_file is the exact completed daily dump basename, not
# a timestamp. It is published atomically before the completion-time marker.
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

# Test clock only; production leaves BACKUP_TEST_NOW unset. GNU date comes
# from the postgres base image. All scheduler and marker times use this clock.
now() {
  if [ -n "${BACKUP_TEST_NOW:-}" ]; then
    date -u -d "$BACKUP_TEST_NOW" "$@"
  else
    date -u "$@"
  fi
}

log() { printf '%s backup: %s\n' "$(now +%FT%TZ)" "$*"; }

write_marker() {
  # Atomic: a reader never sees a half-written timestamp.
  local name="$1"
  now +%FT%TZ > "$BACKUP_DIR/$name.part"
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
  stamp="$(now +%Y%m%dT%H%M%SZ)"
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
  if [ "$(now +%u)" = "7" ]; then
    cp -- "$file" "$WEEKLY/"
  fi
  rotate "$DAILY" "$KEEP_DAILY"
  rotate "$WEEKLY" "$KEEP_WEEKLY"
  printf '%s\n' "$stamp.dump.age" > "$BACKUP_DIR/last_success_file.part"
  mv "$BACKUP_DIR/last_success_file.part" "$BACKUP_DIR/last_success_file"
  write_marker last_success
}

# Mirror daily/ and weekly/ to BACKUP_REMOTE. rclone is configured through
# RCLONE_CONFIG_* variables from .env (no config file on disk).
upload() {
  if [ -z "${BACKUP_REMOTE:-}" ]; then
    return 0
  fi
  # Persist before I/O: a restart after any failure retains the hourly
  # throttle. This is an attempt marker, never evidence of upload success.
  write_marker last_upload_attempt || return 1
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

# Missing or invalid markers are unknown, so catch up rather than skip work.
marker_epoch() {
  local value
  if [ -f "$BACKUP_DIR/$1" ]; then
    value="$(cat "$BACKUP_DIR/$1")"
    if [ -z "$value" ]; then
      printf '0\n'
      return
    fi
    date -u -d "$value" +%s 2> /dev/null || printf '0\n'
  else
    printf '0\n'
  fi
}

newest_dump_epoch() {
  local stamp
  if [ ! -d "$DAILY" ]; then
    printf '0\n'
    return
  fi
  stamp="$(find "$DAILY" -maxdepth 1 -type f -name '*.dump.age' -printf '%f\n' | sort | tail -n 1)"
  if [ -z "$stamp" ]; then
    printf '0\n'
    return
  fi
  # Names, not mtimes: copying a dump during recovery must not make it new.
  date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T${stamp:9:2}:${stamp:11:2}:${stamp:13:2}Z" +%s \
    2> /dev/null || printf '0\n'
}

loop() {
  local hour current scheduled last_dump last_upload newest last_attempt rc
  # Fail at start-up (visible in `ps`/logs), not at the first 02:00 run.
  : "${AGE_RECIPIENT:?AGE_RECIPIENT (age public key) is required}"
  hour="${BACKUP_HOUR_UTC:-02}"
  if [[ ! "$hour" =~ ^([01][0-9]|2[0-3])$ ]]; then
    log "invalid BACKUP_HOUR_UTC (expected 00-23)"
    return 1
  fi
  log "scheduled daily at ${hour}:00 UTC"
  while true; do
    current="$(now +%s)"
    scheduled="$(date -u -d "$(now +%F) ${hour}:00:00Z" +%s)"
    last_dump="$(marker_epoch last_success)"
    if [ "$last_dump" -eq 0 ] || [ "$((current - last_dump))" -ge 86400 ] \
       || { [ "$current" -ge "$scheduled" ] && [ "$last_dump" -lt "$scheduled" ]; }; then
      # A separate process, so errexit applies inside it as written.
      rc=0
      "$0" once || rc=$?
      if [ "$rc" -ne 0 ] && [ "$rc" -ne "$EXIT_UPLOAD_FAILED" ]; then
        log "scheduled dump failed; retry on next check"
      fi
    fi
    if [ -n "${BACKUP_REMOTE:-}" ]; then
      last_upload="$(marker_epoch last_upload_success)"
      last_dump="$(marker_epoch last_success)"
      newest="$(newest_dump_epoch)"
      last_attempt="$(marker_epoch last_upload_attempt)"
      # Check the file as well as the marker: a crash after the atomic dump
      # rename but before last_success must not lose an off-site retry.
      if { [ "$newest" -gt "$last_upload" ] || [ "$last_dump" -gt "$last_upload" ]; } \
         && { [ "$last_attempt" -eq 0 ] || [ "$((current - last_attempt))" -ge "$UPLOAD_RETRY_SECONDS" ]; }; then
        "$0" upload || true
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
