#!/usr/bin/env bash
# Restore an encrypted dump into a database (ARCHITECTURE §15.6; runbooks/restore.md).
#
#   restore.sh <file.dump.age> <target database>   < age-identity.txt
#
# The age IDENTITY (the owner's offline private key) is read from standard
# input by default (AGE_IDENTITY_FILE=-), so it is never written to the
# server's disk or into the container: the operator pipes it in over SSH
# (`docker compose exec -T backup restore.sh ... < identity`). Setting
# AGE_IDENTITY_FILE to a path is still supported. The dump is decrypted into
# a pipe, never to disk.
#
# The target database must exist and be empty (or be a scratch database for
# the restore test). After a *production* restore, the operator must run
# `python -m assistant post-restore --backup-taken-at <ts>`; `assistant run`
# refuses to start until that is done (`system_state.restore_pending`).
set -euo pipefail

file="${1:?dump file}"
target="${2:?target database}"
identity="${AGE_IDENTITY_FILE:--}"

if [ "$identity" = "-" ] && [ -t 0 ]; then
  echo "restore.sh: pipe the age identity on standard input (or set AGE_IDENTITY_FILE)" >&2
  exit 2
fi

age --decrypt -i "$identity" "$file" \
  | pg_restore --no-owner --no-acl --dbname="$target" --exit-on-error

# Mark the database as restored: the app will not start roles until
# post-restore has run (§15.6 step 1). The file name is passed as a psql
# variable (quoted by psql), not spliced into the SQL text.
psql --dbname="$target" -v ON_ERROR_STOP=1 -v dump_file="$(basename "$file")" <<'SQL'
INSERT INTO app.system_state (key, value)
VALUES ('restore_pending', jsonb_build_object('file', :'dump_file'::text, 'at', now()))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
SQL
echo "restored $(basename "$file") into $target; restore_pending set — run post-restore"
