#!/usr/bin/env bash
# I20: independent ids-only journal shipping and authenticated restore pipe.
# Encryption reuses the backup's age recipient/offline identity and B2 remote.
set -euo pipefail
umask 077
BACKUP_DIR="${BACKUP_DIR:-/backups}"
JOURNAL_DIR="$BACKUP_DIR/privacy-journal"

# Same deterministic marker clock as backup.sh; real snapshot cutoffs come
# from the database, never from this test override or a file timestamp.
now() {
  if [ -n "${BACKUP_TEST_NOW:-}" ]; then
    date -u -d "$BACKUP_TEST_NOW" "$@"
  else
    date -u "$@"
  fi
}

status() {
  printf '%s\n' "$1" > "$BACKUP_DIR/journal_status.part"
  mv "$BACKUP_DIR/journal_status.part" "$BACKUP_DIR/journal_status"
}

ship() {
  mkdir -p "$JOURNAL_DIR/segments"
  # A separate process lock covers segment/head advancement, including retries.
  exec 8> "$JOURNAL_DIR/upload.lock"
  if [ "${1:-}" = force ]; then
    flock -w 30 8 || return 1
  else
    flock -n 8 || return 0
  fi
  local ready stamp token segment checksum last current
  current="$(now +%s)"
  last=0
  if [ -f "$JOURNAL_DIR/last_attempt" ]; then
    last="$(cat "$JOURNAL_DIR/last_attempt")"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
  fi
  if [ "${1:-}" != force ] && [ "$((current - last))" -lt 60 ]; then return 0; fi
  printf '%s\n' "$current" > "$JOURNAL_DIR/last_attempt.part"
  mv "$JOURNAL_DIR/last_attempt.part" "$JOURNAL_DIR/last_attempt"
  # Never read/advance the independent head from a rewound snapshot.
  ready="$(psql -XqAt -v ON_ERROR_STOP=1 -c "SELECT
    NOT EXISTS (SELECT 1 FROM app.system_state
      WHERE key IN ('restore_pending', 'privacy_journal_upload_blocked'))" 2> /dev/null)" || {
    status database_unavailable; return 1;
  }
  if [ "$ready" != t ]; then status recovery_blocked; return 0; fi
  if [ -z "${BACKUP_REMOTE:-}" ]; then status remote_missing; return 1; fi
  : "${AGE_RECIPIENT:?age recipient required}"
  stamp="$(now +%FT%TZ)"
  # Object labels carry no identity/cutoff semantics and cannot collide on
  # a wall-clock rollback. This UUID is only a filename, never a crypto key.
  IFS= read -r token < /proc/sys/kernel/random/uuid
  segment="$JOURNAL_DIR/segments/$token.age"
  if ! psql -XqAt -v ON_ERROR_STOP=1 -f /usr/local/share/privacy-journal.sql 2> /dev/null \
      | age -r "$AGE_RECIPIENT" > "$segment.part"; then
    rm -f "$segment.part"
    status export_failed; return 1
  fi
  mv "$segment.part" "$segment"
  checksum="$(sha256sum "$segment" | cut -d' ' -f1)"
  # Strict version/token/checksum wire format; no subject IDs in object names.
  if ! printf '1 %s %s\n' "$token" "$checksum" \
      | age -r "$AGE_RECIPIENT" > "$JOURNAL_DIR/head.age.part"; then
    status encryption_failed; return 1
  fi
  if ! rclone copyto "$segment" "$BACKUP_REMOTE/privacy-journal/segments/$token.age" --quiet \
      2> /dev/null; then status segment_upload_failed; return 1; fi
  if ! rclone copyto "$JOURNAL_DIR/head.age.part" "$BACKUP_REMOTE/privacy-journal/head.age" --quiet \
      2> /dev/null; then status head_upload_failed; return 1; fi
  mv "$JOURNAL_DIR/head.age.part" "$JOURNAL_DIR/head.age"
  printf '%s\n' "$stamp" > "$BACKUP_DIR/last_journal_upload_success.part"
  mv "$BACKUP_DIR/last_journal_upload_success.part" "$BACKUP_DIR/last_journal_upload_success"
  status acknowledged
}

fetch() {
  : "${BACKUP_REMOTE:?backup remote required}"
  local identity manifest token checksum actual temp payload uuid_pattern
  identity="$(cat)"
  # Only encrypted objects touch disk; identity and decrypted metadata use pipes.
  temp="$(mktemp -d)"
  journal_fetch_temp="$temp"
  trap 'rm -rf -- "$journal_fetch_temp"' EXIT
  if ! rclone copyto "$BACKUP_REMOTE/privacy-journal/head.age" "$temp/head.age" --quiet \
      2> /dev/null; then echo journal_missing >&2; return 1; fi
  if ! manifest="$(age --decrypt -i <(printf '%s\n' "$identity") "$temp/head.age" 2> /dev/null)"; then
    echo journal_decrypt_failed >&2; return 1
  fi
  uuid_pattern='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
  if [[ ! "$manifest" =~ ^1\ ($uuid_pattern)\ ([0-9a-f]{64})$ ]]; then
    echo journal_manifest_invalid >&2; return 1
  fi
  token="${BASH_REMATCH[1]}"
  checksum="${BASH_REMATCH[2]}"
  if ! rclone copyto "$BACKUP_REMOTE/privacy-journal/segments/$token.age" "$temp/segment.age" --quiet \
      2> /dev/null; then echo journal_tail_missing >&2; return 1; fi
  actual="$(sha256sum "$temp/segment.age" | cut -d' ' -f1)"
  if [ "$actual" != "$checksum" ]; then echo journal_checksum_failed >&2; return 1; fi
  # age streams plaintext before checking the final authentication tag. Hold
  # metadata in memory until its exit status is known; a failed decrypt must
  # never deliver a complete-looking JSON object to the recovery process.
  if ! payload="$(age --decrypt -i <(printf '%s\n' "$identity") "$temp/segment.age" 2> /dev/null)"; then
    echo journal_decrypt_failed >&2; return 1
  fi
  printf '%s\n' "$payload"
}

case "${1:-}" in
  ship) ship "${2:-}" ;;
  fetch) fetch ;;
  *) echo 'usage: privacy-journal.sh ship [force]|fetch' >&2; exit 2 ;;
esac
