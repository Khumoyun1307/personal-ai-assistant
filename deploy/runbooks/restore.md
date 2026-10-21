# Restore from backup (ARCHITECTURE §15.6)

A restore rewinds the database by up to 24 h. Messages Telegram already
delivered in between are gone for good (it does not redeliver), and any
action, consent change, deletion or setting made after the dump is undone.
The mandatory post-restore step makes that honest and safe: it pauses
autonomy, cancels open actions, records a `possible` coverage gap and tells
the owner.

## A. The restore test (before R1; repeat after every major upgrade)

Into a **scratch** database on the server, never over production, and never
with the production bot token running against it:

```bash
cd /opt/assistant
docker compose -f deploy/compose.prod.yml exec db createdb -U assistant restore_test
# the owner's offline age key is needed only for this step: copy it in, use it, remove it
scp ~/age-identity.txt deploy@<ip>:/tmp/age-identity.txt
docker compose -f deploy/compose.prod.yml cp /tmp/age-identity.txt backup:/tmp/age-identity.txt
docker compose -f deploy/compose.prod.yml exec -e AGE_IDENTITY_FILE=/tmp/age-identity.txt backup \
  restore.sh /backups/daily/<latest>.dump.age restore_test
docker compose -f deploy/compose.prod.yml exec backup rm -f /tmp/age-identity.txt; rm -f /tmp/age-identity.txt
# post-restore against the scratch database (no poller/worker roles — no sends)
docker compose -f deploy/compose.prod.yml run --rm \
  -e DATABASE_URL=postgresql+psycopg://assistant:${POSTGRES_PASSWORD}@db:5432/restore_test \
  app python -m assistant post-restore --backup-taken-at <dump timestamp, ISO 8601 UTC>
docker compose -f deploy/compose.prod.yml exec db psql -U assistant -d restore_test -c \
  "SELECT count(*) FROM app.messages; SELECT * FROM app.coverage_gaps ORDER BY id DESC LIMIT 1;"
docker compose -f deploy/compose.prod.yml exec db dropdb -U assistant restore_test
```

Record in M02.md: dump file, size, row counts, and that the `restore` gap row
exists. This is the "backup restored into an isolated database" check.

## B. A real restore

1. `docker compose -f deploy/compose.prod.yml stop app` — nothing may send
   while the database is being replaced.
2. Decide the dump (`/backups/daily/`, or download from the remote with
   `rclone copy spaces:assistant-backups/daily/<file> /backups/daily/`).
3. Recreate the database and restore, as in A but into `assistant`:
   `dropdb`/`createdb`, then `restore.sh <file> assistant`. The script sets
   `system_state.restore_pending`; `assistant run` refuses to start roles
   while it is set.
4. `python -m assistant post-restore --backup-taken-at <ts>` (app container,
   `run --rm`): one transaction — autonomy paused for every account, open
   actions cancelled (`restored_from_backup`) and executing ones moved to
   `delivery_uncertain`, queued jobs deleted (reconcilers re-create valid
   work), schedules recomputed, a `possible` coverage gap for
   [backup_taken_at, now], the retention sweep, then `restore_pending`
   cleared. It prints what it changed.
5. `docker compose -f deploy/compose.prod.yml up -d app`. The owner receives
   the restore notice listing what to re-apply (consent, settings, pause).
6. Remove the age identity from the host.
