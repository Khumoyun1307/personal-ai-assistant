# Restore from backup (ARCHITECTURE §15.6)

A restore rewinds the database by up to 24 h. Messages Telegram already
delivered in between are gone for good (it does not redeliver), and any
action, consent change, deletion or setting made after the dump is undone.
The mandatory post-restore step makes that honest and safe: it pauses
autonomy, cancels open actions, records a `possible` coverage gap and tells
the owner.

**The age identity (private key) never touches the server's disk.** It is
streamed from the laptop over SSH into `restore.sh`'s standard input
(`AGE_IDENTITY_FILE=-`, age reads identities from `-`), so there is nothing
to copy into the container, nothing to remove and nothing to shred. For the
duration of a restore the owner puts the key back at
`var/cloud/age-identity.txt` on the laptop (from the password manager) and
deletes it again afterwards.

All server commands run in `/opt/assistant` as `deploy`, and every compose
call passes `--env-file .env` (the project directory is `deploy/`; without
it the root `.env` is not used for interpolation). Below,
`dc` stands for `docker compose --env-file .env -f deploy/compose.prod.yml`.

## A. The restore test (before R1; repeat after every major upgrade)

Into a **scratch** database on the server, never over production, and never
with a poller running against it:

```powershell
just prod-restore-test <ip>
```

`deploy/server.sh restore-test` takes the newest dump in `/backups/daily`,
creates `restore_test`, pipes the identity into
`dc exec -T backup restore.sh <dump> restore_test`, runs `post-restore`
against the scratch database in a one-off app container (`--no-deps`; the
scratch URL is derived **inside** the container from its own `DATABASE_URL`,
so the password never passes through the operator's shell), prints the dump
name and size, row counts and the newest coverage-gap row
(`restore/possible`), then drops `restore_test`.

Record in the milestone report: dump file, size, row counts, and that the
`restore` gap row exists. This is the "backup restored into an isolated
database" check.

## B. A real restore

1. `just prod <ip> app-stop` — nothing may send while the database is being
   replaced.
2. Decide the dump (`dc exec backup ls /backups/daily`, or the off-site copy:
   `just prod <ip> offsite-list`, then on the server
   `dc exec backup bash -c 'rclone copy "$BACKUP_REMOTE/daily/<file>" /backups/daily/'`).
3. `just prod-restore <ip> <file>` (e.g. `20261022T020000Z.dump.age`):
   refuses while the app runs, drops and recreates the `assistant` database,
   streams the identity into `restore.sh` and prints the `post-restore`
   timestamp. The restore sets `system_state.restore_pending`;
   `assistant run` refuses to start roles while it is set.
4. `just prod <ip> post-restore <ts>` (the dump name is the UTC time, e.g.
   `20261022T020000Z` → `2026-10-22T02:00:00Z`): one transaction — autonomy
   paused for every account, open actions cancelled (`restored_from_backup`)
   and executing ones moved to `delivery_uncertain`, queued jobs deleted
   (reconcilers re-create valid work), schedules recomputed, a `possible`
   coverage gap for [backup_taken_at, now], the retention sweep, then
   `restore_pending` cleared. It prints what it changed.
5. `just prod <ip> app-start`. The owner receives the restore notice listing
   what to re-apply (consent, settings, pause).
6. The owner deletes `var/cloud/age-identity.txt` from the laptop again.

## C. Rebuilding a lost host

Provision a fresh host (`provision.md` B2–B5), then, **before the first
backup runs** (it is daily at `BACKUP_HOUR_UTC`):

1. On the server: `dc build && dc up -d db backup` — **not** the app: an app
   polling into the empty database would consume the updates Telegram has
   queued (up to 24 h), which the restore would then overwrite.
2. Fetch the off-site history into the new, empty backups volume, so the
   first sync mirrors the full set instead of deleting it:
   `dc exec backup bash -c 'rclone copy "$BACKUP_REMOTE/daily" /backups/daily && rclone copy "$BACKUP_REMOTE/weekly" /backups/weekly'`.
3. Restore as in §B steps 3–4, then `just deploy <ip>` (starts migrate and
   the app) and §B step 6.

Guard: `backup.sh` refuses an off-site sync that would delete more than
`BACKUP_MAX_REMOTE_DELETE` (default 7) remote dumps in one go, writes no
upload marker, and the app's daily `backup_check` alerts the owner that the
off-site copy is stale. If the larger deletion is intended (for example the
owner asked for everything older to go), run it once with
`dc exec -e BACKUP_MAX_REMOTE_DELETE=100 backup backup.sh upload`.

**After a long upload outage the guard stays tripped.** While uploads fail,
local rotation keeps deleting one daily dump per day (and a weekly one per
week) that is still off-site. Once the outage has lasted more than
`BACKUP_MAX_REMOTE_DELETE` days (default 7), catching up would delete more
than that many remote dumps, so every later sync is refused, the hourly
retries keep failing, and the `backup_upload_stale` alert repeats — this does
not clear by itself. When the cause of the outage is fixed (and this is not a
rebuilt host with an empty `/backups`, the case above), check with
`just prod <ip> offsite-list` that the remote holds only older dumps than the
local `daily/`, then run the one-off override above; the next scheduled sync
is back to normal.
