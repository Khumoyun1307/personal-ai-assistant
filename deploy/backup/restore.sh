#!/usr/bin/env bash
# Restore an encrypted dump into a database (ARCHITECTURE §15.6; runbooks/restore.md).
#
#   restore.sh <file.dump.age> <target database>
#
# Needs the age IDENTITY (private key) in AGE_IDENTITY_FILE — the owner's
# offline key, present only for the duration of the restore and removed
# afterwards. Decrypts to a pipe, never to disk.
#
# The target database must exist and be empty (or be a scratch database for
# the restore test). After a *production* restore, the operator must run
# `python -m assistant post-restore --backup-taken-at <ts>`; `assistant run`
# refuses to start until that is done (`system_state.restore_pending`).
set -euo pipefail

file="${1:?dump file}"
target="${2:?target database}"
: "${AGE_IDENTITY_FILE:?AGE_IDENTITY_FILE (path to the age private key) is required}"

age --decrypt -i "$AGE_IDENTITY_FILE" "$file" \
  | pg_restore --no-owner --no-acl --dbname="$target" --exit-on-error

# Mark the database as restored: the app will not start roles until
# post-restore has run (§15.6 step 1).
psql --dbname="$target" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO app.system_state (key, value) VALUES ('restore_pending', jsonb_build_object('file', '$(basename "$file")', 'at', now()))
   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();"
echo "restored $(basename "$file") into $target; restore_pending set — run post-restore"
