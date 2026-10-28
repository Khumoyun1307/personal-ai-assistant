# OPS-RT — Restore test before R1 exit

| | |
|---|---|
| **Owner** | Antony (script, evidence) + TL (server commands) · **Peer review** Neryn · **Owner** go-ahead, age key on the laptop for the run, client taps |
| **Size** | S (one small `deploy/server.sh` change + one live run) |
| **Branch** | `ops/p2-restore-verification` from `main` → PR to `main` (post-release routing, TL ruling on #66, 2026-10-28; M04 already shipped). Original pre-release route: `ops/restore-test-p2` → `int/m04`. The live run remains separately owner-approved after CP3 |
| **Depends on** | F0 (post-restore step 2 for actions), S04.4 (sends), CP3 (M04 on production with `actions.send` on) |
| **Normative inputs** | IMPLEMENTATION_PLAN §6 acceptance (restore test, NFR-09), ARCHITECTURE §15.6, `deploy/runbooks/restore.md` §A, `deploy/server.sh restore-test`, PRD NFR-09 |

## 1. Goal

Show on the real stack, before R1 exit, that restoring a backup can never cause a send: a backup taken **before** an action was approved and sent is restored into an isolated database, `post-restore` runs, and the restored state holds no sendable action, no queued job, a recorded `restore` gap and a queued owner notice that names what was cancelled (NFR-09; IMPLEMENTATION_PLAN §6 acceptance).

## 2. Scope

**In:** extend `deploy/server.sh restore-test` with content-free action checks on the scratch database; the live run procedure; evidence. **Out:** a real production restore (runbook §B); the automated scenario `tests/scenarios/test_m04_restore.py::test_a_restored_database_with_an_approved_action_sends_nothing` (QA-S4, already in the test plan) — this unit is its live counterpart.

## 3. Design

### 3.1 Isolation (how the plan's conditions are met)

| Plan condition | How |
|---|---|
| Isolated staging stack | The existing `restore-test` restores into a **scratch database** `restore_test` on the server (never over production) and drops it afterwards. Restored real data never leaves the server and is never loaded into a dev environment (ARCHITECTURE §15.6) |
| Never with the production bot token | Nothing that can call Telegram runs against `restore_test`: only `python -m assistant post-restore` and `tools/acceptance/p2_verify.py`, both in one-off containers started with `--no-deps`. Neither imports `assistant.telegram` (a unit test asserts it), so the token present in the container's environment is never used. **TL decision (spec).** |
| App started without poller and worker roles | No `assistant run` at all against `restore_test`; the automated scenario covers "reconcilers and `actions.execute` after post-restore send nothing" |

### 3.2 `deploy/server.sh restore-test` additions

After the existing `post-restore` call and before `dropdb restore_test`, run against the scratch database (URL derived inside the container, as the existing `post-restore` call does):
```
p2_verify.py release                        # alembic head, P2 tables, action counts by state
p2_verify.py actions --since <backup_taken_at − 2 days>
```
and print, content-free: count of `actions` in `proposed|awaiting_approval|revising|approved|scheduled|executing` (must be 0); count by `state_reason` (expect `restored_from_backup`); `procrastinate_jobs` in `todo|doing` (must be 0); presence of `system_state[restore_notice_pending]` with its counts (`cancelled`, `uncertain`) and cancelled action ids (ids only). The script exits 1 if any "must be 0" fails.

F0's post-restore step stores the cancelled and uncertain **action ids** in the notice payload alongside the counts; the owner notice (delivered by the running app's watchdog after a real restore) says how many replies were cancelled or are uncertain, with local times, and points to `/activity` (no message text). If F0 stored counts only, extend the payload in this unit's PR (privacy/post_restore.py is Neryn's; ask Neryn — it is a two-line change).

### 3.3 Live run (one planned window after CP3, ~30 min)

| Step | Who | Action | Expected |
|---|---|---|---|
| 0 | Owner | Go-ahead for the run in the conversation; put the age identity at `var/cloud/age-identity.txt` on the laptop (runbook §A) | — |
| 1 | Owner | In the test chat (second account as contact) get a draft card (`propose_reply`, or Assist) — leave it **awaiting approval** | `p2-verify actions` shows action A `awaiting_approval` |
| 2 | TL | `just prod-backup <ip>`; note the dump name (UTC time) | New dump in `/backups/daily` |
| 3 | Owner | Approve card A | Card shows "Sent ✓"; `p2-verify action <A>` shows approved → claimed → succeeded; `trial-check --since <today>` PASS |
| 4 | TL | `just prod-restore-test <ip>` | Restored: A `cancelled`/`restored_from_backup` (it was awaiting in the dump); 0 pending/executing actions; 0 todo/doing jobs; gap `restore/possible`; notice queued with A's id; `restore_test` dropped |
| 5 | TL | `just prod <ip> p2-verify actions --since <today>` on production | A `succeeded` once; production untouched by the test |
| 6 | Owner | Delete the age identity from the laptop again (runbook §A) | — |

Optional (if the owner wants the `executing` path too): no live step can catch an action mid-send reliably; the automated scenario and `test_post_restore.py` cover `executing → delivery_uncertain`.

## 4. Interfaces

Consumes `p2_verify.py` (OPS-TV), F0's post-restore step, `just prod-backup` / `prod-restore-test` (S02.5/S02.6).

## 5. Tests

`tests/unit/test_post_restore_imports.py::test_post_restore_and_p2_verify_never_import_telegram`; `tests/integration/test_post_restore.py::test_the_restore_notice_carries_cancelled_and_uncertain_action_ids` (if the payload is extended here); the smoke path `just smoke-backup` keeps passing (CI) with the new restore-test lines (they must tolerate a database with no actions).

## 6. Acceptance criteria

1. The live run's evidence (dump name, counts, the "must be 0" lines, gap row, notice ids, production check) is recorded the same day in `docs/milestones/M04.md` via [qa/evidence-template.md](../qa/evidence-template.md).
2. No message text appears in any output.
3. The age identity existed on the laptop only for the run.

## 7. Docs

`deploy/runbooks/restore.md` §A: the new output lines and what "pass" means; ARCHITECTURE §15.6 as built (the notice payload ids).

## 8. Risks

- **The dump does not contain action A** (backup taken before the card existed): step 1 must finish before step 2; verify A's id in the restore output.
- **The age key lingers on the laptop:** step 6 is part of the checklist and the evidence.
