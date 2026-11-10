# Restore from backup (ARCHITECTURE §15.6)

A restore rewinds the database by up to 24 h. Messages Telegram already
delivered in between are gone for good (it does not redeliver), and any
action, consent change, deletion or setting made after the dump is undone.
The mandatory post-restore step makes that honest and safe: it pauses
autonomy, quarantines restored AI consent, cancels open actions, records a
`possible` coverage gap and tells the owner. AI stays blocked until the
owner explicitly renews each consent scope in the control chat.

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

**Database roles (I24).** The backup sidecar uses `assistant_backup`, with
read/dump grants only; it cannot restore. The one-off `restore` service uses
`.env.restore` and `assistant_migrate` (database/schema owner), with no provider,
Telegram or B2 secrets. It shares the backup image and mounts dumps read-only.
Cluster administration (`createdb`/`dropdb`) stays inside `db` as the init role;
create every restore target with `-O assistant_migrate`. `pg_restore --no-owner
--no-acl` creates restored objects as that migration role. After restore run
`db-roles ... setup` for that target to reapply runtime/dump grants before
post-restore or any app startup. Role passwords are never restored from a dump;
retain/reuse `var/cloud/prod-generated.env` when rebuilding the host.

## A. The restore test (before R1; repeat after every major upgrade)

Into a **scratch** database on the server, never over production, and never
with a poller running against it:

```powershell
just prod-restore-test <ip>
```

`deploy/server.sh restore-test` takes the newest dump in `/backups/daily`,
creates `restore_test`, pipes the identity into
`dc run --rm --no-deps -T restore <dump> restore_test`, reapplies grants using
`db-roles ... setup --database restore_test`, runs `post-restore` and the explicit scratch-only `privacy-purge` fallback
against the scratch database in a one-off app container (`--no-deps`; the
scratch URL is derived **inside** the container from its own `DATABASE_URL`,
so the password never passes through the operator's shell), prints the dump
name and size, row counts and the newest coverage-gap row (`restore/possible`).
It also runs the P2 release and action views (actions since two days before
the dump), followed by a read-only restore gate. All verifier mounts are
read-only and all commands derive the scratch URL inside the container.
The scratch database is dropped on success **and on failure**.
All scratch commands use `tools.acceptance.scratch_restore`: SQLAlchemy replaces
only the URL's database component with `restore_test`, preserving driver,
credentials and query options even when query values contain slashes. URLs
with alternate `dbname`/`database` query selectors or unsupported shapes are
rejected before post-restore can run. The launcher never prints the URL.

Pass requires `pending_actions=0` (proposed, awaiting approval, revising,
approved, scheduled and executing), `pending_jobs=0` (todo and doing),
`privacy_recovery_pending=no`, `restore_notice_pending=yes`, `restore_notice_valid=yes`, and
`restore_verification=PASS`. A nonzero count or a missing/malformed notice
exits 1. Empty action tables and notices with no affected actions are valid.
The output groups fixed `state_reason` codes (`restored_from_backup` for
cancelled/uncertain actions) and prints only numeric account/action ids and
derived cancelled/uncertain counts from the notice; it never dumps notice JSON.

Record in the milestone report: dump file, size, row counts, zero-count
gates, restore gap, notice counts/action ids, and `restore_test dropped`.
For the planned P2 live test, leave a synthetic draft awaiting approval,
take the backup, then approve it before running this test. Check its id is
cancelled in scratch and still succeeded once on production afterwards.
Every production command needs the owner's separate go-ahead. Delete the
age identity from the laptop at the end. Offline tests do not complete this
live acceptance requirement.

## B. A real restore

1. Obtain the owner's authorization for the recovery window. Run
   `just prod <ip> app-stop`; review settings and pause state separately.
2. Choose the encrypted dump. Preserve the off-site journal and head: never
   sync an empty/recovered local journal over B2. The existing offline age
   identity is needed; no new encryption key or processor is introduced.
   Fence/retire an old host before a replacement can publish: there must be
   only one journal writer. No database lock is held during age/B2 calls.
3. `just prod-restore <ip> <file>` refuses a running app, waits for in-flight
   journal commits using a short database barrier, then records the database
   clock in the same transaction as its independent recovery cutoff (UTC, six
   fractional digits). After releasing the barrier it requests a final source
   journal upload, stops the sidecar, then restores and reapplies service grants.
   A failed final upload cannot be certified by an earlier scheduled snapshot,
   including one from the same second; replay reports `journal_stale_tail` and
   keeps quarantine closed. On a lost source, the cutoff includes the whole unknown
   loss interval, not the time of the last available head. Do not move it
   backwards to make an old head pass. Pre-0019 snapshots require migration
   to the current schema and manual/purge fallback (`journal_supported=false`).
4. Run `just prod <ip> post-restore <dump timestamp>`. It revokes both restored
   AI scopes, clears stale cards/questions, pauses autonomy, cancels pending
   actions, settles executing actions as uncertain, deletes queued jobs,
   skips/reschedules restored occurrences, records the gap and runs retention.
   **The startup gate remains closed after this step.** Failures leave it
   closed; fix and retry the same timestamp. Never delete the marker by hand.
5. `just prod <ip> privacy-status` prints the restore UUID and fixed state codes.
   Try `just prod-privacy-replay <ip>` with the same offline-identity stdin procedure used by
   `restore` (the server action is `deploy/server.sh privacy-replay`). It fetches
   encrypted B2 head/segment, verifies SHA-256, fully authenticates both with
   age, then pipes only ids/enums/timestamps into the offline replay command.
   Source UUIDs scope every lookup; a complete contiguous chain must include
   the dump anchor and cover the independent cutoff. Only then does replay
   atomically reapply deletions/withdrawals and open the gate. Decrypt failure,
   missing head/segment/prefix/suffix, unknown key, stale head or checksum error
   leaves it closed. No provider or Telegram call is made. The identity and
   decrypted metadata are never written to server disk or printed.
   Migration 0019 includes I8 notification snapshots in recovery: independent
   snapshot expiry and notification cascades are replayed by recovery UUID.
   Complete purge also removes their reasons; a remaining registered content
   column keeps the gate closed.
   Explicit message deletions invalidate covering summaries even if maintenance
   already expired the restored payload. Routine message expiry is recorded
   separately as `purge`; replay preserves its covering rolling summaries.
   A later explicit deletion records `delete` and removes those summaries
   before quarantine opens.
   A `journal_retention_review_required` reason means a retention setting
   changed after the backup: review/reissue the owner's latest period choices
   before completing manual reconciliation, or choose complete purge.
6. If continuity cannot be proven, choose one explicit fallback:

   - **Complete content purge:** `just prod <ip> privacy-purge <restore UUID>`.
     This clears all accounts' content through existing cascades, all raw
     payloads (including queued/unrouted ones), then runs retention. It records
     a restore/account-scoped receipt and opens only after success. Retrying
     after interruption is safe; a sweep failure leaves quarantine closed.
   - **Manual selective reconciliation:** only if the owner knows every
     relevant deletion in the interval. Reissue each with
     `privacy-delete --restore-id <UUID> --account-id <id> --scope
     <conversation|contact|managed|everything> [--subject-id <id>]`.
     The CLI validates tenant ownership and never prints content. Retain
     counts/IDs-only receipts, verify retention and cancelled/uncertain action
     receipts. To reissue known retention choices while startup is closed, use
     `privacy-retention --restore-id <UUID> --account-id <id>` with the needed
     `--messages-days`, `--control-chat-days` or `--raw-updates-days` options;
     existing policy bounds apply and no consent is granted. Then attest using
     `privacy-manual-complete --restore-id <UUID> --account-id <id>`
     (repeat the account option for **every** restored account)
     `--owner-confirmed`. Cancellation or an unknown selective history cannot
     certify reconciliation: keep quarantine or select complete purge.
     If a decision cannot be reissued through these supported scopes, use
     complete purge; never attest to selective completion while omitting it.

   Both fallbacks leave journal shipping blocked: an unknown independent tail
   cannot be overwritten by the restored chain. Retire the old-copy inventory
   under §D before resetting the journal. This does not block ordinary new
   backups or the separately authorized processing of freshly consented data.
7. After successful reconciliation, start the backup service (`dc up -d backup`)
   and app. The restore notice lists cancelled/uncertain actions and the gap.
   AI remains blocked until fresh deterministic `/start` and `/privacy managed`
   grants, separately per scope/account. Reconciliation never renews consent;
   consent alone cannot reconcile a restore. Restored pending/failed raw
   updates were discarded to prevent erased text or old consent replies being
   reprocessed. Review settings, pauses and coverage gaps with the owner.
8. Remove the temporary laptop identity copy using the established owner
   procedure. Record replay RPO/RTO and manual/purge RPO/RTO separately in A4;
   no unattended-recovery claim follows from a maintenance-only rehearsal.
## C. Rebuilding a lost host

Provision a fresh host (`provision.md` B2–B5), then, **before starting the
backup sidecar**: an empty backups volume triggers an immediate dump and
off-site sync. Missed daily schedules also catch up at startup rather than
waiting for the next `BACKUP_HOUR_UTC`.

0. If the old `alviro-data` block volume survived, attach it instead of a
   new one: `prepare` never formats a device with any signature, keeps its
   data and writes the sentinel only once the volume is verified mounted.
   Reconcile roles through the I24 upgrade in `deploy.md` if that volume
   predates service separation; retain its original init password.
   The database on it is then intact: skip steps 1–3 and run `just deploy
   <ip>`. Either way, every `deploy/server.sh` action that starts, backs up
   or restores the stack refuses until the volume is mounted with its
   sentinel (`just prod-status <ip>` → `sentinel present`; ARCHITECTURE
   §15.4 mount guard).

1. On the server: `dc build migrate backup && dc up -d --wait db`, then
   `dc run --rm --no-deps -T db-roles python -m assistant.db.production_roles setup`
   (use the selected release image, per `deploy.md`) — keep the backup sidecar and
   app stopped while recovering. An app
   polling into the empty database would consume the updates Telegram has
   queued (up to 24 h), which the restore would then overwrite.
2. Fetch the off-site history into the new, empty backups volume, so the
   first sync mirrors the full set instead of deleting it:
   `dc run --rm --no-deps --entrypoint bash backup -c 'rclone copy "$BACKUP_REMOTE/daily" /backups/daily && rclone copy "$BACKUP_REMOTE/weekly" /backups/weekly'`.
3. Restore with a one-off container while the sidecar stays stopped (the
   `just prod-restore` wrapper in §B requires a running sidecar). First
   recreate the target database on the server:
   `dc exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O assistant_migrate "$POSTGRES_DB"'`.
   Stream the age identity from the laptop over SSH into the following
   server command's stdin, substituting the chosen dump name for `<file>`:
   `dc run --rm --no-deps -T --entrypoint sh restore -c 'exec restore.sh "$1" "$PGDATABASE"' sh '/backups/daily/<file>'`.
   The identity must never be written to the server's disk. Reapply grants:
   `dc run --rm --no-deps -T db-roles python -m assistant.db.production_roles setup`.
   Run §B step 4
   (`post-restore`), then `just deploy <ip>` (starts migrate, the app and
   backup sidecar) and §B step 6. The first new dump catches up
   immediately; do not copy success/attempt markers from the old host to
   suppress it. On an ordinary restart with the original backups volume,
   keep those markers: `last_success` avoids duplicate catch-up dumps, and
   `last_upload_attempt` plus `last_upload_success` resume pending off-site
   retries at hourly intervals without repeating a successful dump.

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

## D. Journal retention and final erasure

The journal is retained privacy enforcement metadata. It is not covered by the
14-daily/4-weekly dump deletion glob, and a nominal 35-day expiry is never enough
proof. Do not delete tombstones while any restorable copy can resurrect data.
Inventory local daily/weekly dumps, B2 daily/weekly objects, offline copies,
provider snapshots and any copied scratch/recovery databases **weekly**. Keep
counts, UUID receipts and timestamps only, never content. Unknown copy inventory
blocks pruning. Existing B2 hard-delete configuration must remain in force.

This implementation uses conservative whole-epoch pruning, not guessed
per-record expiry. Within 7 days after independently verifying destruction of
the last restorable copy of an old epoch:

1. Stop app and backup. Ensure the restore/privacy gate is already reconciled.
2. Verify **all** old restore points (including late/offline copies, retained B2
   versions and snapshots of journal tables) have been destroyed. This step
   sacrifices those recovery points; do not attest based only on their age.
   Retain an external, content-free inventory receipt UUID.
3. Remove that epoch's local encrypted `privacy-journal/segments/`, `head.age`
   and corresponding B2 `privacy-journal/segments/` and `head.age`. Use scoped
   rclone deletion only after inspecting the inventory; never broad bucket
   deletion. Remove obsolete local journal success/attempt markers too.
4. Run `just prod <ip> privacy-journal-reset --inventory-receipt <receipt UUID>
   --copies-destroyed`. It refuses a running app/sidecar or unresolved restore.
   It deletes local decisions, removes erased-account heads, resets surviving
   heads and records the receipt under existing one-year audit retention.
   No processor grant is renewed. Final account erasure includes this cleanup
   once its last restorable copy is destroyed.
5. Restart, take a new backup and verify acknowledgements. Never reset a live
   epoch while keeping a restorable old
   copy; unknown inventory means keep tombstones and shipping quarantine.

`last_journal_upload_success` means both the immutable ciphertext and encrypted
head were acknowledged by the existing B2 remote. `journal_status` is a fixed,
content-free state; missing/stale acknowledgement means independent durability
is pending. Shipping retries on each backup-loop check independently of the
once-daily dump, using `privacy-journal/last_attempt` on the persistent volume;
a restart cannot lose the outbox or pretend a failed head upload succeeded.
Journal segments upload normally once written, without checking policy versions,
publication or owner notices (owner decision, 2026-11-10). The owner handles the
privacy policy personally; recovery does not wait for it. `recovery_blocked`
means a restore or unresolved old-copy inventory prevents a rewound chain from
overwriting the independent head. Resolve that state through replay or the
fallback/epoch-retirement steps above; never clear its markers to force shipping.
