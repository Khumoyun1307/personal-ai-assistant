# A4 operational rehearsal — local part (placeholder secrets, synthetic data)

Date: 2026-11-17. Branch `stabilize/a4-rehearsal` from `main` f83c0fa. Prepared by an engineer agent for the lead.
**Status: local rehearsal prepared and run. A4 is not done.** The production part below still needs the owner's authorization and a planned window.

Nothing here touched production, the production or dev bot, a real LLM key, the B2 bucket, `.env`, `var/`, backups or content-bearing rows. Every stack ran under its own Compose project (`st-a4-smoke`, `st-a4-smoke-guard`, `st-a4-recovery`, `st-a4-restore`) with generated placeholder passwords, a throwaway age key and a local rclone remote standing in for B2. Each was removed with `down -v` (containers and volumes). Only `restore-rehearsal.sh` also removes its generated image tags; the other two leave their shared tags (defect 3). The app process (poller, workers, system tasks) never started, so no Telegram or provider call was possible.

**Fix round, 2026-11-18 (review of PR #284).** Changes:
- A restored D-S17 warning card (OUTDATED with an approval token) was cancelled and its token cleared, but it was left out of the restore notice. `post_restore.py` now includes it in the notice; a regression test was added.
- The rehearsal seeds that warning card and checks every seeded card by id.
- Recovery commands run as `assistant_app`.
- The gate check is stricter.
- Account 1 gets fresh cards before the second dump.
- Project-name and results-path guards were added.

The restore-rehearsal numbers in section 2 come from a re-run after these fixes (2026-11-18). The backup-smoke and recovery-smoke numbers are from 2026-11-17; those suites were not changed in this round.

## How to run it

```powershell
just rehearse-a4 [results.tsv]   # all three suites, timed; A4_SUITES / A4_PROJECT_PREFIX optional
just rehearse-restore            # restore + privacy reconciliation part only
```

| File | Role |
|---|---|
| `deploy/smoke/a4-rehearsal.sh` | Runs `backup-smoke.sh`, `recovery-smoke.sh` and `restore-rehearsal.sh`, each in its own Compose project. Writes one content-free TSV row per step or check (`suite  check  PASS/FAIL  ms`). |
| `deploy/smoke/restore-rehearsal.sh` | New. Runs the production stack with synthetic owner state. It backs up and ships the journal, then makes owner decisions and completes a send *after* the dump. It then replaces the database using `deploy/server.sh restore`'s commands in the same order (barrier + full-precision cutoff, final journal upload, drop/create, `restore.sh` with the cutoff, `db-roles setup`), followed by `post-restore`. Scenario 1 uses journal replay; scenario 2 uses a stale tail with the complete-purge fallback. |
| `deploy/smoke/rehearsal_state.py` | New. Seeds synthetic state: per account, plus one card set per round, with one card per state and a D-S17 warning card. It applies the post-backup decisions and checks the restored-state invariants for each seeded card id. Runs in a one-off container of the production app image. Seeding uses the database owner; checks run as `assistant_app`. Prints fixed check names and ids only. |
| `deploy/smoke/recovery-smoke.sh` | Three changes. The project name can be overridden (`RECOVERY_PROJECT`). The readiness timeout can be overridden (`RECOVERY_HEALTH_TIMEOUT_SECONDS`, default 5). The backup Dockerfile path now goes through `host_path` (Windows fix, defect 1). |
| `deploy/smoke/backup-smoke.sh` | Unchanged; reused through its existing `SMOKE_PROJECT` override. |

The host was running seven or eight other engineers' stacks during the run. Durations are wall-clock times on that loaded laptop. Most of each duration is `docker compose run` container start-up (roughly 30–100 s per one-off container), not the restore work itself. Treat them as an upper bound for the procedure on a tiny database, not as production RTO.

## 1. Inventory: what is already proven, and what only a rehearsal can show

| Scenario | Already proven by automated tests / smoke | Only a rehearsal can show |
|---|---|---|
| Backup / upload failure | `backup-smoke.sh` (CI): failing dump records nothing; upload failure exits 3 and writes no marker; delete guard; catch-up of a missed schedule across restarts; hourly upload retry across restarts; journal head failure is not acknowledged and the retry is throttled across restarts. `tests/unit/test_backup_script.py`. I26 `test_service_monitoring*` (missing/stale backup markers make the service degraded). | Real B2 credentials and network failure modes. Whether the owner actually receives the `backup_upload_stale` / degraded alert on a real host. Real dump size and duration. |
| Safe restore | `test_post_restore.py` (including the new `test_post_restore_retires_a_restored_outdated_warning_card`), `scenarios/test_m04_restore.py` (a restored approved action sends nothing), `test_restore_verify.py`, the restore step of `backup-smoke.sh`. | The restore sequence on the real images, with recovery commands as `assistant_app` and the restore itself as `assistant_migrate`, against a real dump, with wall-clock RTO. It reproduces `server.sh`'s commands; drift is listed under Limitations. **New local rehearsal: scenarios 1 and 2.** |
| Privacy reconciliation | `test_privacy_journal.py` (replay is idempotent, invalid journal keeps quarantine, purge-sweep failure and retry, manual reconciliation scoped to restore/account), `test_restore_consent.py`, journal parts of `backup-smoke.sh`. | Replay through the real encrypted fetch pipe (`privacy-journal.sh fetch` into `recovery_cli replay`) against a dump's anchors and an independent cutoff. Fallback timing. **New local rehearsal: scenarios 1 and 2.** Production: the real B2 journal and the offline key. |
| Failed release | `recovery-smoke.sh` (CI when deploy/migrations/db change): expand-compatible failure recovers the previous image; breaking failure stops the services and keeps the marker and the forward-fix text. `tests/unit/test_migration_compatibility.py`, `test_release_deploy.py`, `test_server_sh.py`, `release-smoke.ps1`. | Real Alembic DDL, procrastinate drain and the app healthcheck on the arm64 host. The SSH/CI-gate path. Owner-visible downtime. |
| Provider outage | `test_agent_runtime.py::test_a_provider_outage_is_reported_and_nothing_crashes`, `::test_a_reached_monthly_budget_means_no_model_call`, `test_assist_drafting.py::test_refused_or_failed_drafts_clear_the_flag_and_are_audited`, `test_service_monitoring.py` (failed LLM jobs inside the window, queued-job age, alert once then recover). | A real OpenAI outage or 5xx/timeout shape, and whether the owner sees an honest degraded reply and the I26 alert. **Not reproducible locally without a provider key; tests only.** |
| Card uncertainty | `scenarios/test_m04_r1_at_most_once.py` (restart between claim and send is uncertain and never resent; network timeout is uncertain and not retried), `test_mark_uncertain.py`, `test_card_sync.py` (H7: an uncertain edit or unresolved presentation is never replayed), `test_approvals.py::test_uncertain_full_text_is_not_replayed_by_a_fresh_presenter` and `::test_full_text_success_then_card_refusal_never_replays_full_text`. The restore rehearsal adds: a restored `executing` card becomes `delivery_uncertain`. | Real Telegram timeout behaviour on a card edit and the owner-visible rendering. Production/dev-bot only. |
| Worker restart | `test_jobs.py` (a job left `doing` by a dead worker is retried; a stalled job holding a queueing lock is failed, not re-collided; a deletion cut off by shutdown is re-queued), `test_process.py` (SIGTERM during a send records the outcome before exit), `test_agent_runtime.py::test_an_interrupted_run_is_marked_and_the_turn_is_answered`, restart journeys (`test_j15_*_after_restart`, `test_i01_j01_dictation_survives_recipient_answer_and_restart`, `test_j14_two_interruptions_*`, `test_h2_*_after_restart`, `test_j21_*_after_restart`, `test_h5_*_binds_across_restart`). `recovery-smoke.sh` covers drain plus worker-heartbeat readiness on a synthetic app. | The real `stop_grace_period` drain and `restart-timing` on the host, using production `just prod <ip> restart-timing`. **Starting the real app locally needs a bot token, so: tests only.** |

## 2. Local runs and measured results (2026-11-17)

### Relevant automated tests

`verify.sh a4-rehearsal` covered the test files and node ids listed in the inventory, plus `test_migrations.py`. Results: ruff clean, mypy clean (248 files), import contracts 5 kept / 0 broken, **pytest exit 0**. About 450 tests ran; the `x` results are the A1 journey cases strictly marked as pending contracts. The release unit tests (`test_migration_compatibility.py`, `test_release_deploy.py`, `test_server_sh.py`) and `release-smoke.ps1` were not re-run locally; there is no `pwsh` on this host. CI runs them.

Fix round (2026-11-18): `verify.sh` on `test_post_restore.py` (including the new warning-card test, which failed on its notice assertion before the `post_restore.py` change), `scenarios/test_m04_restore.py`, `test_restore_verify.py`, `test_post_restore_imports.py`, `test_action_lifecycle_bulk.py` and `test_restore_consent.py`: pytest exit 0, 42 passed. mypy clean, import contracts 5 kept. `ruff check .` and `ruff format --check .` clean after one PT018 fix in the new test. Scripts are shellcheck clean.

### Backup / upload failure — `backup-smoke.sh` (project `st-a4-smoke`): **PASS**, 27.0 min in total

| Step | Result | Wall time |
|---|---|---|
| Data-volume guard: no volume, nothing starts | PASS | 109 s |
| `backup.sh once`: encrypted dump in a volume owned by `postgres`; `last_success` written; no upload marker | PASS | 65 s |
| Decrypt + `pg_restore` into a scratch database; `restore_pending` set | PASS | 51 s |
| A failing `pg_dump` records nothing (exit 1, no `.part`, markers unchanged) | PASS | 52 s |
| Off-site sync mirrors rotation | PASS | 77 s |
| Delete guard refuses a sync that would delete 9 remote dumps; no marker | PASS | 61 s |
| Upload failure: exit 3, dump kept, no upload marker | PASS | 26 s |
| Missed schedule catches up once across restarts | PASS | 52 s |
| A 24-hour-old or missing marker triggers an immediate dump | PASS | 25 s |
| A pending upload retries hourly across restarts without repeating the dump | PASS | 93 s |
| The newest completed dump triggers an upload even without its success marker | PASS | 31 s |
| Journal: head failure not acknowledged; throttle survives restart; recovery | PASS | 167 s |
| A failed final journal upload cannot certify an earlier snapshot (`journal_stale_tail`) | PASS | 127 s |
| Ordinary dumps keep exact journal anchors; invalid key, late-auth and checksum failures release no plaintext | PASS | 386 s |

Limitations: the remote is a local rclone directory, so B2 errors, credentials and hard-delete are not exercised. Alert delivery to the owner is shown only by I26 tests.

### Failed release — `recovery-smoke.sh` (project `st-a4-recovery`)

**PASS** on the third attempt, with `RECOVERY_HEALTH_TIMEOUT_SECONDS=90`. Suite total 25.5 min, including image builds and the deliberate readiness timeouts.

| Case | Result | Wall time |
|---|---|---|
| Expand migration, then candidate health failure: candidate stopped, previous image serves HTTP on the new schema, release state unchanged, marker cleared | PASS | 426 s |
| Breaking migration, then health failure: app and backup stopped, marker kept, recovery dump name recorded, forward-fix procedure printed | PASS | 1070 s |

Earlier attempts:
- **Attempt 1:** failed on the Windows path bug (defect 1 below).
- **Attempt 2:** failed in the expand case with `expand recovery stayed incomplete`. The rehearsal's 5-second `RELEASE_HEALTH_TIMEOUT_SECONDS` also bounds the *recovered previous image's* readiness wait. On the loaded host the previous image could not report ready within 5 s, so `release_failed` correctly fell back to the forward-fix stop. That is fail-closed behaviour in the product, not a defect, but it makes the fixture timing-sensitive (defect 2 below).

Limitations (unchanged from runbooks/deploy.md): the app, migrations and worker heartbeats are synthetic, and source selection and build are stubbed. It does not prove real Alembic DDL compatibility or the real app healthcheck.

### Safe restore + privacy reconciliation — `restore-rehearsal.sh` (project `st-a4-restore`): **PASS**, 24/24 timed checks, 42/42 invariant checks (re-run 2026-11-18 after the fix round)

The host was less loaded than on 2026-11-17: setup took 4.4 min, against 14.1 min on 2026-11-17. On the earlier run, under heavy load, scenario 1 RTO was 17.4 min and scenario 2 RTO was 16.9 min, also all passing (that run predates the fix round).

Synthetic state:
- Per account: AI control and managed consents granted, two chats with one message each, and one queued procrastinate job.
- Per card round, on its own chat: one card in each action state (13) plus a D-S17 warning card (OUTDATED, approval token, freshness warning).

Seeding uses the database owner. Every recovery command and check runs as `assistant_app`. After the dump: managed consent is revoked, one chat is deleted through `delete_conversation_data`, and the seeded `awaiting_approval` card is approved and completed (`succeeded`).

Each invariant check covers 14 items:
- Gate open, and no pending/executing action.
- Six seeded card ids, each checked for state, `restored_from_backup`, no token, no warning-card approval path, and being named in the notice:
  - `proposed`, `awaiting_approval` (the card sent after the backup), `approved`, `scheduled` and `warning` must be cancelled.
  - `executing` must be `delivery_uncertain`.
- No approval token left on the account.
- `ai_control` and `ai_managed` not granted.
- No queued job.
- Post-backup deletion applied.
- Unrelated chat kept (replay) or all content cleared (purge).

**Scenario 1 — journal replay (final upload acknowledged)**

| Check | Result | Wall time |
|---|---|---|
| Backup with off-site sync | PASS | 31 s |
| Journal shipped, `journal_status=acknowledged` | PASS | 24 s |
| Barrier + cutoff + final journal upload before replacement | PASS | 26 s |
| Database replaced from the dump (drop/create, `restore.sh` with cutoff, `db-roles setup`); gate closed | PASS | 73 s |
| `post-restore` (app role); gate still closed | PASS | 86 s |
| `privacy-journal.sh fetch` into `recovery_cli replay` (app role); gate opens | PASS | 77 s |
| Invariants, app role | PASS (14/14) | 43 s |
| `tools.acceptance.restore_verify` (app role): `pending_actions=0`, `pending_jobs=0`, valid notice, `privacy_recovery_pending=no` | PASS | 20 s |
| **RTO, failure to verified** (local, tiny database; not the production definition) | **5.5 min** | 328 s |
| Repeated replay is a no-op (`privacy_replay_not_required`) | PASS | 35 s |
| Journal shipping resumes after replay (`acknowledged`) | PASS | 18 s |

**Scenario 2 — stale journal tail, complete-purge fallback**

Account 2 is seeded, and both accounts get a fresh card round before this dump, so account 1's checks cover cards restored by this restore.

| Check | Result | Wall time |
|---|---|---|
| Backup with off-site sync | PASS | 16 s |
| Final head upload fails (rclone stub): non-zero exit, `journal_status=head_upload_failed` | PASS | 14 s |
| Restore + `post-restore`; gate closed | PASS | 122 s |
| Replay refuses with `blocked: journal_stale_tail`; gate stays closed | PASS | 89 s |
| `recovery_cli purge --restore-id <current>` (app role); gate opens | PASS | 113 s |
| Invariants, account 2 (app role) | PASS (14/14) | 35 s |
| Invariants, account 1 with fresh round-2 cards (app role) | PASS (14/14) | 40 s |
| `restore_verify` | PASS | 24 s |
| **RTO, failure to verified (fallback)** (local) | **7.5 min** | 449 s |
| Shipping stays blocked afterwards (`recovery_blocked`): the old independent chain is not overwritten | PASS | 22 s |

RPO in this rehearsal was the gap between the dump and the simulated failure: 1.4 min (scenario 1) and 0.7 min (scenario 2) on the 2026-11-18 run. Business data written in that gap is lost by design. The journalled post-backup *deletion* was not lost in scenario 1: the deleted chat's content is gone after replay while the other chat's content is kept. A journalled *revocation* cannot be told apart here, because `post-restore` withdraws every restored AI consent anyway. In scenario 2 the decisions could not be proven, so the purge erased all restored content instead. The production RPO bound comes from the schedule (daily at `BACKUP_HOUR_UTC`, with immediate catch-up after downtime or a missing/24-hour-old marker; backup-smoke shows this). The bound is ≤ 24 h plus the time to detect a missed upload, against the NFR-09 target of ≤ 24 h. Privacy decisions have a shorter RPO: the journal-shipping retry interval.

Acceptance properties observed locally:
- **No restored send.** Each seeded card is checked by id (`card_<state>_*` checks):
  - The card that was approved and sent after the dump came back `cancelled` (`restored_from_backup`).
  - The other pending cards and the D-S17 warning card came back `cancelled`.
  - The `executing` card came back `delivery_uncertain`.
  - None of them keeps a token or an approval path, and each is named in the restore notice's cancelled/uncertain lists in `system_state`.
- **No revoked processing.** Both AI scopes are not granted after replay and after purge. Replay prints `privacy_reconciliation_complete; fresh_consent_required` (the replay output is echoed into the rehearsal log), and purge prints `privacy_purge_complete; fresh_consent_required; journal_shipping_blocked`.
- **No silently lost work.** Queued jobs were dropped by `post-restore`, as designed: reconcilers re-create valid work. The restore notice lists the cancelled and uncertain cards, and `restore_verify` checks it. The coverage gap row is written by `post-restore`; this rehearsal did not check it separately (`restore-test` prints it).
- **No ambiguous-send replay.** An uncertain card is never re-executed (tests above). The rehearsal shows that the restored executing card is settled as uncertain, not re-queued.

Limitations:
- The real `deploy/server.sh` was not executed. It needs the server `.env`, `findmnt` and the data volume. The rehearsal reproduces its restore commands in the same order, and its mount guard is exercised by backup-smoke. Known drift from `server.sh restore`:
  - The final journal upload runs as a one-off `compose run` of the backup image. On the server it is `compose exec` in the running sidecar, followed by `compose stop backup`; the sidecar never runs here.
  - `server.sh`'s guards are skipped: `require_data_volume`, the running-app refusal, the dump-name check and the stdin identity check.
  - `BACKUP_REMOTE` is a local rclone remote (`rehearsal:/backups/remote`), not the B2 remote.
  - Recovery commands use `assistant_app`, as on the server. Before the 2026-11-18 fix round they ran as the database superuser, so a missing runtime grant could have passed unnoticed.
- Manual selective reconciliation (`privacy-delete` + `privacy-manual-complete`) is not rehearsed; only replay and complete purge are.
- RTO definitions differ. Local "failure to verified" runs from the simulated failure to the passing restore checks; the app never starts. The production checklist's RTO runs from app-stop to app healthy. The two are not directly comparable.
- The app process never started, so "zero provider calls while unresolved" relies on `test_restore_consent.py` and the I19 startup gate tests.
- The database is a few dozen rows, so durations say nothing about a production-size `pg_restore`.
- The remote is local, not B2.
- The independent journal shares the same Docker volume as the dumps here; in production it is in B2, off the host.

## 3. Defects and observations for the lead

1. **Fixed (script bug):** `recovery-smoke.sh` built the synthetic backup image with an unconverted `-f /tmp/...` path. It failed on Windows/Git Bash, so `just smoke-recovery` could not run on the owner's machine. The path now goes through `host_path`, matching the other `docker build` arguments.
2. **Fixed (script robustness):** `recovery-smoke.sh` hard-coded a 5 s readiness timeout. It applies to the recovery wait as well as the candidate, so the expand case fails on a slow host. The timeout is now overridable with `RECOVERY_HEALTH_TIMEOUT_SECONDS`; the default stays 5, so CI is unchanged. For the lead: in production the same `RELEASE_HEALTH_TIMEOUT_SECONDS` (default 180 s) bounds both waits. A slow recovered image therefore ends in the forward-fix stop, not a running service. That is safe, but worth knowing when choosing the production value.
3. **Observation, no fix:** `backup-smoke.sh` and `recovery-smoke.sh` tag shared images (`assistant:prod`, `assistant-backup:prod`, `assistant:i23-rehearsal-base`). Two engineers running them concurrently from different worktrees can overwrite each other's images. `restore-rehearsal.sh` uses per-project tags and removes them afterwards.
4. **Observation:** `rehearsal_state.py` had to import every model module (as `migrations/env.py` does) before ORM writes. `assistant.db.models` alone leaves `actions.run_id → agent_runs` unresolved. Operator tools that write through the ORM need the same imports. Existing CLIs are unaffected.
5. **Fixed (product, minor; found in review):**
   - What happens: `post_restore` already cancels restored D-S17 warning cards (OUTDATED with an approval token or freshness warning) and clears their tokens, because `cancel_pending` includes `warning_card_filter()`. But its restore-notice query selected only PENDING/executing rows, so a cancelled warning card was missing from the owner's restore notice.
   - What it was not: the suspected "restored warning card stays approvable" was not reproduced. The new test showed the token cleared, the state `cancelled` / `restored_from_backup`, and `approve` with the old token returning `NOT_ACTIVE`; only the notice assertion failed.
   - Fix: the notice query uses the same filter. Regression: `tests/integration/test_post_restore.py::test_post_restore_retires_a_restored_outdated_warning_card`.
6. Optional: add `restore-rehearsal.sh` to the CI `deploy-scripts` job under the same `recovery-changes` condition. It would give continuous restore/replay evidence. Not done here because it adds CI time; this is the lead's call.

## 4. Production-only remainder (checklist; NOT executed)

Every item needs the owner's specific go-ahead and a planned window (AGENTS §9; runbooks). Record content-free outputs only. Measure replay and purge RPO/RTO separately (runbooks/restore.md §B.8).

**Preconditions**
- [ ] The owner authorizes the window, the release SHA (I22 exact-SHA gate, CI green) and every step below.
- [ ] Deploy the candidate containing I19–I26 and this branch: `just deploy <ip> <sha>`. Confirm that `deploy/release-state` now exists (`just prod-status <ip>`). Update `docs/RELEASE.md`.
- [ ] The owner restores the offline age identity to `var/cloud/age-identity.txt` for the window only, and deletes it afterwards.

**Backup / upload failure**
- [ ] `just prod-backup <ip>`: record the dump name, size, duration and exit code (0 or 3).
- [ ] `just prod <ip> offsite-list`: newest dump present in B2.
- [ ] Optional controlled upload failure: invalid remote in a one-off run only, never by editing `.env`. Confirm exit 3 and the stale-upload alert / degraded `service-status`, then confirm recovery on the next hourly retry.
- [ ] `just prod-status <ip>`: `last_success`, `last_upload_success`, `last_journal_upload_success`, `journal_status=acknowledged`.

**Safe restore (scratch) — measures production-size restore time**
- [ ] Optional first: leave a synthetic draft awaiting approval, take a backup, then approve it (runbooks/restore.md §A).
- [ ] `just prod-restore-test <ip>`: record the dump, size, row counts, `pending_actions=0`, `pending_jobs=0`, `privacy_recovery_pending=no`, a valid notice, `restore_verification=PASS`, `restore_test dropped`, and the total duration. Check that the synthetic draft is cancelled in scratch and succeeded exactly once in production.

**Real restore with privacy reconciliation (only if the owner chooses a full drill; rewinds production by up to 24 h)**
- [ ] `just prod <ip> app-stop`, then `just prod-restore <ip> <file>`. Record the cutoff time and whether the final journal upload succeeded.
- [ ] `just prod <ip> post-restore <ts>`, then `just prod <ip> privacy-status`. Record the restore UUID.
- [ ] `just prod-privacy-replay <ip>`. On `journal_stale_tail`, the owner chooses `privacy-purge` or manual reconciliation. Manual selective reconciliation was not rehearsed locally.
- [ ] After a purge or manual fallback, journal shipping stays `recovery_blocked` on purpose: the old independent chain must not be overwritten. To recover it:
  1. Follow runbooks/restore.md §D: inventory every restorable copy and retire the old epoch.
  2. Run `just prod <ip> privacy-journal-reset --inventory-receipt <UUID> --copies-destroyed` with app and backup stopped.
  3. Restart, take a backup, and check `journal_status=acknowledged` in `just prod-status <ip>`.
- [ ] Start the backup service and the app. The owner confirms the restore notice, the fresh `/start` and `/privacy managed` consents, and the coverage gap.
- [ ] Record RPO (dump time to failure) and RTO (app-stop to app healthy) for replay, and separately for the fallback. This production RTO is defined differently from the local "failure to verified" figure.

**Failed release**
- [ ] The owner approves a deliberate failing-candidate drill, or accepts the CI/local evidence instead. A real drill needs a reviewed throwaway SHA on `main`, so the default recommendation is: accept `recovery-smoke` plus one real `just prod-rollback <ip>` across an expand-only head difference.
- [ ] Record `prod-rollback` duration, the health result and the release-state update.

**Provider outage**
- [ ] Optional: in a window, with the owner, briefly push a settings override that points the provider route at an unreachable model id. Confirm the honest degraded reply in the control chat and the I26 failed-LLM-job alert after the sustained window, then revert. Otherwise accept the test evidence.

**Card uncertainty**
- [ ] Production cannot safely induce a Telegram timeout. Accept the test evidence. During the restore drill, confirm that an `executing` card restored as `delivery_uncertain` is listed in the notice and never re-sent.

**Worker restart**
- [ ] `just prod <ip> restart-timing`: record the seconds until healthy. Then `just prod-status <ip>`: the `service-status` indicators are back to healthy, and no stuck queued jobs or unanswered turns.
- [ ] Optional: `just prod <ip> app-stop` during an idle period, then `app-start`; confirm the restart and the pending-turn recovery.

**Close-out**
- [ ] The owner deletes the laptop identity copy.
- [ ] The lead records the measured values against NFR-09 (RPO ≤ 24 h, RTO ≤ 4 h) in A4, and A5 reviews them.
