# OPS-RT3s — Restore-test P3 check script (junior unit)

| | |
|---|---|
| **Unit** | OPS-RT3s — the read-only P3 restore check (`tools/acceptance/p3_restore_check.py`): after `post-restore`, every delegation that was active is ended with `restored_from_backup`, nothing it owned stays live, no autonomous reply from before the restore is still pending, nothing acts for a delegation the restore ended, and autonomy stays paused until the owner resumes it. It runs inside the existing scratch restore test and as `p3-verify restore-check`; plus the P3 section of `deploy/runbooks/restore.md` |
| **Owner slot** | D2 (David's team, junior) |
| **Mentor** | SQ (Codex; peer review dispatched by the TL) |
| **QA** | QD1, after merge (on `int/p3` before R1). SQ runs it live with the owner in LIVE-P3 (the OPS-RT3 run, D+8) |
| **Size** | S (≈ 0.5 engineer-day, one fix round included) |
| **Depends on** | F0-S (post-restore step 2b: `end_delegations(..., reason='restored_from_backup')`, the notice payload's `delegations_ended`; `p3_factories`); F0-E (`tools/acceptance/p3_verify.py`, `Subcommand`, the empty `tools/acceptance/p3_restore_check.py`, `tests/log_hygiene.py`). P2's restore test as it is on `main` (OPS-RT, P2 #130): `tools/acceptance/restore_verify.py`, `scratch_restore.py`, `deploy/server.sh restore-test` |
| **Consumed by** | `deploy/server.sh restore-test` (through `restore_verify.main`, unchanged invocation); the operator after a real restore (`just prod <ip> p3-verify restore-check`); SQ in LIVE-P3 |
| **Branch → base** | `p3/ops-rt3s` from `origin/int/p3` → PR to `int/p3` (D+4) |
| **Normative inputs** | [P3-schema](../contracts/P3-schema.md) §7.1 (`end_delegations`), §8 (post-restore step 2b, the notice payload), §9 (factories); [P3-interfaces](../contracts/P3-interfaces.md) §13 (`delegation_ended` audit details), §14.2 (`restore-check` subcommand: "delegations ended `restored_from_backup`, scheduled autonomous actions cancelled, nothing resumes"; stdlib + psycopg, read-only snapshot, content-free, exit 0/1/2), §15.1; decisions OD-12 and decisions.md §C conflict 20 (ARCHITECTURE §15.6: the restore step ends every active delegation); ARCHITECTURE §15.6; `deploy/runbooks/restore.md` |

## 1. Goal

A restore rewinds the database by up to 24 hours. Since P3, a restored database can hold delegations that were active at backup time, their chat links, delegation-only knowledge and scheduled autonomous replies. F0-S's post-restore step 2b ends every active delegation (`restored_from_backup`), and step 2 already cancels every pending action (OD-12). This unit proves it on real data, without reading any content:

- it adds the checks to the scratch restore test the TL and SQ run before the trial (runbook §A), so a broken step 2b stops that test;
- it registers the same checks as `p3-verify restore-check`, which the operator runs on production after a real restore, before starting the app (runbook §B);
- it documents both in the restore runbook.

The script reads ids, states, reasons, timestamps and counts only, and prints fixed codes, internal ids and counts.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2 (safety), §3, §4 (PR template), §5, §6.2.
3. [P3-schema](../contracts/P3-schema.md) §7.1 and §8 (what step 2b does, in order) and §2.1–§2.2 (`delegations`, `delegation_conversations`).
4. `src/assistant/privacy/post_restore.py` (after F0-S): the one transaction, its `now`, the `restored_from_backup` audit row per active account (`at = now`, `details.backup_taken_at`), the notice payload under `system_state` key `restore_notice_pending`.
5. `tools/acceptance/restore_verify.py` and `tests/unit/test_restore_verify.py`, `tests/integration/test_restore_verify.py` (the P2 gate you extend; how it prints; how its tests connect).
6. `tools/acceptance/scratch_restore.py` and the `restore-test)` block of `deploy/server.sh` (how the gate reaches the scratch database: `python -m tools.acceptance.scratch_restore restore-check` execs `python -m tools.acceptance.restore_verify`; `set -euo pipefail` stops the test on a non-zero exit). You change neither.
7. `tools/acceptance/p3_verify.py` (F0-E): `Subcommand`, `SUBCOMMAND_MODULES`, how it imports the subcommand modules, `main` (one read-only `REPEATABLE READ` snapshot; exit codes 0/1/2; an unexpected exception prints only its class name). `tools/acceptance/p2_verify.py::open_database`.
8. `tests/integration/p3_factories.py` (F0-S) and `tests/integration/p2_factories.py`.
9. `deploy/runbooks/restore.md`, all of it.
10. This spec, to the end.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `tools/acceptance/p3_restore_check.py` | existing (F0-E's empty module) | Keep F0-E's docstring line naming the owner and its import of `Subcommand` exactly as F0-E wrote it; add everything of §6.1 and replace `SUBCOMMANDS = ()` |
| `tools/acceptance/restore_verify.py` | existing | **Named touch point** (§6.2): one import line and the body of `main()`'s `with` block. `verify()`, `_notice()`, `_id()` and every other line unchanged |
| `deploy/runbooks/restore.md` | existing | The P3 section of §6.3: a new subsection at the end of §A and a new step 4b in §B. Nothing else |
| `tests/unit/test_p3_restore_check.py` | new | §7.1 |
| `tests/integration/test_p3_restore_check.py` | new | §7.2 |
| `tests/integration/test_log_hygiene_ops_rt3s.py` | new | §7.3, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |

Nothing else.

## 4. Forbidden

- Everything under `src/` (the checker lives in `tools/`, which `src/` never imports and which imports nothing from `src/`: AGENTS §5 rule 4). In particular `src/assistant/privacy/post_restore.py` and `inbox/backup_check.py` (F0-S, J1): if step 2b is wrong, report it; never fix it here.
- `deploy/server.sh`, `tools/acceptance/scratch_restore.py`, `tools/acceptance/p2_verify.py`, `tools/acceptance/p3_verify.py`, `tools/acceptance/{p3_checks,p3_views,p3_report}.py` (F0-E, SQ).
- `tests/unit/test_restore_verify.py`, `tests/integration/test_restore_verify.py`, `tests/integration/test_post_restore.py`, `tests/unit/test_server_sh.py`, `tests/integration/p3_factories.py`, `tests/integration/p2_factories.py` (read and reuse, never edit).
- Imports in `p3_restore_check.py` other than the standard library, `psycopg` and what F0-E's header already imports. No logger; output is `print` of fixed `key=value` lines only.
- Any statement that writes (`INSERT`, `UPDATE`, `DELETE`, DDL). The connection is read-only anyway; the code must not try.
- Any SQL that names a content column (`delegations.spec`, `memory_items.content`, `actions.text`, `tasks.title`, …; `privacy/content_columns.py`).
- `pyproject.toml`, `uv.lock`, `.env*`, `var/`, every `docs/` file, every other runbook.
- **Every file not in §3.**

## 5. Shared lists you must update

None. The subcommand name `restore-check` is registered by your module's `SUBCOMMANDS` (F0-E's registry collects it; a duplicate name raises). No flag, setting, audit event, notification kind or exact-set test changes.

## 6. Step-by-step

### 6.1 `tools/acceptance/p3_restore_check.py` (exact API)

Below F0-E's header, the module docstring continues (or is replaced by, keeping F0-E's owner line):

```python
"""P3 restore check (OPS-RT3s; P3-schema §8; OD-12; deploy/runbooks/restore.md).

After `post-restore`, no delegation from before the restore may still be
active, none of its chat links or delegation-only knowledge may stay live,
no autonomous reply from before the restore may still be pending, nothing
may act for a delegation the restore ended, and autonomy must stay paused
until the owner resumes it. Read-only and content-free: fixed codes, counts,
internal ids and the restore time. Standard library and psycopg only; never
imported by `src/`.

Two entry points share `evaluate`: `SUBCOMMANDS` (`p3-verify restore-check`,
on production after a real restore, runbook §B) and `verify_p3`, which
`restore_verify.main` runs in the scratch restore test (runbook §A)."""
```

```python
RESTORE_EVENT: Final = "restored_from_backup"
RESTORE_NOTICE_KEY: Final = "restore_notice_pending"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUABLE = "NOT_EVALUABLE"


EXIT_CODES: Final[Mapping[Outcome, int]] = MappingProxyType(
    {Outcome.PASS: 0, Outcome.FAIL: 1, Outcome.NOT_EVALUABLE: 2}
)

Q_RESTORE_AT: Final = "SELECT max(at) FROM app.audit_log WHERE event = 'restored_from_backup'"
Q_ACTIVE_ACCOUNTS: Final = "SELECT count(*) FROM app.accounts WHERE status = 'active'"
Q_ENDED_BY_RESTORE: Final = (
    "SELECT account_id, count(*) FROM app.delegations "
    "WHERE ended_reason = 'restored_from_backup' AND ended_at = %(restore_at)s "
    "GROUP BY account_id ORDER BY account_id"
)
Q_AUTONOMOUS_BY_RESTORE: Final = (
    "SELECT state, count(*) FROM app.actions "
    "WHERE origin = 'delegation_auto' AND state_reason = 'restored_from_backup' "
    "AND state IN ('cancelled', 'delivery_uncertain') GROUP BY state"
)
Q_RESTORE_AUDIT_ROWS: Final = (
    "SELECT count(*) FROM app.audit_log WHERE event = 'delegation_ended' "
    "AND details ->> 'reason' = 'restored_from_backup' AND at = %(restore_at)s"
)
Q_NOTICE: Final = "SELECT value FROM app.system_state WHERE key = 'restore_notice_pending'"

#: (check name, query returning one count of offending rows), in print order.
CHECKS: Final[tuple[tuple[str, str], ...]] = (
    (
        "no_active_delegation_from_before_restore",
        "SELECT count(*) FROM app.delegations "
        "WHERE status = 'active' AND confirmed_at <= %(restore_at)s",
    ),
    (
        "no_live_link_of_an_ended_delegation",
        "SELECT count(*) FROM app.delegation_conversations AS dc "
        "JOIN app.delegations AS d ON d.id = dc.delegation_id AND d.account_id = dc.account_id "
        "WHERE dc.ended_at IS NULL AND d.status <> 'active'",
    ),
    (
        "no_delegation_knowledge_of_an_ended_delegation",
        "SELECT count(*) FROM app.memory_items AS m "
        "JOIN app.delegations AS d ON d.id = m.delegation_id AND d.account_id = m.account_id "
        "WHERE m.scope = 'delegation' AND d.status <> 'active'",
    ),
    (
        "no_pending_autonomous_action_from_before_restore",
        "SELECT count(*) FROM app.actions WHERE origin = 'delegation_auto' "
        "AND state IN ('proposed', 'approved', 'scheduled', 'executing') "
        "AND created_at <= %(restore_at)s",
    ),
    (
        "no_autonomous_action_for_a_restored_delegation",
        "SELECT count(*) FROM app.actions AS a "
        "JOIN app.delegations AS d ON d.id = a.delegation_id AND d.account_id = a.account_id "
        "WHERE a.origin = 'delegation_auto' AND d.ended_reason = 'restored_from_backup' "
        "AND a.created_at > d.ended_at",
    ),
    (
        "autonomy_paused_until_the_owner_resumes",
        "SELECT count(*) FROM app.accounts AS ac WHERE ac.status = 'active' "
        "AND NOT ac.autonomy_paused AND ac.created_at <= %(restore_at)s "
        "AND NOT EXISTS (SELECT 1 FROM app.audit_log AS r WHERE r.account_id = ac.id "
        "AND r.event = 'autonomy_resumed' AND r.actor = 'owner' AND r.at > %(restore_at)s)",
    ),
)
ALL_QUERIES: Final[tuple[str, ...]] = (
    Q_RESTORE_AT,
    Q_ACTIVE_ACCOUNTS,
    Q_ENDED_BY_RESTORE,
    Q_AUTONOMOUS_BY_RESTORE,
    Q_RESTORE_AUDIT_ROWS,
    Q_NOTICE,
    *(query for _, query in CHECKS),
)


def notice_counts(value: object) -> dict[int, int] | None: ...
def evaluate(conn: psycopg.Connection[Any]) -> Outcome: ...  # prints the report
def verify_p3(conn: psycopg.Connection[Any]) -> bool: ...  # evaluate(conn) is Outcome.PASS
def run(conn: Any, args: argparse.Namespace) -> int: ...  # EXIT_CODES[evaluate(conn)]


SUBCOMMANDS = (
    Subcommand(
        name="restore-check",
        help="P3 restore check: delegations ended, nothing resumes (read-only, counts only)",
        add_arguments=_no_arguments,
        run=run,
    ),
)
```

`_no_arguments(parser: argparse.ArgumentParser) -> None` adds nothing and returns `None`. The names above are binding (`restore_verify`, the tests and F0-E's registry use them). Format the SQL strings as you like (ruff format); the SQL text itself must not change.

**Rules (each is tested in §7):**

1. **`notice_counts(value)`** returns `{int(key): count}` when `value` is a `dict` whose every key is a `str` that `isascii()` and `isdecimal()`, has at most 19 characters and is a positive integer, and whose every value has `type(v) is int` (never `bool`) and `v >= 0`; an empty dict gives `{}`. Anything else gives `None`. It never raises and never prints.
2. **`evaluate(conn)`**, in this order, printing one line per item (nothing else is printed, ever):
   1. `restore_at = conn.execute(Q_RESTORE_AT).fetchone()[0]`.
   2. `restore_at is None`:
      - `conn.execute(Q_ACTIVE_ACCOUNTS).fetchone()[0] == 0` ⇒ print `p3_restore_at=none` and `p3_restore_check=PASS reason=no_active_accounts`; return `PASS` (nothing exists that could resume);
      - otherwise print `p3_restore_check=NOT_EVALUABLE reason=no_restore_recorded`; return `NOT_EVALUABLE`.
   3. Print `p3_restore_at=<restore_at.astimezone(UTC).isoformat()>`.
   4. `ended = {account_id: count}` from `Q_ENDED_BY_RESTORE` (`{"restore_at": restore_at}`). For each account in ascending id order print `p3_account=<id> delegations_ended=<n>`; then `p3_delegations_ended=<sum>`.
   5. From `Q_AUTONOMOUS_BY_RESTORE`: print `p3_autonomous_cancelled=<n>` and `p3_autonomous_uncertain=<n>` (0 when absent). These count every restore so far (informational; no pass or fail).
   6. For each `(name, query)` in `CHECKS`: `count = conn.execute(query, {"restore_at": restore_at}).fetchone()[0]`; print `p3_check=<name> result=PASS count=0` when `count == 0`, else `p3_check=<name> result=FAIL count=<count>`.
   7. `one_audit_row_per_restored_delegation`: `count = abs(<Q_RESTORE_AUDIT_ROWS count> - sum(ended.values()))`; printed like rule 6.
   8. `restore_notice_counts`: `row = conn.execute(Q_NOTICE).fetchone()`.
      - `row is None` (the running app already delivered the notice) ⇒ `p3_check=restore_notice_counts result=SKIPPED count=0` (not a failure).
      - `row[0]` not a `dict`, or `notice_counts(row[0].get("delegations_ended")) is None` ⇒ `result=FAIL count=1`.
      - otherwise `count` = the number of account ids in `set(counts) | set(ended)` with `counts.get(id, 0) != ended.get(id, 0)`; printed like rule 6. Never print a notice value.
   9. Print `p3_restore_check=PASS` and return `PASS` when every check of rules 6–8 is `PASS` or `SKIPPED`; else print `p3_restore_check=FAIL` and return `FAIL`.
   - Every count comes back from psycopg as an `int`; convert with `int(...)` before comparing or printing.
3. **`verify_p3(conn)`** returns `evaluate(conn) is Outcome.PASS`; **`run(conn, args)`** returns `EXIT_CODES[evaluate(conn)]`. Neither catches exceptions: F0-E's `main` and `restore_verify.main` print only the class name of an unexpected error.
4. **Why these checks** (write the reason as one short comment above each `CHECKS` entry):
   - `confirmed_at <= restore_at` and `created_at <= restore_at`: a delegation or autonomous reply the owner confirmed **after** the restore is legitimate and is not flagged (§7.2 `test_a_delegation_confirmed_after_the_restore_is_not_flagged`).
   - `created_at > d.ended_at`: "nothing resumes" — nothing may act for a delegation the restore ended (a reconciler that re-deferred `replies.delegated` must have found no active delegation).
   - the `autonomy_resumed` audit row with `actor = 'owner'`: post-restore pauses every active account without an audit row; only `/resume` or a card press (actor `owner`) may lift it.
   - `ended_at = restore_at`: step 2b runs in the post-restore transaction with the same `now` as the `restored_from_backup` audit row (P3-schema §8).

### 6.2 `tools/acceptance/restore_verify.py` (named touch point)

Add, below the existing `from tools.acceptance.p2_verify import REASONS, open_database` line:

```python
from tools.acceptance.p3_restore_check import verify_p3
```

and change only the `with` block inside `main()`'s `try` to:

```python
        with open_database(url) as conn:
            p2_result = verify(conn)
            # P3 (OPS-RT3s): delegations ended, nothing resumes; same snapshot.
            p3_passed = verify_p3(conn)
            return int(p2_result != 0 or not p3_passed)
```

`verify()` is unchanged, so the existing unit tests (`tests/unit/test_restore_verify.py`, which call `verify()` with a fake connection, and the one that makes `verify` raise inside `main()`) keep passing unchanged. The P2 lines are printed first, ending with `restore_verification=PASS|FAIL`, then the P3 lines ending with `p3_restore_check=…`; the exit code is 1 if either fails, so `server.sh restore-test` (under `set -euo pipefail`) stops and never prints "restore verification passed". This is a named touch point of this unit (P3-interfaces §15.1: one import and `main()`'s `with` block in `tools/acceptance/restore_verify.py`); it is the only way to reach the scratch database without editing the frozen `server.sh`.

### 6.3 `deploy/runbooks/restore.md` (the P3 section)

**(a)** At the end of §A (after the paragraph that ends "Offline tests do not complete this live acceptance requirement."), add:

```markdown
### A.1 P3 check: delegations and automatic replies (OPS-RT3s)

The same read-only gate then runs `tools/acceptance/p3_restore_check.py` in the
same snapshot (ARCHITECTURE §15.6 step 2b; OD-12). It prints:

- `p3_restore_at=<UTC time of the post-restore run>`;
- `p3_account=<id> delegations_ended=<n>` for each account whose delegations
  the restore ended, then `p3_delegations_ended=<total>`;
- `p3_autonomous_cancelled=<n>` and `p3_autonomous_uncertain=<n>` (automatic
  replies a restore cancelled or made uncertain; informational);
- one `p3_check=<name> result=PASS|FAIL count=<n>` line per check, `count`
  being the number of offending rows: `no_active_delegation_from_before_restore`,
  `no_live_link_of_an_ended_delegation`,
  `no_delegation_knowledge_of_an_ended_delegation`,
  `no_pending_autonomous_action_from_before_restore`,
  `no_autonomous_action_for_a_restored_delegation`,
  `autonomy_paused_until_the_owner_resumes`,
  `one_audit_row_per_restored_delegation`, and `restore_notice_counts`
  (`SKIPPED` once the running app has delivered the notice);
- `p3_restore_check=PASS`, `FAIL` or `NOT_EVALUABLE`.

Pass also requires `p3_restore_check=PASS`; a `FAIL` or `NOT_EVALUABLE` exits 1
and the restore test stops. Nothing but fixed codes, internal ids, counts and
the restore time is printed.

The test restores the newest dump in `/backups/daily` and does not migrate it.
A dump taken before revision 0015 was applied (before deploy #1) has no P3
tables, so `post-restore` itself stops there (a database error, before this
check runs). That is not a restore fault: take a fresh dump after the P3
deploy (`just prod-backup <ip>`) and run the test again.

For the P3 live run (LIVE-P3, with SQ): before `just prod-backup`, have at
least one active delegation (shadow mode is enough) and, if automatic sending
is on, one scheduled automatic reply. Run this test and record the
`p3_account=<id> delegations_ended=<n>` line (n of at least 1), the eight
`p3_check` lines and `p3_restore_check=PASS` in the milestone report. The
scratch database is dropped afterwards; production keeps its delegation.
```

**(b)** In §B, between step 4 and step 5, add:

```markdown
4b. `just prod <ip> p3-verify restore-check` (read-only; before `app-start`):
   expect `p3_restore_check=PASS`, with the `p3_account` lines matching the
   delegations the notice will list. A `FAIL` means a delegation or automatic
   reply from before the restore is still live: do not start the app; tell
   the TL with the printed lines.
```

### 6.4 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd2'` set, from PowerShell on the host (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR (WORKFLOW §5).

1. **Branch.** `git fetch origin`; `git switch -c p3/ops-rt3s origin/int/p3`. **Check:** `just test tests/integration/test_post_restore.py tests/integration/test_restore_verify.py tests/unit/test_restore_verify.py -q` → all pass (F0-S's step 2b and P2's gate are on your base).
2. **Red tests.** Create the three test files of §7. In `p3_restore_check.py` add the constants and queries of §6.1 and stub `notice_counts`, `evaluate`, `verify_p3`, `run` with `raise NotImplementedError`; set `SUBCOMMANDS` as in §6.1. **Check:** `just test tests/unit/test_p3_restore_check.py tests/integration/test_p3_restore_check.py -q` → the tests fail with `NotImplementedError` (the query-text and registry tests may already pass), none with `ImportError` or `NameError`.
3. **`notice_counts`.** **Check:** `just test tests/unit/test_p3_restore_check.py -q` → passes.
4. **`evaluate`, `verify_p3`, `run`.** **Check:** `just test tests/integration/test_p3_restore_check.py -q -k "not scratch_restore_gate"` → passes.
5. **`restore_verify.py` touch point** (§6.2). **Check:** `just test tests/integration/test_p3_restore_check.py tests/unit/test_restore_verify.py tests/integration/test_restore_verify.py tests/unit/test_scratch_restore.py tests/unit/test_server_sh.py -q` → all pass.
6. **Hygiene test** (§7.3). **Check:** `just test tests/integration/test_log_hygiene_ops_rt3s.py -q` → passes.
7. **Runbook** (§6.3). **Check:** `git diff origin/int/p3 -- deploy/runbooks/restore.md` shows only the new §A.1 subsection and step 4b.
8. **Static checks.** `docker compose -f compose.dev.yml run --rm app uv run ruff check tools/acceptance/p3_restore_check.py tools/acceptance/restore_verify.py tests/unit/test_p3_restore_check.py tests/integration/test_p3_restore_check.py tests/integration/test_log_hygiene_ops_rt3s.py`; the same files with `uv run ruff format --check`; `just test tests/unit/test_layering.py -q` (nothing in `src/` imports `tools/`). **Check:** all clean.
9. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the six files of §3.
10. **Commit and PR** (§12). Commit at step 2 (red tests) and at step 8 (green), each with `git commit -F <message file>`. **Check:** `git log --oneline origin/int/p3..HEAD` lists exactly those two commits (plus any fix commit after review).

## 7. Named tests (red first)

### 7.1 `tests/unit/test_p3_restore_check.py`

- `test_notice_counts_reads_account_ids_and_counts` — `notice_counts({"17": 2, "3": 0}) == {17: 2, 3: 0}`; `notice_counts({}) == {}`.
- `test_notice_counts_refuses_anything_else` — parametrized, each `None`: `None`, `[]`, `"x"`, `{"17": -1}`, `{"17": True}`, `{"17": "2"}`, `{"17": 2.0}`, `{"abc": 1}`, `{"0": 1}`, `{"-1": 1}`, `{"12345678901234567890": 1}`, `{"１７": 1}` (full-width digits).
- `test_the_restore_check_is_registered_once` — `[s.name for s in p3_restore_check.SUBCOMMANDS] == ["restore-check"]`; its `run is p3_restore_check.run`.
- `test_the_exit_codes` — `EXIT_CODES == {Outcome.PASS: 0, Outcome.FAIL: 1, Outcome.NOT_EVALUABLE: 2}`.
- `test_the_queries_name_no_content_column` — for every query in `ALL_QUERIES` and every `(table, column)` in `assistant.privacy.content_columns.CONTENT_COLUMNS`: when `app.<table>` appears in the query (lowercased), the bare word `column` does not.
- `test_the_queries_only_read` — no query in `ALL_QUERIES` contains the words `insert`, `update`, `delete`, `truncate`, `drop`, `alter` or `create` (whole words, case-insensitive), and each starts with `SELECT`.
- `test_p3_verify_lists_restore_check` — `subprocess.run([sys.executable, "tools/acceptance/p3_verify.py", "-h"], cwd=<repo root>, capture_output=True, text=True, check=False)`; `"restore-check"` appears in `stdout + stderr` (proves F0-E's script-mode import finds your module).

### 7.2 `tests/integration/test_p3_restore_check.py`

Fixtures: the `env` fixture of `tests/integration/business_env.py` (clean database per test) and `migrated_url`. `RESTORE_AT = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)`; every test first calls `env.clock.set(RESTORE_AT)`. Helpers in the test file:

- `seed(env) -> Seeded(account, conversation, delegation, action)`: inside one `async with env.factory() as session, session.begin():` — `seed_account`, `seed_conversation`, `seed_delegation(session, account_id=…, conversation_ids=[conversation.id], kind="time_bounded", starts_at=RESTORE_AT - timedelta(hours=3), ends_at=RESTORE_AT + timedelta(days=1), confirmed_at=RESTORE_AT - timedelta(hours=3))`, `seed_autonomous_action(session, account_id=…, conversation_id=…, delegation_id=…, state="scheduled", created_at=RESTORE_AT - timedelta(hours=2))`, `seed_memory_item(session, account_id=…, scope="delegation", kind="shareable", source="owner_stated", delegation_id=…, content="Synthetic note")`.
- `restore(env)`: `await post_restore(env.factory, env.clock, backup_taken_at=RESTORE_AT - timedelta(hours=1))`.
- `check(url) -> int`: `with open_database(url) as conn: return p3_restore_check.run(conn, argparse.Namespace())`, called through `asyncio.to_thread`; read the printed lines with `capsys.readouterr().out.splitlines()`.
- Tampering runs in its own committed transaction (`async with env.factory() as session, session.begin():`).

Tests:
- `test_before_any_restore_the_check_is_not_evaluable` — `seed`, no `restore`: `check == 2`; the output is exactly `["p3_restore_check=NOT_EVALUABLE reason=no_restore_recorded"]`.
- `test_with_no_active_account_and_no_restore_the_check_passes` — empty database: `check == 0`; output exactly `["p3_restore_at=none", "p3_restore_check=PASS reason=no_active_accounts"]`.
- `test_after_post_restore_the_check_passes_and_counts_what_ended` — `seed`, `restore`: `check == 0`; the output is exactly, in order: `p3_restore_at=2026-11-02T12:00:00+00:00`, `p3_account=<account.id> delegations_ended=1`, `p3_delegations_ended=1`, `p3_autonomous_cancelled=1`, `p3_autonomous_uncertain=0`, the six `CHECKS` names each as `p3_check=<name> result=PASS count=0`, `p3_check=one_audit_row_per_restored_delegation result=PASS count=0`, `p3_check=restore_notice_counts result=PASS count=0`, `p3_restore_check=PASS`.
- `test_each_leftover_fails_its_check` — parametrized; after `seed` and `restore`, one tamper, then `check == 1`, the output contains the named line, and the last line is `p3_restore_check=FAIL`:

  | id | Tamper | Expected line |
  |---|---|---|
  | `active` | `UPDATE app.delegations SET status = 'active', ended_at = NULL, ended_reason = NULL WHERE id = :d` | `p3_check=no_active_delegation_from_before_restore result=FAIL count=1` |
  | `link` | `UPDATE app.delegation_conversations SET ended_at = NULL WHERE delegation_id = :d` | `p3_check=no_live_link_of_an_ended_delegation result=FAIL count=1` |
  | `knowledge` | `seed_memory_item(…, scope="delegation", kind="shareable", source="owner_stated", delegation_id=d, content="Synthetic note")` | `p3_check=no_delegation_knowledge_of_an_ended_delegation result=FAIL count=1` |
  | `pending` | `UPDATE app.actions SET state = 'scheduled', state_reason = NULL WHERE id = :x` (the seeded action) | `p3_check=no_pending_autonomous_action_from_before_restore result=FAIL count=1` |
  | `acted` | `seed_autonomous_action(…, delegation_id=d, state="cancelled", created_at=RESTORE_AT + timedelta(minutes=1))` | `p3_check=no_autonomous_action_for_a_restored_delegation result=FAIL count=1` |
  | `unpaused` | `UPDATE app.accounts SET autonomy_paused = false WHERE id = :a` | `p3_check=autonomy_paused_until_the_owner_resumes result=FAIL count=1` |
  | `audit` | `DELETE FROM app.audit_log WHERE event = 'delegation_ended'` | `p3_check=one_audit_row_per_restored_delegation result=FAIL count=1` |
  | `notice` | `UPDATE app.system_state SET value = jsonb_set(value, '{delegations_ended}', CAST(:v AS jsonb)) WHERE key = 'restore_notice_pending'` with `v = json.dumps({str(a): 2})` | `p3_check=restore_notice_counts result=FAIL count=1` |
- `test_an_owner_resume_after_the_restore_passes` — after `seed`, `restore`: `UPDATE app.accounts SET autonomy_paused = false WHERE id = :a` and `INSERT INTO app.audit_log (account_id, at, actor, event, subject_type, subject_id, details) VALUES (:a, :at, 'owner', 'autonomy_resumed', 'account', :a, '{"source": "command"}')` with `at = RESTORE_AT + timedelta(minutes=5)`: `check == 0`.
- `test_a_malformed_restore_notice_fails_without_printing_it` — set `delegations_ended` to `{"CANARY-SYNTH-P3": "x"}` (the `notice` tamper with that value): `check == 1`; `p3_check=restore_notice_counts result=FAIL count=1`; `CANARY` does not appear in the output.
- `test_a_delivered_notice_is_skipped` — `DELETE FROM app.system_state WHERE key = 'restore_notice_pending'`: `check == 0`; the output contains `p3_check=restore_notice_counts result=SKIPPED count=0`.
- `test_a_delegation_confirmed_after_the_restore_is_not_flagged` — after `seed`, `restore`: a second conversation with a new active delegation (`starts_at = confirmed_at = RESTORE_AT + timedelta(minutes=10)`, `ends_at = RESTORE_AT + timedelta(days=1)`) and a `scheduled` autonomous action for it created at `RESTORE_AT + timedelta(minutes=11)`: `check == 0`.
- `test_rg21_counts_are_per_account` — two accounts, each with one chat and one active delegation, `restore`: `check == 0`; the output holds `p3_account=<low id> delegations_ended=1` before `p3_account=<high id> delegations_ended=1`, and `p3_delegations_ended=2`.
- `test_the_scratch_restore_gate_includes_the_p3_check` — `monkeypatch.setenv("DATABASE_URL", migrated_url)`; after `seed` and `restore`: `await asyncio.to_thread(restore_verify.main) == 0` and the output contains `restore_verification=PASS` and `p3_restore_check=PASS`; after the `active` tamper: `restore_verify.main() == 1` and the output contains `restore_verification=PASS` and `p3_restore_check=FAIL`.

### 7.3 `tests/integration/test_log_hygiene_ops_rt3s.py`

- `test_restore_check_output_is_content_free` — `seed` with `content=CANARY` on the knowledge item, then `UPDATE app.delegations SET spec = jsonb_set(spec, '{topic}', to_jsonb(CAST(:c AS text)))` with `c = CANARY`, a task `seed_task(session, account_id=…, conversation_id=…, title=CANARY)` and the contact's `display_name = CANARY`; `restore`; inside `capture_logs()` (`tests/log_hygiene.py`) run `check` and `restore_verify.main()` (with `DATABASE_URL` set as above). `assert_content_free(captured_stdout + log_output, CANARY)`.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **A missing restore is not a pass.** No `restored_from_backup` audit row while active accounts exist ⇒ `NOT_EVALUABLE` (exit 2; the scratch gate fails), never `PASS`.
- **A delivered notice is skipped, not failed and not faked.** The line says `SKIPPED`.
- **What the owner did after the restore is not a fault.** A delegation confirmed later, its replies, and an owner `/resume` are not flagged; an un-paused account without an owner resume is.
- **Counts are of offending rows.** `count=0` means none was found in that snapshot; the check never says "fixed" or "cleaned".
- **Uncertain is not cancelled.** `p3_autonomous_cancelled` and `p3_autonomous_uncertain` are separate.
- **Nothing is printed from a payload** unless it was validated as an integer id or count first.
- **An old dump is not a pass and not a P3 fault.** A dump from before revision 0015 stops `post-restore` (no `delegations` table); the runbook says to take a fresh dump after deploy #1. The check never runs against a database without the P3 tables, and a database error never prints a row or the URL (`restore_verify.main` prints only the error class).
- **A restore that ended no delegation prints no `p3_account` line and `p3_delegations_ended=0`,** not an invented count; with every other check passing that is a valid result when no delegation was active in the dump. (The LIVE-P3 run is the one that needs n of at least 1.)
- **The check proves the state it reads, not the app's behaviour.** It does not start the app, resume anything or send anything; `count=0` means "none found in this snapshot".

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing for the right reason, and passes.
- [ ] `git diff --stat origin/int/p3...HEAD` lists only the six files of §3; the `restore_verify.py` hunk is the import line and `main()`'s `with` block only; the runbook hunk is §A.1 and step 4b only.
- [ ] `ruff check` and `ruff format --check` are clean on the changed files; `test_layering.py` passes.
- [ ] `p3_restore_check.py` imports only the standard library, `psycopg` and F0-E's `Subcommand`; no logger; no `assert`; no `except`.
- [ ] Every query is a `SELECT` and names no content column.
- [ ] The existing P2 restore tests (`tests/unit/test_restore_verify.py`, `tests/integration/test_restore_verify.py`, `tests/unit/test_scratch_restore.py`, `tests/unit/test_server_sh.py`) pass unchanged.
- [ ] The PR description pastes the output of `test_after_post_restore_the_check_passes_and_counts_what_ended` (synthetic ids) for SQ's LIVE-P3 checklist, and names the `restore_verify.py` touch point for the TL.

## 10. When to stop and ask

Ask on the OPS-RT3s issue, starting the comment with "Question for SQ:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- F0-S's post-restore does not end delegations, does not write `delegations_ended` into the notice payload, or writes `ended_at` different from the `restored_from_backup` audit row's `at` (`test_after_post_restore_…` shows `delegations_ended` missing or `one_audit_row_…` failing on clean data). Do **not** change the rule; ask (it is an F0-S fix for J1).
- `tools/acceptance/p3_restore_check.py`, `Subcommand` or `SUBCOMMAND_MODULES` is missing or shaped differently from P3-interfaces §14.2, or `test_p3_verify_lists_restore_check` fails because of how `p3_verify.py` imports modules.
- Importing `p3_restore_check` from `restore_verify` fails under `python -m tools.acceptance.restore_verify` (for example an import cycle with F0-E's `p3_verify.py`), or `p3_verify.py` imports its subcommand modules at import time instead of in `parser()`.
- F0-S's post-restore writes the `restored_from_backup` audit row and `end_delegations` with different `now` values (check `one_audit_row_per_restored_delegation` or `restore_notice_counts` fails on clean data): P3-schema §8 requires one transaction; do not loosen the equality, ask.
- An existing P2 restore test fails after your change.
- `p3_factories` has no `seed_delegation`, `seed_autonomous_action` or `seed_memory_item`, or they refuse the fields §7.2 passes.
- You think a check needs a file outside §3 (for example `server.sh` or `scratch_restore.py`).
- You have spent more than half a day and are not close.

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`): `p3_restore_check.py` has no `except` at all. The existing operational catch in `restore_verify.main()` stays as it is.
- No `assert` in production code.
- Full type hints (`ruff` and the reviewers check them; `tools/` is outside `mypy src evals`, so keep the hints exact anyway).
- Read-only: `SELECT` only; one snapshot (the caller's connection); never `commit`.
- Content-free output: the fixed `key=value` lines of §6.1, internal ids, counts and the restore time only.
- `tools/` never imports `src/`; `src/` never imports `tools/`.
- GitHub: every PR body and comment through `--body-file <file>` (never an inline body in PowerShell); no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`; never commit `.env`, `var/`, dumps or real data.

## 12. Pull request

- **Title:** `OPS-RT3s: P3 restore check and restore runbook section`.
- **Size:** about 400 changed lines or fewer (expected ≈ 170 tool code + 6 in `restore_verify.py` + ≈ 40 runbook + ≈ 260 tests).
- **Labels:** `team:david`, `eng:d2` on the draft; when CI is green, mark it ready and add `needs-review:sq` and `needs-qa`; comment on the issue that it is ready.
- **Template** (WORKFLOW §4.3), filled honestly:
  - *Unit:* `WU: OPS-RT3s · Spec: docs/engineering/p3/specs/OPS-RT3s.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: after merge.
  - *Safety invariants touched:* 6 at-most-once and 10 kill switch, checked not changed (`test_each_leftover_fails_its_check[pending]`, `[acted]`, `[unpaused]`); 7 fail closed (`test_before_any_restore_the_check_is_not_evaluable`); 11 honest status (`test_a_delivered_notice_is_skipped`); 12 tenant scoping (`test_rg21_counts_are_per_account`).
  - *Tests:* the §7 names; the commands you ran and their result, or "not run locally: <reason>; CI is the gate". RG tests: RG-21 extended (`test_rg21_counts_are_per_account`); RG-10 by `test_log_hygiene_ops_rt3s.py`; RG-19 evidence for "restore never sends" (`test_each_leftover_fails_its_check[pending]`, `[acted]`, `[unpaused]`; the checker proves it on a restored database, F0-S's `test_post_restore.py` proves the mechanism).
  - *Shared lists updated:* none.
  - *Data:* no migration, no content column; read-only tool; logs: `test_log_hygiene_ops_rt3s.py`.
  - *Prompts, flags and settings:* none.
  - *Docs:* `deploy/runbooks/restore.md` §A.1 and §B step 4b.
  - *Interface changes:* none; out-of-list edits: none (the `restore_verify.py` touch point is named in this spec; say so).
  - *Open items for M07.md:* anything non-blocking the review raised.
- After merge: QD1 checks it on `int/p3`; SQ uses it in the LIVE-P3 OPS-RT3 run; a finding becomes one fix PR from you.
