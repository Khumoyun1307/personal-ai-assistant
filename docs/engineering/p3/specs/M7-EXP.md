# M7-EXP — `/export`: confirm card, `privacy.export` job, per-class JSON parts

| | |
|---|---|
| **Unit** | M7-EXP (S07.4 export; FR-PRV-05, FR-CTL-05) |
| **Team / slot** | Josh / J3 |
| **Size** | M (≈ 1 eng-day) |
| **Depends on** | GW-DOC (`ControlChannel.send_document`, fakes), F0-S (`EXPORT_TASK`, `export_queueing_lock`, `EXPORT_ENABLED`, `EXPORT_MAX_PART_MB`, the P3 tables), F0-R (`jobs/tasks_export.py` registration with `RequeueOnShutdown`, `ExportJobHandlers`, `ControlRoutes.help_lines`, `_wire_autonomy`). Soft dependency on M6-PAUSE (`pause_after_control_forbidden`, R46): wired as soon as it is on the base (see §3.4) |
| **Branch → base** | `p3/m7-exp` → `int/p3`; if not merged by R1, a flagged PR to `main` for deploy #2 |
| **Peer reviewer** | E2 (Codex), dispatched by the TL; plus a **QE2 privacy read** |
| **QA slot and lane** | QJ1 or QJ2, before merge |
| **Tier** | 1, with a TL deep review (privacy) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §4.6, §7.1, §7.2, §10, §12.1, §12.3, §12.5, §13, §15; [P3-schema](../contracts/P3-schema.md) §5; ARCHITECTURE §6.7, §10.1, §10.4, §12.2, §14.5, ADR-14; AGENTS §6.6, §6.11, §6.12, §9; PRD FR-PRV-05, FR-CTL-05; IMPLEMENTATION_PLAN §9 S07.4; decisions OD-20, OD-24 |

## 1. Goal

`/export` sends the owner a readable copy of everything Alviro stores for their account, as JSON files in the control chat and only there, after a confirm card that warns the files stay in Telegram's history. Each data class is one or more standalone parts of at most `EXPORT_MAX_PART_MB`, built in memory from one consistent snapshot. Raw updates and security tokens are left out, with a note. Every part's outcome is reported truthfully, an uncertain part is never resent, and at most one complete export per day goes out.

## 2. Scope

**In:**
1. `privacy/export.py` (new): the class registry, the snapshot reader, the part builder, `DataExporter.run` (the job body) with its at-most-once record and the after-commit `pause_on_forbidden` call (R46).
2. `telegram/control/export.py` (new): the `/export` command, the `exp` confirm card and its press handler.
3. `jobs/tasks_export.py`: the job body call (F0-R registered `privacy.export`; M7-EXP owns the module after CP1).
4. `_wire_autonomy` lines: command, help line, callback kind, job handler group.
5. Tests: the four IMPLEMENTATION_PLAN S07.4 export tests, no file on disk, RG-10/RG-20/RG-21 extensions; ARCHITECTURE §6.7 and §10.4.

**Out:**
- An export tool for the model (OD-20: command only), an HTTP or downloadable archive (ADR-14), a delete button (OD-20; `deleteMessage` is not on the control allow-list), a ZIP.
- The gateway method (GW-DOC). Opening or inspecting a real export: **only the owner** (AGENTS §9; LIVE-P3 records structure only: part count, sizes, delivery).
- The privacy-policy text that mentions export (PV6, OD-24).

## 3. Design

### 3.1 What is exported (`privacy/export.py`)

Every table of the `app` schema is either exported or listed as not exported, and a test compares both sets with the live schema (§5), so a new table cannot be silently missed.

| Class (file stem) | Tables | Rows selected | Columns left out (with the reason in the manifest) |
|---|---|---|---|
| `account` | `accounts` | `id = :account_id` | — |
| `consents` | `consents` | `account_id = :account_id` | — |
| `connections` | `business_connections`, `business_connection_events` | same | `connection_id` in both (a live credential for acting in the owner's chats) |
| `conversations` | `conversations`, `contacts`, `coverage_gaps` | same | — |
| `messages` | `messages` | same | `search_key`, `search_tsv` (search copies derived from the text, which is exported) |
| `control_chat` | `control_turns`, `pending_inputs` | same | `pending_inputs.token` (a button secret) |
| `summaries` | `conversation_summaries` | same | — |
| `memory` | `memory_items` (every scope, kind, source and status, incl. delegation knowledge, shareable and inferred items) | same | — |
| `actions` | `actions` | same | `approval_token` (a button secret) |
| `tasks` | `tasks`, `conversation_triage` | same | — |
| `automations` | `automations`, `automation_occurrences`, `digests` | same | — |
| `notifications` | `notifications` | same | — |
| `delegations` | `delegations`, `delegation_conversations` | same | — |
| `ai_runs` | `agent_runs`, `agent_run_steps`, `tool_confirmations`, `llm_calls` | same; `agent_run_steps`: `run_id IN (SELECT id FROM app.agent_runs WHERE account_id = :account_id)` | — |
| `audit_log` | `audit_log` | same | — |
| `inbound_updates` | `inbound_updates` | same | `payload` (raw Telegram updates; recovery copies kept at most 7 days, ARCHITECTURE §6.1) |

**Not exported** (`NOT_EXPORTED`, each with its reason in the manifest): `system_state`, `availability_minutes` (deployment records, no tenant), `foreign_connections` (other people, never an account's), `alembic_version`. Tables outside the `app` schema (procrastinate's) are never read.

```python
@dataclass(frozen=True, slots=True)
class ExportTable:
    name: str
    scope: Literal["account_id", "id", "via_run"]


@dataclass(frozen=True, slots=True)
class ExportClass:
    name: str
    tables: tuple[ExportTable, ...]


EXPORT_CLASSES: tuple[ExportClass, ...]  # the table above, in that order
EXCLUDED_COLUMNS: Mapping[str, frozenset[str]]  # table -> columns left out
EXCLUSION_REASONS: Mapping[tuple[str, str], str]  # fixed manifest sentences
NOT_EXPORTED: Mapping[str, str]  # table -> fixed reason
```

Columns are read from `information_schema.columns` (ordered by `ordinal_position`) in the same snapshot, minus `EXCLUDED_COLUMNS`, so a column added later is exported without a code change. Each table and column name is checked against `^[a-z_][a-z0-9_]*$` and double-quoted before it goes into the statement (identifiers come from the schema, never from input). Rows are ordered by `id` (every exported table has one; a test checks it). Every statement carries the account predicate in the table above (AGENTS §6.12).

**Values to JSON:** `datetime` → ISO 8601 with offset (UTC as stored); `date` → ISO date; `Decimal` → string; `UUID` → string; `dict`/`list`/`str`/`int`/`bool`/`None` as they are. Any other type raises `TypeError`: the export stops and reports it (§3.5), never a guessed value.

### 3.2 Part format and splitting

Each part is one standalone UTF-8 JSON document:

```json
{"format": "alviro-export", "version": 1, "class": "messages", "part": 2,
 "account_id": 7, "exported_at": "2026-11-10T09:00:00+00:00",
 "rows": [{"table": "messages", "row": {"id": 1, "...": "..."}}, ...]}
```

Rows are serialised one at a time (`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`) and appended while the part's exact byte size, including the envelope and the closing brackets, stays within `max_part_bytes` (= `EXPORT_MAX_PART_MB × 1024 × 1024`, at most 45 MiB = 47,185,920 bytes, under GW-DOC's 50,000,000-byte refusal). The next row starts the next part of the same class. A class with no rows still gets one part with `"rows": []`. A single row larger than a part is left out and counted (`rows_too_large` in the manifest and the summary; truthful, invariant 11).

File names are generated and ASCII only (GW-DOC's filename rule, ≤ 64 characters): `alviro-export-<YYYYMMDD-HHMM>-<class>-<NN>.json` (UTC stamp of the export, part number zero-padded to two digits, more digits when needed; the longest name is 52 characters), and `alviro-export-<stamp>-manifest.json`. Captions are fixed plain text: "Export <stamp>: <class>, part <n>". Neither ever holds content.

The **manifest** is the last part: the format version, `exported_at`, the account id, per class its tables with row counts and its part file names, the left-out columns with their reasons, the not-exported tables with their reasons, `rows_too_large` if any, and this note: "These files stay in this chat's history in Telegram, where Alviro's deletion and retention cannot reach them. Delete them from the chat when you no longer need them."

### 3.3 The command and the confirm card (`telegram/control/export.py`)

`handle_export_command(session, gateway, clock, scheduler, account, message, argument) -> HandlerResult` (a `routes.CommandHandler`), built with the flag and the part size bound in `_wire_autonomy`:
1. `export.enabled` off → send "Export is not available right now." (truthful; P3-interfaces §12.1).
2. A complete export in the last 24 hours (§3.6) → "You already exported your data in the last 24 hours. You can ask again after {owner-local time}."
3. Otherwise create a `pending_inputs` card (`service.create_card(kind="exp", ttl=EXPORT_CARD_TTL)`, 1 hour) and, after commit, send it with buttons **Export** (`exp:<token>:ok`) and **Cancel** (`exp:<token>:cx`); a send that fails or is uncertain deletes the card, so no tap can reach it (the `monitors.queue_notice` pattern).

Card text (fixed; `{mb}` from the setting):

```
📦 <b>Export your data?</b>

I will send everything I store for you as JSON files in this chat: one or more files per kind of data (messages, notes, tasks, delegations, activity and more), each at most {mb} MB. Raw Telegram updates and security tokens are left out.

⚠️ The files stay in this chat's history in Telegram, where my deletion and retention cannot reach them. Delete them yourself when you no longer need them.

You can export once a day.
```

`handle_export_press(session, gateway, clock, scheduler, account, press) -> HandlerResult` (kind `exp`):
1. `service.claim_card(...)` (single use, account-scoped, under the card's row lock). `None` → answer "This button is no longer active."
2. `cx` → after commit: answer the press and edit the card to "Cancelled. Nothing was exported." (keyboard removed).
3. `ok` → re-check the flag and the 24-hour rule (as the command does; a refusal edits the card with that text); then defer `privacy.export` with `args={"account_id": account.id, "card_id": claimed.id}`, `lock=owner_control_lock(account.id)`, `queueing_lock=export_queueing_lock(account.id)`, on the press's connection **inside a SAVEPOINT** (`session.begin_nested()`), because a queueing-lock collision raises inside the transaction (P2 queueing-lock rule, P3-interfaces §7.1). `None` (an export is already queued) → edit: "An export is already being prepared." Otherwise edit, after commit: "Preparing your export. The files will arrive here one by one, then a summary." (keyboard removed).
4. Any other op → answer "This button is no longer active."

Every answer and edit goes through the gateway after commit (AGENTS §5, §7); nothing is sent inside the transaction.

### 3.4 The job (`DataExporter.run`, `privacy.export`)

```python
class DataExporter:
    def __init__(
        self,
        *,
        session_factory,
        clock: Clock,
        channel: ControlChannel,
        enabled: bool,
        max_part_bytes: int,
    ) -> None: ...
    async def run(
        self, account_id: int, card_id: int
    ) -> str: ...  # returns a note (enum) for the job log
```

`privacy` is a domain package: it uses only the `ControlChannel` port (core), `db.uow.transaction`, SQLAlchemy and `accounts` models; it never imports `telegram`, `jobs` or `agent` (import-linter contract 1). It runs under the owner's control lock (`acct:{id}:control`), so it is serialised with deletions, owner runs and applied confirmations (M03 D83), and with `RequeueOnShutdown` (a run cut off by a worker shutdown goes back to the queue; a failure is never retried).

**Tx 1 — decide once** (account row `FOR NO KEY UPDATE` with the bounded `SET LOCAL lock_timeout = '15s'`, as `privacy/requests.py` does; held to commit). A lock timeout rolls Tx 1 back; then, in a fresh short transaction, write `data_export_failed {card_id, cause: "busy"}` and, after commit, tell "I could not start the export because your data is busy. Send /export again in a minute."; return `busy`. Nothing was sent, so this is safe and the owner is never left without an answer.
1. No account → return `no_account`.
2. A `data_exported` or `data_export_failed` row with this `card_id` exists → return `already_done` (silent: at most one announcement).
3. A `data_export_started` row with this `card_id` exists and no final row → a previous run was cut off after it began sending. Write `data_export_failed {card_id, cause: "interrupted"}`, commit, then tell the owner the interrupted text (§3.5); return `interrupted`. Nothing is resent (invariant 6).
4. `enabled` false → `data_export_failed {card_id, cause: "disabled"}` + the "not available" text; return.
5. A complete export in the last 24 hours → `data_export_failed {card_id, cause: "daily_limit"}` + the 24-hour text; return. (`rate_limited` is reserved for Telegram's 429.)
6. `account.control_chat_id` is NULL, or differs from `account.telegram_user_id` → `data_export_failed {card_id, cause: "no_control_chat"}`; return without sending anything. The control chat is recorded only from the owner's own private chat (`/start`, routed `CONTROL`), whose id is the owner's user id (the fast lane relies on the same `from.id == chat.id`), so a mismatch means a corrupted row: refusing it keeps a whole data export from ever reaching another chat (wrong recipient is the worst failure).
7. Write `data_export_started {card_id}` and commit **before** the first part is sent: the at-most-once key across a requeued run (contract gap G12). Keep `control_chat_id` from this transaction; the snapshot never re-reads where to send.

**Snapshot and sending:** one `REPEATABLE READ`, `READ ONLY` transaction (SQLAlchemy connection execution options `isolation_level="REPEATABLE READ"` and `postgresql_readonly=True`; the engineer checks the exact option names for the installed driver). It takes no row lock, so holding it while parts upload never blocks a writer (AGENTS §13); it keeps every class consistent with every other. For each class in order: stream its tables' rows (`yield_per`, 500), build parts (§3.2), and send each part as soon as it is complete with `channel.send_document(account_id=…, chat_id=control_chat_id, filename=…, data=…, caption=…)`. At most one part is in memory at a time; parts are never written to disk.

Each part's outcome:

| `Delivery` | Outcome | Then |
|---|---|---|
| `ok` | `sent` | next part |
| `uncertain` | `uncertain` (it may have arrived) | next part; never resent |
| failed, `error` = `forbidden` or `chat_unreachable` | `failed` | stop: every later part is `not_attempted` (the owner cannot receive files) |
| failed, `error` = `rate_limited` | `failed` | stop: every later part is `not_attempted` (honour Telegram's limit instead of sending more, AGENTS §7; `Delivery` carries no `retry_after`, so the export does not wait and retry) |
| any other failure | `failed` | next part |

A `TypeError` or a database error while building stops the export: the parts already sent keep their outcomes; the rest are `not_attempted`; the cause is `build_failed`.

The manifest is sent last (also when some parts failed; not after a stop for `forbidden`, `chat_unreachable`, `rate_limited` or a build failure).

**Tx 3 — record, then tell:** after a stop, write `data_export_failed {card_id, cause, sent_parts, uncertain_parts}` with `cause` = `forbidden`, `chat_unreachable`, `rate_limited` or `build_failed`; otherwise write `data_exported {card_id, parts, bytes, classes, failed_parts, uncertain_parts}` (`failed_parts` counts `failed` and `not_attempted`); commit; then send the summary (§3.5) with `channel.send` (an uncertain summary is not resent). If Tx 3 fails, a rerun sees `data_export_started` without a final row and reports "interrupted": never a second set of files.

**Forbidden means blocked (R46, ARCHITECTURE §6.8).** After Tx 3 has committed, when a part or the summary came back `forbidden`, the job calls `pause_on_forbidden(account_id, now)`, M6-PAUSE's `actions/autonomy.py::pause_after_control_forbidden` bound to the session factory in `_wire_autonomy` (`DataExporter` takes it as an optional callable, so `privacy/export.py` imports nothing from `actions`; `None` = not wired, nothing happens). It runs in its own `try/except Exception` that logs `pause_after_forbidden_failed` with the exception class name only; it never changes the export's recorded outcome, sends nothing and never resumes. Only `forbidden` pauses (not `chat_unreachable`, `rate_limited` or an uncertain result). If M6-PAUSE has not merged when this unit does, leave the wiring line out and mark the test below `xfail(strict=True, reason="awaiting M6-PAUSE")`; whichever PR merges second wires it and removes the marker.

Logs: `data_export_finished` / `data_export_stopped` with `account_id`, counts, class names (fixed codes) and the exception class only; never a file name, caption or value (AGENTS §9).

### 3.5 What the owner is told (fixed texts, counts and class names only)

| Case | Text |
|---|---|
| All parts sent | "📦 Export finished: {n} files sent ({mb} MB). They stay in this chat's history in Telegram until you delete them." |
| Some parts not sent or not confirmed | "📦 Export finished with problems: {sent} of {n} files sent. Not sent: {class part list}. Not confirmed, may have arrived, not sent again: {list}. You can run /export again." |
| Stopped (`forbidden`, `chat_unreachable`, `rate_limited`, `build_failed`) | "⚠️ The export stopped after {sent} file(s) were sent. Nothing was sent twice. You can run /export again." (After `forbidden` or `chat_unreachable` this summary will normally not arrive either; the audit row still records the truth.) |
| Busy at job start | "I could not start the export because your data is busy. Send /export again in a minute." |
| Interrupted (rerun after a cut-off) | "⚠️ The export was interrupted (the service restarted). Some files may have arrived; I did not send them again. Send /export to start a new one." |
| Disabled / daily limit at job start | the command's texts (§3.3) |

"Sent" is said only for `ok` parts; "not confirmed" is never folded into "sent" (invariant 11, RG-20).

### 3.6 The once-a-day rule

A **complete** export is a `data_exported` row with `failed_parts = 0` and `uncertain_parts = 0`. Another export is refused while one completed less than 24 hours ago (rolling, server time; the message shows the owner-local time it becomes possible). A partial, failed or interrupted export does not count, so the owner can retry it at once. The rule is checked at the command, at the press and, authoritatively, in Tx 1 under the account lock.

### 3.7 Wiring (`bootstrap.py`, inside `_wire_autonomy` only)

- `ExportJobHandlers(run=DataExporter(session_factory=…, clock=…, channel=GatewayControlChannel(ctx.gateway), enabled=EXPORT_ENABLED in flags, max_part_bytes=settings.export_max_part_mb * 1024 * 1024, pause_on_forbidden=<partial(pause_after_control_forbidden, ctx.session_factory) once M6-PAUSE is on the base, else None>).run)` (use F0-R's field name for the handler; contract gap G12).
- `ControlRoutes`: `commands["export"]` (always registered, so the command answers truthfully when off), `callbacks["exp"]`, `help_lines["export"] = "/export — a copy of your data as files in this chat"` only when `export.enabled` is on.

### 3.8 Edge cases (each has a test in §5)

A second press on the same card; Export pressed after the card expired; two cards from two `/export` commands both pressed (the second gets "already being prepared" or, after completion, the 24-hour text); the flag turned off between the press and the job; a worker shutdown during the third part (rerun reports "interrupted", sends nothing); a part that is uncertain; a part refused with `forbidden`; a row inserted into `messages` by ingestion while parts upload (absent from the export); another account with canaries in every class; an account with no delegations or tasks (empty parts); a value of an unexpected type; the control chat unknown, or not equal to the owner's user id; a part answered `rate_limited`; the account lock busy at job start; a `/privacy` deletion requested while an export runs (it waits for the control lock, so no deleted data is exported after its receipt).

## 4. Files

**Owned (P3-interfaces §15):**
- `privacy/export.py` (new; the only P3 edit to `privacy/*` outside J1's files).
- `telegram/control/export.py` (new).
- `jobs/tasks_export.py` (F0-R pre-created; M7-EXP owns it after CP1).
- Test files: `tests/integration/test_export.py`, `tests/integration/test_export_cards.py`, `tests/unit/test_export_format.py`, `tests/integration/test_log_hygiene_m7_exp.py`.

**Named touch points:** `bootstrap.py` (`_wire_autonomy` lines only); `docs/ARCHITECTURE.md` §6.7 and §10.4 (§7).

**Consumed, never edited:** `core/channel.py` (GW-DOC), `telegram/control/agent_channel.py`, `telegram/gateway.py`, `accounts/service.py` (`create_card`, `claim_card`), `accounts/callbacks.py`, `core/jobs.py`, `core/flags.py`, `jobs/tasks.py`, `privacy/content_columns.py`.

**Never edit without an ICR** (§15.2): `privacy/{content_columns,retention,deletion,p3_deletion,post_restore}.py` (J1), migrations, `telegram/gateway.py`, `telegram/control/{dispatcher,routes,handlers,panels}.py`, `config.py`, `core/*`.

## 5. Tests (red first)

Real PostgreSQL; synthetic data with canary strings; `FakeChannel` (GW-DOC: `documents`, `document_bytes`, `document_script`, `on_document`); `FakeClock`; `p2_factories` and `p3_factories` for every class; a second account with its own canaries. Tests parse the parts' JSON; **no test or agent ever runs an export against real data** (AGENTS §9).

**IMPLEMENTATION_PLAN §9 S07.4:**
- `test_export_covers_every_data_class`: (a) the live `app` tables equal `EXPORT_CLASSES` tables ∪ `NOT_EXPORTED`, and every exported table has an `id` column; (b) with a canary seeded in every column of `privacy/content_columns.py::CONTENT_COLUMNS` except the excluded ones (incl. `delegations.spec`, a delegation-scope note, owner and contact shareable items, an inferred `pending_review` item, an owner reminder task, a managed automation), each canary appears in exactly one class's parts.
- `test_export_excludes_other_accounts_and_secrets`: no canary, id or row of the other account in any part; no `pending_inputs.token`, `actions.approval_token`, connection id or `inbound_updates.payload` value in any part; the manifest lists each exclusion with its reason.
- `test_export_delivered_only_to_the_control_chat`: every document and the summary go to `account.control_chat_id` through `ControlChannel`; `privacy/export.py` imports no business sender (AST check) and no business call is recorded.
- `test_export_split_under_50_mb`: with `max_part_bytes = 20_000` and a `messages` class of about 100 KB, every part is ≤ 20,000 bytes, there are ≥ 5 parts, they parse alone, and their rows together equal the table's rows in order; with the default setting, `max_part_bytes = 47,185,920 < 50,000,000`.
- `test_export_leaves_no_file_on_disk` (IMPLEMENTATION_PLAN §9 data requirement "temporary export file deleted after delivery"): `privacy/export.py` and `telegram/control/export.py` contain no `open(`, `tempfile`, `Path.write_*` or `os.write` (AST); an export run in a temporary working directory leaves it unchanged.

**RG extensions and at-most-once:**
- `test_rg20_an_uncertain_part_is_never_resent_and_is_reported_not_confirmed` (RG-20): a scripted uncertain third part → no second attempt; the summary lists it as "not confirmed"; `uncertain_parts = 1`.
- `test_rg21_export_reads_only_the_owners_rows` (RG-21): every statement is account-scoped (two accounts, row counts per class match the owner's).
- `tests/integration/test_log_hygiene_m7_exp.py::test_export_logs_counts_and_codes_only` (the RG-10 extension): canaries in every class; logs and audit rows hold none, nor any file name or caption.
- `test_a_failed_part_is_reported_and_the_rest_continue`.
- `test_a_forbidden_part_stops_the_export_and_marks_the_rest_not_attempted`.
- `test_a_forbidden_part_pauses_autonomy_after_the_export_row_commits` (R46): a scripted `forbidden` part → `pause_on_forbidden` called once, after the `data_export_failed` row committed, with this account's id; a recording stand-in for the callable; a callable that raises never changes the export outcome; `rate_limited`, `chat_unreachable` and an uncertain part never call it; with the callable `None` nothing happens.
- `test_a_rate_limited_part_stops_the_export`: a scripted `rate_limited` second part → no later part is attempted; no manifest; the summary reports the stop.
- `test_export_refuses_a_control_chat_that_is_not_the_owners_private_chat`: `control_chat_id` set to another id → `data_export_failed {cause: "no_control_chat"}`, zero documents, zero messages.
- `test_a_busy_account_lock_is_reported_and_nothing_is_sent`: a barrier-held account lock past the bounded wait → `data_export_failed {cause: "busy"}`, the busy text once, zero documents; a rerun of the same job is silent (`already_done`).
- `test_an_interrupted_export_is_never_resent_on_rerun`: cancel the job after two parts (simulating a shutdown); the rerun sends no document and tells "interrupted" once; a third run is silent.
- `test_a_finished_export_job_rerun_is_silent` (`already_done`).
- `test_export_reads_one_consistent_snapshot`: `on_document` inserts a `messages` row for the account during the first part's send; the row is in no part.
- `test_a_build_error_stops_with_the_parts_sent_so_far` (a value of an unknown type injected through a test-only table entry or monkeypatched converter).

**Card and command** (`tests/integration/test_export_cards.py`):
- `test_export_asks_first_and_warns_the_files_stay_in_telegram`: `/export` → a card with Export and Cancel and the warning text; no job yet.
- `test_cancel_exports_nothing`.
- `test_export_press_defers_one_job_under_the_control_lock_and_its_queueing_lock`: job args are ids only; `lock = acct:{id}:control`, `queueing_lock = export:{id}`; a second card's Export → "already being prepared", the transaction still commits (SAVEPOINT).
- `test_a_card_press_works_once`.
- `test_export_is_limited_to_once_a_day`: a complete export at T → `/export` at T + 23 h refused with the owner-local time; at T + 24 h allowed; a partial export does not count.
- `test_export_is_not_available_while_its_flag_is_off` (command, press and job; no help line while off).
- `test_export_is_never_an_agent_tool` (OD-20): no registered tool's name or handler reaches `privacy/export.py`, and no module under `src/assistant/agent/` imports it (AST).

**Unit** (`tests/unit/test_export_format.py`): `test_part_size_includes_the_envelope`; `test_values_convert_to_json_and_unknown_types_raise`; `test_file_names_are_ascii_and_match_the_gateway_rule`; `test_identifiers_outside_the_safe_pattern_are_refused`; `test_a_column_named_like_a_secret_is_excluded` (every live column whose name matches `(^|_)(token|secret|password|api_key)$` or contains `connection_id` is in `EXCLUDED_COLUMNS`; a guard for future columns. The anchored pattern keeps usage counts such as `llm_calls.input_tokens` exported).

**Scenarios:** QA-S7 (QE1) owns `tests/scenarios/m07/`. When a strict-xfail marked `awaiting M7-EXP` turns green, tell QE1, who removes the marker in a commit on this PR's branch (P3-interfaces §15.1).

Run locally: these four files plus `tests/integration/test_data_deletion.py::test_another_accounts_ids_are_not_found_by_deletion` and `tests/integration/test_managed_data_deletion.py::test_every_text_or_jsonb_column_is_classified`; ruff, format, mypy on `src/assistant/{privacy,telegram/control,jobs}`; `lint-imports`.

## 6. Acceptance criteria

1. `/export` asks first, warns that the files stay in Telegram, and works only with `export.enabled` on.
2. The export contains every class of the account (every `app` table is exported or listed as not exported, checked against the live schema), nothing of another account, no token, connection id or raw update.
3. Parts are standalone JSON, each ≤ `EXPORT_MAX_PART_MB` MiB (< 50 MB), built in memory from one snapshot, sent to the control chat only; no file is ever written to disk.
4. Every part's outcome is reported; an uncertain part is never resent; a requeued or repeated job never sends a second set of files.
5. At most one complete export per 24 hours.
6. Every named test in §5 passes in CI; RG-10, RG-20, RG-21 extended.
7. Audit rows `data_export_started`, `data_exported`, `data_export_failed` hold ids, counts and codes only.
8. QE2's privacy read is clean; ARCHITECTURE §6.7 and §10.4 updated.

## 7. Docs updated in the same PR

- ARCHITECTURE **§6.7**: the `/export` command, the confirm card, the once-a-day rule and the summary texts.
- ARCHITECTURE **§10.4**: an "Export" row: parts are built in memory and never written to disk on our side; the delivered files live in Telegram's chat history, outside Alviro's retention and deletion (§14.5); the three audit rows are kept like other audit rows (1 year).
- PR description: the class table and exclusions for QE2's privacy read; the decisions marked "(spec)".

## 8. Owner decisions

- **OD-20:** command only; a confirm card warning that the files stay in Telegram; JSON parts ≤ 45 MB built in memory; raw updates and tokens left out with a note; once a day; no delete button. A delete button needs a new gateway method and a control allow-list change (TF §4.2, 48 h limit); a ZIP or CSV changes only §3.2; including raw updates changes one exclusion row.
- **OD-24:** privacy policy v6 names the export and the fact that the files stay in Telegram; `export.enabled` is switched on only at enable 5, after v6 is published (P3-interfaces §10.3).

## 9. Risks and notes

- **Contract gap G12:** P3-interfaces §13 has no `data_export_started` event, no `card_id` in the export events and no `sent_parts`/`uncertain_parts` on `data_export_failed`; its `cause` codes are `interrupted`, `disabled`, `daily_limit`, `no_control_chat`, `busy`, `rate_limited`, `forbidden`, `chat_unreachable`, `build_failed`. §7.2 does not name the `ExportJobHandlers` field. All are needed for at-most-once across a requeued job; the TL adds them to the contract.
- **A long read-only snapshot** keeps old row versions alive while parts upload (minutes for a large account). It takes no row lock, so writers never wait; a migration's `ACCESS EXCLUSIVE` lock would wait for it, which deploys already avoid during jobs.
- **Upload timeouts** are reported as "not confirmed" and never resent; if they recur, lower `EXPORT_MAX_PART_MB` (GW-DOC §9).
- **New tables in later milestones** fail `test_export_covers_every_data_class` until they are classed or listed as not exported, which is the intent.
- **Real exports are owner-only.** No agent opens, prints or summarises one; LIVE-P3 checks only part count, sizes and delivery.
- **The control lock while an export runs:** owner requests (`owner_request` runs) and applied confirmations queue behind it for its duration (minutes for a large account). `/pause`, the stop word and the Pause button are not affected: the fast lane and the dispatcher do not take the job lock. Deletions wait too, which is the intent (no deleted data is exported after a deletion receipt).
- **The wrong-recipient guard** (§3.4 Tx 1 step 6) depends on the control chat being the owner's private chat; if a later milestone allows a control chat elsewhere, this check must change with it.
