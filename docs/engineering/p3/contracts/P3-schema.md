# P3 contract — schema 0015, content columns, retention, deletion cascade, post-restore

*Normative for every P3 unit. Implemented by **F0-S** parts (a)–(d) (Josh's team, author **J1**; J2 writes parts (e)–(k) in the same PR; see `P3-interfaces.md`). After CP1 this contract is frozen: changes go through an interface change request (ICR, WORKFLOW) and the schema steward (**J1**) issues the next revision, `0016`. Conventions are the repository's own (`db/base.py`, migrations 0012–0014): schema `app`, `bigint generated always as identity` primary keys, `timestamptz` UTC, enums as `text` + named CHECK constraints, FKs named `fk_<table>_<column>_<referred table>` with explicit `ondelete`, partial unique indexes named `ux_…`, hand-written and reviewed migrations with a data-handling note in the docstring (AGENTS §12). Sections that depend on an owner decision are tagged "(OD-n)"; they assume the default in `decisions.md` and only those sections change if the owner answers differently (R8). Scope: the F0-S row of README §3 (parts (a)–(d)); every existing name below was checked against `0012`–`0014` and the int/m05 ORM. The TL rulings of 2026-10-28 and 2026-10-29 (R10–R101) are applied; text they settled is marked "(Rn)" (R10–R31 are P3-T47…P3-T68 in `decisions.md` §B6, Rn = P3-T(n+37); R32 onward are the rows of §B7 and §B8 that cite them).*

## 1. Revision plan

| Revision | File | Revises | Content |
|---|---|---|---|
| `0015` | `migrations/versions/0015_p3.py` | `0014` | M06 + M07: new `delegations`, `delegation_conversations`, `availability_minutes`; changed `actions`, `memory_items`, `conversations`, `tasks`, `automations`; the legacy-takeover data step |

Rules:
- `0015` is the **only** P3 revision. F0-S checks that `alembic heads` shows `0014` on `int/p3` when it starts (both `main` after CP5 and int/m05 end at `0014`). A later schema change is `0016`, issued by J1 through an ICR.
- Exactly one head at all times: `test_there_is_exactly_one_alembic_head` asserts `["0015"]`.
- `0015` gets an up/down reverse test (`test_0015_p3_reverse`) and a data-step test in both directions (§9).
- ORM models mirror the DDL exactly (`Mapped[]`, `mapped_column`, same constraint names and predicates); `test_upgrade_creates_every_table_in_the_app_schema`, `test_identity_primary_keys_reject_a_client_supplied_id`, `test_timestamps_are_timezone_aware` and `test_every_tenant_table_has_an_account_id` pass with the updated sets in §9.
- No P2 code writes `actions.state = 'scheduled'` or `origin = 'delegation_auto'`. If a row in either state exists when `0015` runs, a new CHECK or unique index fails and the migration aborts; the TL investigates before retrying (fail closed).

**Order inside `upgrade()`:** (1) `availability_minutes`; (2) `delegations`; (3) `delegation_conversations`; (4) `actions` columns, FKs, CHECKs, indexes; (5) `memory_items` columns, FK, CHECK rewrite, indexes; (6) `conversations` columns, then the data step (§3.3); (7) `tasks`; (8) `automations`. `downgrade()` is §3.7.

**Docstring (exact data-handling note):**

```text
P3 F0-S: delegations, autonomy columns, follow-up and memory review storage.

Hand-written and reviewed (AGENTS §12). CONTENT: delegations.spec is new
and has registry, retention, deletion and export coverage; memory_items
.content now also holds shareable knowledge and inferred suggestions.
Data step: conversations whose autonomy_paused_reason is the legacy
'takeover' get takeover_at = migration time and the reason cleared, so a
takeover from before P3 never blocks a delegation (OD-5).
Data-handling note for the downgrade: it deletes every delegation and its
spec (content), every delegation-scope and shareable memory item, every
inferred memory item and unreviewed suggestion (content), the NFR-08
availability history, and task/automation refs and provenance; it marks
every chat with a recorded takeover as paused by takeover again (the M02
behaviour, fail closed).
```

## 2. New tables

### 2.1 `delegations` (ORM: `src/assistant/automation/delegation_models.py::Delegation`)

A row exists only after the owner confirmed the card: the unconfirmed spec lives in `tool_confirmations.args` (no `draft` status).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint identity | no | | PK `pk_delegations` |
| `account_id` | bigint | no | | FK `accounts.id` ON DELETE CASCADE (`fk_delegations_account_id_accounts`) |
| `kind` | text | no | | CHECK `ck_delegations_kind`: `kind IN ('time_bounded','standing')` |
| `spec` | jsonb | yes | | **CONTENT.** The validated `automation/delegation_rules.py::DelegationRules` with `schema_version` (P3-interfaces): chats, topic, allowed behaviours, limits, escalation, disclosure override, `knowledge_ids`. NULL once scrubbed by a deletion (§7). Never NULL while `status = 'active'` (CHECK below) |
| `refs` | jsonb | no | `'{}'` | Ids only, for deletion lookup, same shape as `automations.refs`: `{"conversation_ids": [...], "contact_ids": [...]}`. `conversation_ids` = exactly the chats the delegation covers (`DelegationRules.conversation_ids`); `contact_ids` = those chats' contacts plus every contact the spec names (`DelegationRules.knowledge_contact_ids`, the knowledge recipients) |
| `status` | text | no | `'active'` | CHECK `ck_delegations_status`: `status IN ('active','ended','cancelled')`. `cancelled` = ended before `starts_at` (it never covered anything); `ended` = ended at or after `starts_at`. Every writer that ends a delegation uses `CASE WHEN starts_at > :now THEN 'cancelled' ELSE 'ended' END` |
| `starts_at` | timestamptz | no | | Owner-confirmed start (≥ `confirmed_at`) |
| `ends_at` | timestamptz | yes | | NULL ⇔ `kind = 'standing'`. Enforced by policy (`active_covering`), not by the `delegation_end` automation firing |
| `confirmed_at` | timestamptz | no | | The owner's confirm tap (server time) |
| `ended_at` | timestamptz | yes | | Set with `status` ≠ `active` |
| `ended_reason` | text | yes | | CHECK `ck_delegations_ended_reason`: `ended_reason IS NULL OR ended_reason IN ('time_elapsed','owner_ended','superseded','data_deleted','connection_disabled','consent_revoked','restored_from_backup')`. Writers: `time_elapsed`, `connection_disabled`, `consent_revoked` M6-LIFE; `owner_ended`, `superseded` M6-DELEG; `data_deleted` §7; `restored_from_backup` §8 |
| `share_widened` | boolean | no | `false` | The card's "anyone raising this topic" tick (§9.3 `knowledge_scope`). Authoritative for readers (`shareable_for`); `DelegationRules.share_widened` is written with the same value in the same statement (M6-DELEG) |
| `run_id` | bigint | yes | | FK `agent_runs.id` ON DELETE SET NULL (`fk_delegations_run_id_agent_runs`). The owner run whose `create_delegation` call was confirmed |
| `tool_call_id` | text | yes | | With `run_id`, the idempotency key |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` | |

Constraints:
- `uq_delegations_run_id_tool_call_id` UNIQUE (`run_id`, `tool_call_id`) — NULLs distinct (the automations pattern).
- `ck_delegations_standing_has_no_end`: `(kind = 'standing') = (ends_at IS NULL)`.
- `ck_delegations_window`: `ends_at IS NULL OR ends_at > starts_at`.
- `ck_delegations_end_fields`: `(status = 'active' AND ended_at IS NULL AND ended_reason IS NULL) OR (status <> 'active' AND ended_at IS NOT NULL AND ended_reason IS NOT NULL)`.
- `ck_delegations_active_spec`: `status <> 'active' OR spec IS NOT NULL`.
- `ck_delegations_standing_one_chat`: `kind <> 'standing' OR COALESCE(jsonb_array_length(refs -> 'conversation_ids'), 0) = 1` (ARCH §10.3: a standing delegation covers exactly one chat; a missing key counts as 0 and fails, a non-array raises and fails).

Indexes:
- `ix_delegations_account_id_status` (`account_id`, `status`) — `list_active`, status block, sweep (account-leading, P2-T34).
- `ix_delegations_active_ends_at` (`ends_at`) WHERE `status = 'active' AND ends_at IS NOT NULL` — the M6-LIFE overdue reconciler, a cross-account system scan (precedent `ix_automations_due`).

### 2.2 `delegation_conversations` (ORM: `automation/delegation_models.py::DelegationConversation`)

One row per covered chat. It makes "which delegation covers chat X now" an indexed, fail-closed lookup and enforces **one live delegation per chat** in the database (OD-7).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint identity | no | | PK `pk_delegation_conversations` |
| `account_id` | bigint | no | | FK `accounts.id` ON DELETE CASCADE (`fk_delegation_conversations_account_id_accounts`) |
| `delegation_id` | bigint | no | | FK `delegations.id` ON DELETE CASCADE (`fk_delegation_conversations_delegation_id_delegations`) |
| `conversation_id` | bigint | no | | FK `conversations.id` ON DELETE CASCADE (`fk_delegation_conversations_conversation_id_conversations`). Privacy deletion never deletes a conversation row, so links survive a chat deletion as ended rows |
| `created_at` | timestamptz | no | `now()` | |
| `ended_at` | timestamptz | yes | | NULL = live link |

Constraints and indexes:
- `uq_delegation_conversations_delegation_id_conversation_id` UNIQUE (`delegation_id`, `conversation_id`).
- `ux_delegation_conversations_live` UNIQUE (`conversation_id`) WHERE `ended_at IS NULL` — at most one live delegation per chat.
- `ix_delegation_conversations_account_id_conversation_id` (`account_id`, `conversation_id`).

Rules (enforced in code, tested in §9):
- Rows are inserted in the confirm transaction with the delegation row. Every writer that ends a delegation sets `ended_at = :now` on all its live rows in the same transaction. A live row of a non-active delegation is a bug; `active_covering` (P3-interfaces) still fails closed because it also checks `delegations.status` and the window.
- **Supersede (M6-DELEG, OD-7; TL-ruled R11):** confirming a newer delegation first ends the older one's live row for each shared chat and cancels the older one's pending autonomous actions in those chats (`cancel_pending_for_delegations(..., conversation_ids=<shared chats>, reason=delegation_ended)`, §7.1), in the same transaction, lock order account → conversation → delegation → action; an older delegation left with no live row ends with `superseded` through `end_delegations`. Without the cancel, the older delegation's in-flight action would hold `ux_actions_autonomous_inflight` and the newer delegation's first reply in that chat would be dropped as "already proposed".
- The account of the row equals the account of both its delegation and its conversation (checked by the writer; `active_covering` filters on all three).

### 2.3 `availability_minutes` (ORM: `src/assistant/db/models.py::AvailabilityMinute`)

The NFR-08 recorder store (decision: a recorded-minutes table, not log scraping; P3-interfaces §4.4): one row per UTC minute in which the poller completed a `getUpdates` and committed what it got. A **system table** with no `account_id`: it describes the deployment, not a tenant, and holds no content.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `minute` | timestamptz | no | | PK `pk_availability_minutes` (a natural key, like `system_state.key`; no identity column). The poll time truncated to the minute in UTC by the writer (`now.replace(second=0, microsecond=0)` on a UTC `Clock` value); no CHECK, because `date_trunc` on `timestamptz` is not immutable |
| `polls` | integer | no | `1` | CHECK `ck_availability_minutes_polls`: `polls >= 1`. Successful polls in that minute (diagnostics only) |

Writer: OPS-AV, through F0-S's `PollObserver` hook, called after the `_mark_poll_ok` commit in its own transaction: `INSERT ... ON CONFLICT (minute) DO UPDATE SET polls = availability_minutes.polls + 1`. Readers: `deploy/sql/nfr08.sql` (OPS-AV), the trial report (OPS-VIEWS). Retention 90 days (§6).

## 3. Changes to existing tables

### 3.1 `actions` (ORM: `actions/models.py::Action`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `delegation_id` | bigint | yes | | FK `delegations.id` ON DELETE SET NULL (`fk_actions_delegation_id_delegations`). Set on every `delegation_auto` action and on every held reply, the autonomy-fallback `agent_proposal` row (M6-PLAN; the conversion held replies and the `disclosure_missing` re-proposal of M6-TXA and M6-SCHED copy it); NULL otherwise. NULL on a `delegation_auto` row means its delegation row was deleted (retention or "delete everything"). No CHECK requires it: the SET NULL would then fail |
| `scheduled_for` | timestamptz | yes | | Set at `approved→scheduled` by M6-SCHED's `actions/release.py::schedule_autonomous` (called by M6-PLAN's `replies.delegated` job, P3-interfaces §4.1; so M6-PLAN depends on M6-SCHED, R18): the scheduling time + `AUTONOMY_SEND_DELAY_SECONDS`. Kept after release |
| `late_after` | timestamptz | yes | | `scheduled_for + AUTONOMY_MAX_LATENESS_SECONDS`, stored at scheduling so the verifier needs no config. Tx A after it ⇒ `cancelled/late` (M6-TXA) |
| `trigger_automation_id` | bigint | yes | | FK `automations.id` ON DELETE SET NULL (`fk_actions_trigger_automation_id_automations`). The J7 nudge's follow-up automation, set by `propose(trigger=…)` (NUDGE-POL) |
| `disclosure_included` | boolean | no | `false` | Set by M6-TXA in the claim, for `delegation_auto` only: true iff the exact text claimed for sending contains the application-inserted disclosure. Every other origin keeps `false` even if its text happens to start with the AI marker (the CHECK below would reject the claim otherwise). The content-free marker OPS-TV3 checks |

Constraints:
- `ck_actions_disclosure_origin`: `origin = 'delegation_auto' OR NOT disclosure_included` — invariant 13 and PRD §11.4 in the schema: an owner-approved draft never carries the disclosure (OD-4).
- `ck_actions_trigger_not_autonomous`: `origin <> 'delegation_auto' OR trigger_automation_id IS NULL` — a nudge is never an autonomous send (OD-18).
- `ck_actions_schedule_pair`: `(scheduled_for IS NULL) = (late_after IS NULL)`.
- `ck_actions_late_after_order`: `late_after IS NULL OR late_after >= scheduled_for`.
- `ck_actions_scheduled_has_time`: `state <> 'scheduled' OR scheduled_for IS NOT NULL`.

Indexes:
- `ix_actions_scheduled` (`scheduled_for`) WHERE `state = 'scheduled'` — the due-scheduled reconciler (M6-SCHED; cross-account system scan).
- `ux_actions_autonomous_inflight` UNIQUE (`conversation_id`) WHERE `origin = 'delegation_auto' AND state IN ('proposed','approved','scheduled','executing')` — at most one autonomous action in flight per chat.
- `ux_actions_delegation_base` UNIQUE (`delegation_id`, `base_message_id`) WHERE `origin = 'delegation_auto' AND state_reason IS DISTINCT FROM 'disclosure_missing'` — one autonomous reply per delegation and contact message, ever (a reconciler re-run after `succeeded` cannot send twice). A row cancelled with `disclosure_missing` leaves the index, so M6-TXA's re-proposal for the same base is allowed, **once** (same body + disclosure, no model call); a second `disclosure_missing` on the re-proposal becomes a held reply (OD-11), never a second re-proposal (§11 R-1, TL-ruled R10; named test in M6-TXA). NULL `base_message_id` (message deleted) is distinct.
- `ix_actions_account_id_delegation_id` (`account_id`, `delegation_id`) WHERE `delegation_id IS NOT NULL` — per-delegation counters under the delegation lock (ADR-20), activity, bulk cancel by delegation.
- `ix_actions_account_id_trigger_automation_id` (`account_id`, `trigger_automation_id`) WHERE `trigger_automation_id IS NOT NULL` — the nudge lookup and the OPS-VIEWS followups view. Not unique: a reopened follow-up may nudge again (M7-NUDGE owns its idempotency).

`draft_flags` and `policy_decision` stay jsonb with no schema change; their new keys are backward-compatible additions defined in P3-interfaces §2.4: in `draft_flags`, the delegated draft's flags (`in_scope`, `behaviour`, `identity_question`, `escalation_category`, `knowledge_ids`), `identity_answer`, `disclosure_required`, `disclosure_added`, `autonomy_codes`, `shadow`, `converted_from`, `reproposed_from`, `was_due_at`, `presentation` (`"on_request"` on a held reply, OD-11); in `policy_decision`, a check for each of the ten `AUTONOMY_RULES` (P3-interfaces §3.2).

### 3.2 `memory_items` (ORM: `memory/models.py::MemoryItem`)

New columns:

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `delegation_id` | bigint | yes | | FK `delegations.id` ON DELETE CASCADE (`fk_memory_items_delegation_id_delegations`). Set only for `scope = 'delegation'` |
| `category` | text | yes | | A code, never content (OD-15). Required for `agent_inferred` rows; optional for owner rows |

CHECK rewrite. `upgrade()` drops and re-creates the five existing constraints with the same names (every existing row satisfies the new text), then adds the new ones. Exact SQL, in the 0012 spelling (`', '` separators):

| Constraint | Expression |
|---|---|
| `ck_memory_items_scope` | `scope IN ('owner', 'contact', 'conversation', 'delegation')` |
| `ck_memory_items_kind` | `kind IN ('instruction', 'preference', 'fact', 'style', 'shareable')` |
| `ck_memory_items_source` | `source IN ('owner_stated', 'agent_inferred')` |
| `ck_memory_items_status` | `status IN ('active', 'archived', 'pending_review')` |
| `ck_memory_items_scope_target` | `(scope = 'owner' AND contact_id IS NULL AND conversation_id IS NULL AND delegation_id IS NULL) OR (scope = 'contact' AND contact_id IS NOT NULL AND conversation_id IS NULL AND delegation_id IS NULL) OR (scope = 'conversation' AND conversation_id IS NOT NULL AND contact_id IS NULL AND delegation_id IS NULL) OR (scope = 'delegation' AND delegation_id IS NOT NULL AND contact_id IS NULL AND conversation_id IS NULL)` |
| `ck_memory_items_category` (new) | `category IS NULL OR category IN ('work', 'project', 'schedule', 'logistics', 'communication_preference', 'relationship_role', 'other_safe')` (OD-15) |
| `ck_memory_items_shareable_source` (new) | `kind <> 'shareable' OR source = 'owner_stated'` |
| `ck_memory_items_inferred_category` (new) | `source <> 'agent_inferred' OR category IS NOT NULL` |
| `ck_memory_items_delegation_kind` (new) | `scope <> 'delegation' OR kind = 'shareable'` |
| `ck_memory_items_inferred_scope` (new) | `source <> 'agent_inferred' OR scope IN ('contact', 'conversation')` |
| `ck_memory_items_pending_review_source` (new) | `status <> 'pending_review' OR source = 'agent_inferred'` |

Unchanged: `ck_memory_items_archived_at`, `ck_memory_items_applies_to_replies`, `uq_memory_items_run_id_tool_call_id`, the three 0012 indexes.

**Sensitive categories are absent from the CHECK on purpose** (OD-15): `health`, `religion`, `politics`, `sexuality`, `finances` (including debts and salary), `ethnicity`, `legal`, `union_membership`, `biometric`, `immigration`, `precise_location`. The model's `MemoryCandidates.category` enum (M7-MEX) offers the allowed values plus these, so the model can label a sensitive candidate; SENS drops it before any insert, and the CHECK rejects it if a filter bug lets it through (the database half of RG-15). The allowed list is a TL proposal that goes to the owner as part of OD-15 and must be settled before CP1 (R13); a different owner answer before CP1 changes only this tuple and the SENS tables, and a change after CP1 is revision `0016`.

New indexes:
- `ix_memory_items_delegation_id` (`delegation_id`) WHERE `delegation_id IS NOT NULL` — the FK cascade and the end-of-delegation purge.
- `ix_memory_items_account_id_review` (`account_id`, `created_at`) WHERE `status = 'pending_review'` — the review queue, the review cards `memory.extract` enqueues (at most 5 per account per owner-local day, no periodic job; R19, P3-interfaces §11.2) and the 14-day sweep.

Meanings:
- **Shareable knowledge** (`kind = 'shareable'`, always `owner_stated`): `scope = 'delegation'` = knowledge for one delegation only, purged when it ends; `scope = 'owner'` = saved for anyone the owner designates; `scope = 'contact'` / `'conversation'` = saved for that contact or chat (OD-8). Text from a forwarded or contact message becomes shareable only through a card that showed the exact text (M6-DELEG); `provenance` then keeps `tainted`/`managed` true and gains `"owner_confirmed_exact": true` (P3-interfaces §6.2 `shareable_for`; a backward-compatible jsonb addition, no DDL).
- **Inferred suggestions** (`source = 'agent_inferred'`, `status = 'pending_review'` until the owner approves, edits or discards; M7-MEX, M7-MRV): contact- or conversation-scoped, with `provenance = {"tainted": true, "managed": true, "refs": {...}, "source_message_ids": [...]}` (ids only; `source_message_ids` is a backward-compatible key addition). `memory/items.py`'s prompt loaders already filter `status = 'active'`, so a `pending_review` row never reaches a prompt. Only the owner's taps on the review card (M7-MRV) move a suggestion out of `pending_review`: F0-S makes `memory/items.py::forget` treat a `pending_review` row as not found (otherwise a forget followed by its Undo, `set_status(active=True)`, would activate an unreviewed suggestion; invariant 3b). `set_status` already moves only between `active` and `archived`.
- **Shareable items stay out of other prompts** (R54): `memory/items.py::contact_facing` excludes `kind = 'shareable'` for every run (Assist, `extract_memory`, summary, draft and delegated runs); shareable text reaches a prompt only through `shareable_for` (P3-interfaces §6.2). F0-S adds the one predicate (named touch point, P3-interfaces §1.2).

### 3.3 `conversations` (ORM: `conversations/models.py::Conversation`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `takeover_at` | timestamptz | yes | | The newest owner manual message in this chat (Telegram `date`), written by M6-TAKE separately from `autonomy_paused_reason`. A takeover blocks autonomy only if `takeover_at > max(delegation.confirmed_at, autonomy_resumed_at)` (OD-5, ADR-19) |
| `autonomy_resumed_at` | timestamptz | yes | | The owner's "Resume here" tap on the one-time takeover notice (M6-TAKE; card only, never a tool) |
| `pending_delegation_at` | timestamptz | yes | | `replies.delegated` trigger flag, set through the `DelegationRequester` and read by the reconciler (M6-PLAN; ADR-06 pattern of `pending_triage_at`). M6-SCHED sets it again when ingestion cancels an autonomous action because of a new contact message or an edit of the answered message, so a reply cancelled during the send delay is requested again (a second writer; P3-interfaces §2.2) |
| `pending_memory_at` | timestamptz | yes | | `memory.extract` trigger flag and reconciler input (M7-MEX) |

No new index (the `pending_*_at` reconcilers scan like `pending_triage_at`, which has none). `ck_conversations_autonomy_paused_reason` is **unchanged**: `'takeover'` stays allowed so a downgrade can write it back. After `0015` no code writes `autonomy_paused_reason = 'takeover'` (M6-TAKE moves the writer in `conversations/service.py`; its RG-19 test asserts this). `AUTONOMY_PAUSE_REASONS` keeps both values.

**Data step (OD-5), exact SQL, in `upgrade()` after the columns exist:**

```sql
UPDATE app.conversations
SET takeover_at = now(), autonomy_paused_reason = NULL
WHERE autonomy_paused_reason = 'takeover';
```

`now()` is the migration transaction's time. Every delegation is confirmed later, so a pre-P3 takeover can never block one, and `/status` can still show that a takeover happened. `updated_at` is not touched; no audit row is written (the docstring records the step). Rows with `ingest_error` keep it.

### 3.4 `tasks` (ORM: `inbox/models.py::Task`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `refs` | jsonb | no | `'{}'` | Ids only, `{"conversation_ids": [...], "contact_ids": [...]}`: every chat and contact the task names, **plus the origin chats and contacts of a tainted or managed run that wrote the text** (the run's untrusted turns and tool results), so per-chat and per-contact deletion finds owner reminders and to-dos with `conversation_id` NULL, and text that may derive from a chat. M7-AUTO adds the same origin ids to `automations.refs` when it edits recurring-reminder text. Written by M7-REM, M7-FUP, M7-MACT; triage keeps relying on `conversation_id` |
| `tool_call_id` | text | yes | | With `run_id`, the idempotency key of an owner task created by a tool |
| `provenance` | jsonb | no | `'{}'` | Flags and ids, the `memory_items.provenance` shape: `{"tainted": bool, "managed": bool, "refs": {...}}` (m07-arch G5b; from `agent/tools/memory.py::run_origin`). `'{}'` reads as `{"tainted": created_by == 'agent', "managed": created_by == 'agent'}`: a triage task's title is model-written from contact text. Every P3 unit writes the flags from `run_origin` unchanged, so a held call counts as tainted (R68) |

Index: `ux_tasks_run_id_tool_call_id` UNIQUE (`run_id`, `tool_call_id`) WHERE `tool_call_id IS NOT NULL`.

`created_by = 'owner'` (already in `ck_tasks_created_by`) is first written in P3. No other CHECK changes; `snoozed_until` already exists (0014).

Owner task shapes (no new `kind` value; `ck_tasks_kind` is unchanged):
- **One-off reminder:** a task with `kind = 'reminder'` plus a `reminder` automation with `task_id` set. Its text lives in `tasks.title`, always with a task (R12); §7.2 step 7 cancels the automation if the task is deleted.
- **Recurring reminder:** a `reminder` automation with `rrule` set and no task; its text is `ReminderRule.text` in `automations.spec` (P3-interfaces §8.2).
- **To-do without a time (OD-17):** a task with `kind = 'reminder'` and `due_at` NULL, and no automation (R20).

### 3.5 `automations` (ORM: `automation/models.py::Automation`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `paused_reason` | text | yes | | CHECK `ck_automations_paused_reason`: `paused_reason IS NULL OR paused_reason IN ('owner','invalid')`. Meaningful only while `status = 'paused'`; a stale value on another status is ignored. M7-AUTO writes `owner` on pause and clears it on resume. A paused row with NULL: kind `digest` = the owner switched the digest off (M05 `_sync_digest`); any other kind = an invalid stored rule (M05 `_pause_invalid`, fail closed; resume not offered). M7-AUTO's resume rule and SET-P3's labels therefore treat a paused row of any kind but `digest` whose `paused_reason` is not `owner` (NULL or `invalid`) as an invalid rule. No backfill, so the M05 writers keep working unchanged |
| `provenance` | jsonb | no | `'{}'` | Flags and ids, the same shape as `tasks.provenance`: owner text written by a tainted or managed run (M7-REM, M7-FUP, M7-AUTO, M7-SSUM). `'{}'` reads as untainted, owner-authored (every M05 automation was owner-confirmed on a card or is the digest) |

The CHECK is value-only on purpose: M05's `automation/monitors.py::stop` (the `stop_monitor` tool) and `automation/schedules.py::_sync_digest` change `status` without knowing the column, and a status-coupled CHECK would make them fail.

### 3.6 Unchanged (stated so nobody adds a revision for them)

| Table | Why no change |
|---|---|
| `agent_runs.run_type` | Delegated drafts keep run type `draft_reply` with their own prompt key `draft_delegated` (OD-9). *If OD-9 is answered "separate run type":* `0015` also rewrites `ck_agent_runs_run_type` to add `'delegated_reply'` and `agent/models.py::RUN_TYPES` gains it; nothing else here changes |
| `notifications` | `kind` has no CHECK; `refs` gains `delegation_ids` and `memory_ids` as backward-compatible jsonb keys (`NotificationRefs.delegation_ids`, `NotificationRefs.memory_ids`, P3-interfaces §11.1; R75). `automation_ids` already exists (M05); specs that write `automation_id` mean that field |
| `pending_inputs`, `audit_log`, `tool_confirmations` | `kind`, `event` and `tool_name` have no CHECK; new values (`mem_edit`, `deleg_wording`, P3 audit events, P3 tool names) need no DDL |
| `accounts.settings` | No disclosure text here: defaults live in `core/autonomy_texts.py`, overrides in `delegations.spec` (P3-interfaces §5.2) |
| `automation_occurrences`, `digests`, `conversation_triage`, `messages` | Not touched by P3 |

### 3.7 Downgrade (exact order)

1. `memory_items`: `DELETE FROM app.memory_items WHERE scope = 'delegation' OR kind = 'shareable' OR source = 'agent_inferred' OR status = 'pending_review';` then drop `ix_memory_items_account_id_review`, `ix_memory_items_delegation_id`, the six new CHECKs, `fk_memory_items_delegation_id_delegations`, the columns `category` and `delegation_id`; drop and re-create `ck_memory_items_scope`, `_kind`, `_source`, `_status`, `_scope_target` with their exact 0012 text.
2. `actions`: drop the five indexes, the five CHECKs, the two FKs and the five columns. Rows stay (0013 already allows `delegation_auto` and `scheduled`).
3. `automations`: drop `ck_automations_paused_reason`, `paused_reason`, `provenance`. P3 rule specs no longer validate under M05 code and those rows pause as invalid (fail closed).
4. `tasks`: drop `ux_tasks_run_id_tool_call_id`, `refs`, `tool_call_id`, `provenance`.
5. `conversations`: restore the legacy pause, then drop the four columns:
   ```sql
   UPDATE app.conversations
   SET autonomy_paused_reason = 'takeover'
   WHERE takeover_at IS NOT NULL AND autonomy_paused_reason IS NULL;
   ```
6. Drop `delegation_conversations`, then `delegations`, then `availability_minutes`.

Deleting shareable and inferred items (rather than converting them to `fact`) is deliberate: a conversion would widen where the text is used.

## 4. ORM mirrors

Every changed model mirrors §2–§3 exactly. Constraint `name=` values follow the naming convention in `db/base.py` (`name="disclosure_origin"` becomes `ck_actions_disclosure_origin`; indexes keep their full names). CHECK text in the ORM is built from the tuples below so model and migration cannot drift; `test_p3_check_constraints_match_the_model_tuples` (§9) compares them with the live schema.

| File | Change | Constants it must expose |
|---|---|---|
| `automation/delegation_models.py` (new) | `Delegation`, `DelegationConversation` (§2.1, §2.2); a pure helper `ended_status(starts_at: datetime, now: datetime) -> str` (`"cancelled"` if `starts_at > now`, else `"ended"`), the Python twin of the SQL `CASE` every ender uses | `KIND_TIME_BOUNDED`, `KIND_STANDING`, `DELEGATION_KINDS = ("time_bounded", "standing")`; `STATUS_ACTIVE`, `STATUS_ENDED`, `STATUS_CANCELLED`, `DELEGATION_STATUSES = ("active", "ended", "cancelled")`; `END_TIME_ELAPSED`, `END_OWNER_ENDED`, `END_SUPERSEDED`, `END_DATA_DELETED`, `END_CONNECTION_DISABLED`, `END_CONSENT_REVOKED`, `END_RESTORED_FROM_BACKUP`, `DELEGATION_END_REASONS` (that order) |
| `actions/models.py` | `Action` gains the five columns of §3.1, the five CHECKs and the five indexes | `AUTONOMOUS_INFLIGHT_STATES = ("proposed", "approved", "scheduled", "executing")`, used to build the `ux_actions_autonomous_inflight` predicate |
| `memory/models.py` | `MemoryItem` gains `delegation_id`, `category`, the rewritten and new CHECKs, two indexes. The "not built yet" comments go | `SCOPE_DELEGATION`; `MEMORY_SCOPES` (+ `delegation`); `KIND_SHAREABLE`; `MEMORY_KINDS` (+ `shareable`); `SOURCE_OWNER_STATED`, `SOURCE_AGENT_INFERRED`, `MEMORY_SOURCES`; `STATUS_PENDING_REVIEW`; `MEMORY_STATUSES` (`active`, `archived`, `pending_review`); `CATEGORY_OTHER_SAFE`; `MEMORY_CATEGORIES = ("work", "project", "schedule", "logistics", "communication_preference", "relationship_role", "other_safe")`; `SENSITIVE_MEMORY_CATEGORIES = ("health", "religion", "politics", "sexuality", "finances", "ethnicity", "legal", "union_membership", "biometric", "immigration", "precise_location")` (never stored; read by SENS and M7-MEX) (OD-15) |
| `conversations/models.py` | `Conversation` gains `takeover_at`, `autonomy_resumed_at`, `pending_delegation_at`, `pending_memory_at` | `AUTONOMY_PAUSE_REASONS` unchanged; `LEGACY_TAKEOVER_REASON = "takeover"` with the comment "kept in the CHECK for downgrade; never written after 0015" |
| `inbox/models.py` | `Task` gains `refs`, `tool_call_id`, `provenance`, `ux_tasks_run_id_tool_call_id` | — |
| `automation/models.py` | `Automation` gains `paused_reason` (+ CHECK), `provenance` | `PAUSED_BY_OWNER = "owner"`, `PAUSED_INVALID = "invalid"`, `AUTOMATION_PAUSED_REASONS` |
| `db/models.py` | `AvailabilityMinute` (§2.3). The module docstring now names two deployment tables exempt from tenant scoping, `system_state` and `availability_minutes` | — |
| `migrations/env.py` | `import assistant.automation.delegation_models` joins the model import list (autogenerate must see the new tables) | — |

## 5. Content-column registry (`privacy/content_columns.py`)

Add to `CONTENT_COLUMNS`:
- `("delegations", "spec")` — comment: "Delegation rules: topic, behaviours, knowledge ids, disclosure override (owner words; P3)".

Add to `NON_CONTENT_COLUMNS` every other new `text`/`jsonb` column:
- `("delegations", "kind")`, `("delegations", "refs")`, `("delegations", "status")`, `("delegations", "ended_reason")`, `("delegations", "tool_call_id")`;
- `("memory_items", "category")`;
- `("tasks", "refs")`, `("tasks", "tool_call_id")`, `("tasks", "provenance")`;
- `("automations", "paused_reason")`, `("automations", "provenance")`.

`delegation_conversations`, `availability_minutes` and the new `actions`/`conversations` columns have no `text`/`jsonb` column (ids, timestamps, booleans, an integer). `test_every_text_or_jsonb_column_is_classified` then passes against the live schema.

What changes meaning without a new column (each already registered, already covered by retention and deletion):
- `memory_items.content` now also holds shareable knowledge (owner words) and inferred suggestions (model-written from contact text).
- `tasks.title`/`details` now also hold owner reminders and to-dos (owner words, possibly naming contacts).
- `automations.spec` now also holds recurring-reminder text, follow-up expectations and scheduled-summary scopes (P3-interfaces rule models).
- `actions.text` of a `delegation_auto` action holds the reply including the application-inserted disclosure.

## 6. Retention (ARCHITECTURE §10.4 rows; `privacy/retention.py`)

| Data class | Rule | Sweep implementation |
|---|---|---|
| `delegations` rows (with `spec`) | Active: until ended. Ended or cancelled: 1 year after `ended_at`, then deleted | Delete rows with `status IN ('ended','cancelled') AND ended_at < now − ACCOUNTING_RETENTION` (365 d). Cascades: `delegation_conversations`, any delegation-scope `memory_items`; `actions.delegation_id` becomes NULL (those actions are at least as old and are swept first, see order below) |
| `delegation_conversations` | With its delegation | FK cascade only; no rule of its own |
| Delegation-scope knowledge (`memory_items.scope = 'delegation'`) | Until its delegation ends | Deleted by `end_delegations` (§7.1) when the delegation ends; the sweep is the backstop: delete items whose delegation is not `active` **or** has `ends_at <= now` (an overdue delegation no longer covers anything, even before M6-LIFE's reconciler ends it) |
| Saved shareable knowledge (`kind = 'shareable'`, owner/contact/conversation scope) | Until deleted (PRD §13) | No sweep; deletion cascade and the archived-item rule (24 h after forget) as for every memory item |
| Unreviewed suggestions (`memory_items.status = 'pending_review'`) | 14 days from `created_at` (OD-16) | Delete rows with `status = 'pending_review' AND created_at < now − PENDING_REVIEW_RETENTION` |
| Approved inferred items (`source = 'agent_inferred'`, `status = 'active'`) | Until deleted, like every memory item | Existing rules |
| `availability_minutes` | 90 days (NFR-08 needs one month plus the 14-day trial window) | Delete rows with `minute < now − AVAILABILITY_RETENTION` (system table: one statement, no account scope) |
| `tasks.refs`/`provenance`, `automations.provenance`/`paused_reason` | With their row (ids, flags, codes) | Existing rules: open tasks until deleted, closed tasks 1 year after `closed_at`; ended automations 1 year after `ended_at`. Owner tasks are covered by the same statement as agent tasks |
| New `actions` columns, new `conversations` columns | With their row | Existing rules (actions 1 year; conversations kept, content-free) |

Constants in `privacy/retention.py`: `PENDING_REVIEW_RETENTION = timedelta(days=14)` (OD-16) and `AVAILABILITY_RETENTION = timedelta(days=90)`; delegations reuse `ACCOUNTING_RETENTION`.

Implementation:
- New `sweep_p3(session_factory, clock) -> dict[str, int]`, called from `sweep_all` **after** `sweep_p2_rows` (so an action that references a delegation is gone before the delegation row is) and before `sweep_contact_names`. Per account, in its own short transaction, with an explicit `account_id` predicate on every statement (AGENTS §9, the `sweep_p2_rows` pattern): `delegations`, `delegation_knowledge`, `pending_reviews`. Then one system statement for `availability_minutes`.
- The backstop statement, exactly: `DELETE FROM app.memory_items m USING app.delegations d WHERE m.account_id = :account_id AND d.account_id = :account_id AND m.scope = 'delegation' AND m.delegation_id = d.id AND (d.status <> 'active' OR d.ends_at <= :now)`.
- `SweepReport` gains `delegations`, `delegation_knowledge`, `pending_reviews`, `availability_minutes` (default 0), all in `total`; `JobHandlers.retention_sweep` logs them (counts only). The module docstring's table gains the four rows.

## 7. Deletion cascade (`privacy/p3_deletion.py::purge_p3`)

### 7.1 Shared primitives (F0-S, used by deletion, post-restore and the M06 units)

**`actions/lifecycle.py::cancel_pending_for_delegations(session, *, account_id: int, delegation_ids: Sequence[int], reason: StateReason, now: datetime, conversation_ids: Sequence[int] | None = None) -> list[int]`.** Cancels `PENDING` actions (the M04 set, which includes `scheduled`) with `origin = 'delegation_auto'` and `delegation_id` in the set, limited to `conversation_ids` when given (supersede, §2.2; `None` = every chat), with the audit rows `_change_pending` already writes (`action_cancelled`, ids and codes only). `_change_pending` already filters by `conversation_ids` and `origins: frozenset[ActionOrigin] | None` (M04); F0-S adds one keyword-only filter, `delegation_ids: Sequence[int] | None = None`, whose default keeps every current caller unchanged, and calls it with `origins=frozenset({ActionOrigin.DELEGATION_AUTO})`. `executing` is never touched (a committed claim). M6-DELEG, M6-SCHED and M6-LIFE reuse it; this is a named touch point in a never-edit file, written by J1 in F0-S (§11 R-6).

**`automation/delegations_repo.py::end_delegations(session, *, account_id: int, delegation_ids: Sequence[int], reason: str, now: datetime) -> list[int]`** (J1 adds it beside J2's read seams). The one way a delegation ends; the caller takes the locks it needs in the §9.4 order (account → conversation → delegation → action; a level may be skipped, as the overdue reconciler skips the conversation, but never taken in reverse) and owns the transaction. In order:
1. `UPDATE app.delegations SET status = CASE WHEN starts_at > :now THEN 'cancelled' ELSE 'ended' END, ended_reason = :reason, ended_at = :now, updated_at = :now WHERE account_id = :account_id AND id = ANY(:ids) AND status = 'active' RETURNING id` — the returned ids are the ones actually ended (idempotent: an ended row is skipped).
2. `UPDATE app.delegation_conversations SET ended_at = :now WHERE account_id = :account_id AND delegation_id = ANY(:ended) AND ended_at IS NULL`.
3. `cancel_pending_for_delegations(..., delegation_ids=ended, reason=StateReason.DELEGATION_ENDED)`.
4. `DELETE FROM app.memory_items WHERE account_id = :account_id AND scope = 'delegation' AND delegation_id = ANY(:ended)`.
5. One content-free audit row per ended delegation: event `delegation_ended`, `subject_type` `delegation`, actor `system` (or `owner` for `owner_ended`), details `reason` (the `ended_reason` value) and `to` (`ended` / `cancelled`) (P3-interfaces §13).

It never enqueues anything. After a normal end (`owner_ended`, `superseded`: M6-DELEG; `time_elapsed`, `consent_revoked`, `connection_disabled`: M6-LIFE) the caller requests the `delegation_catch_up` for each returned id through `P3Ports.catch_up_requester` (P3-interfaces §6.1, §11.2); deletion and restore never request a catch-up (the data it would describe is gone or rewound).

### 7.2 `purge_p3`

```python
async def purge_p3(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Sequence[int] | None,
    contact_ids: Sequence[int],
    scope: Literal["scoped", "everything", "managed"],
    now: datetime,
) -> dict[str, int]
```

Same arguments as `purge_p2`. Called **immediately after `purge_p2`**, in the same transaction, at its four call sites in `privacy/deletion.py`: `delete_managed_data` (`managed`), `purge_conversation` (`scoped`, the bot-chat purge), `_purge_chats` (`scoped`; covers `delete_conversation_data` and `delete_contact_data`) and `delete_everything` (`everything`). Every statement carries `account_id = :account_id`. Delegations are locked `FOR UPDATE` ordered by id after the conversation locks the caller already holds. Returns `delegations_ended`, `delegations_deleted`, `memory_items`, `tasks`, `automations`, `notifications`, `automations_cancelled`.

**"Naming the scope"** (scoped only): a delegation names the scope if `refs @> '{"conversation_ids": [c]}'` for a scope chat `c`, or `refs @> '{"contact_ids": [k]}'` for a scope contact `k`, or any `delegation_conversations` row of it (live or ended) has `conversation_id` in the scope. Active, ended and cancelled delegations all count.

| Step | `scoped` (chat, contact, bot-chat purge) | `managed` | `everything` |
|---|---|---|---|
| 1. Delegations | `end_delegations(named active ids, reason='data_deleted')`, then `spec = NULL, updated_at = now` on **every** named delegation (active ones are ended first, so `ck_delegations_active_spec` holds) | `end_delegations(every active id, reason='data_deleted')`; `spec` is kept (owner-authored, like automations in P2's managed scope) | `DELETE FROM app.delegations WHERE account_id = :account_id` (links and knowledge cascade; `actions.delegation_id` becomes NULL; pending autonomous actions were already cancelled by `purge_p2`) |
| 2. Delegation-scope knowledge | Delete the items of every named delegation (step 1 removed the active ones') | Removed by step 1 | Removed by the cascade (and by `items.delete_all` before) |
| 3. Inferred memory | Covered by the existing chat/contact item helpers (inferred items are contact- or chat-scoped by CHECK) | `DELETE ... WHERE source = 'agent_inferred'` (normally 0 after `items.delete_about_contacts`; kept so the rule does not depend on the CHECK) | Covered by `items.delete_all` |
| 4. Owner tasks | Delete tasks whose `refs` name a scope chat or contact (any `created_by`) | Delete tasks with `created_by = 'owner' AND provenance @> '{"managed": true}'` | Already deleted by `purge_p2` |
| 5. Automations | Already deleted by `purge_p2` when `refs` name the scope | Delete automations with `kind <> 'digest' AND provenance @> '{"managed": true}'` | Already deleted by `purge_p2` (all but the digest) |
| 6. Notifications | Delete notifications whose `refs @> '{"delegation_ids": [d]}'` for a named delegation (a queued catch-up must not describe deleted data) | Already deleted by `purge_p2` | Already deleted by `purge_p2` |
| 7. Orphaned follow-ups and one-off reminders (every scope) | `UPDATE app.automations SET status = 'cancelled', ended_at = :now, updated_at = :now WHERE account_id = :account_id AND (kind = 'follow_up' OR (kind = 'reminder' AND rrule IS NULL)) AND task_id IS NULL AND status IN ('active','paused')` — a follow-up and a one-off reminder always have a task (ARCH §10.3 mapping; binding on M7-FUP and M7-REM: one-off reminder text lives in `tasks.title`, R12), so a NULL `task_id` means the task was deleted; a recurring reminder (`rrule` set) has no task by design, and a to-do without a time has no automation (§3.4, R20) | same | same (normally 0) |

Notes:
- The deleted chat's own pending actions are already `cancelled/data_deleted` by `purge_p2`; step 1 cancels the ended delegations' autonomous actions in their **other** chats with `delegation_ended`. Owner cards and agent proposals in those chats are not touched; a held reply (the autonomy-fallback `agent_proposal` row) that carries `delegation_id` stays valid, and its Draft reply button still shows its card (OD-11).
- `delegation_end` automations: M6-DELEG writes the delegation's `refs` onto its `delegation_end` automation, so `purge_p2` deletes it with the scope; otherwise the M6-LIFE handler is a no-op for a delegation that is not active.
- Ended delegations keep their row (the audit link from `actions.delegation_id`) until the 1-year sweep.

### 7.3 Report and receipt (`privacy/deletion.py`)

- `DeletionReport` gains `delegations_ended: int = 0`, `delegations_deleted: int = 0`, `automations_cancelled: int = 0`; `purge_p3`'s `memory_items`, `tasks`, `automations` and `notifications` are **added** to the existing counters. `counts()` gains the three keys; `total` includes them.
- `receipt_text`: the "Deleted:" list gains `{delegations_deleted} delegation(s)`; then one line each, only when non-zero: "Delegations ended: N." and "Reminders and follow-ups cancelled because their task was deleted: N." Ended is never reported as deleted (invariant 11). The audit row stays content-free (counts and ids).

## 8. Post-restore (`privacy/post_restore.py`, ARCHITECTURE §15.6; OD-12)

New **step 2b**, in the post-restore transaction right after step 2 (actions), per account (explicit account scope):
1. `SELECT id FROM app.delegations WHERE account_id = :account_id AND status = 'active' ORDER BY id FOR UPDATE`.
2. `end_delegations(..., reason='restored_from_backup', now=<the post-restore transaction's single `now`>)` (§7.1). The `now` is the same value that stamps the `restored_from_backup` audit row, so `delegations.ended_at` equals that audit time (OPS-RT3s identifies "ended by this restore" by that equality). Its action step finds nothing: step 2 already cancelled every pending action with `restored_from_backup`, and those keep that reason. It ends the chat links and deletes delegation-scope knowledge.
3. The notice payload gains `"delegations_ended": {"<account_id>": <count>}`; `PostRestoreReport` gains `delegations_ended: int = 0` (the sum).

Nothing resumes by itself: step 1 already paused autonomy for every account, an ended delegation never becomes active again, and the owner must confirm a new one. The `delegation_end` automation of an ended delegation may still fire after step 4 recomputes it; the M6-LIFE handler is a no-op for a non-active delegation.

Renderer (named touch point, F0-S): `inbox/backup_check.py::deliver_restore_notice` adds, after the skipped-schedules line and only when the count is > 0: "{count} delegation(s) were ended because of the restore. Set them up again if you still want them." (plain text, no user content; counts only.) OPS-RT3s checks the result on the restore test (`tools/acceptance/p3_restore_check.py`).

## 9. Named tests (F0-S, J1)

All synthetic data; `FakeClock` for time; real PostgreSQL 18. Names are binding (QA-P3's regression matrix cites them). F0-S runs only these files locally; CI runs the full suite (AGENTS §11).

**`tests/integration/test_migrations.py`** (extend):
- `EXPECTED_TABLES` gains `delegations`, `delegation_conversations`, `availability_minutes`.
- `test_there_is_exactly_one_alembic_head` asserts `["0015"]`.
- `test_every_tenant_table_has_an_account_id`: `exempt` gains `"availability_minutes",  # NFR-08 deployment record, belongs to no tenant`.
- `test_0015_p3_reverse`: head → down to `0014` → up to `0015`; the three tables, the new columns (`actions` 5, `memory_items` 2, `conversations` 4, `tasks` 3, `automations` 2) and the indexes `ux_actions_autonomous_inflight`, `ux_actions_delegation_base`, `ix_actions_scheduled`, `ux_delegation_conversations_live`, `ix_memory_items_account_id_review`, `ux_tasks_run_id_tool_call_id` exist; down again → all gone and the five rewritten `memory_items` CHECKs read their 0012 text (`pg_get_constraintdef`); back to head.
- `test_0015_moves_legacy_takeover_pauses_to_takeover_at`: at `0014`, three chats with `autonomy_paused_reason` = `'takeover'`, `'ingest_error'`, NULL; upgrade; the first has `takeover_at` set and the reason NULL, the second is unchanged, the third has `takeover_at` NULL.
- `test_0015_downgrade_restores_takeover_pauses_and_deletes_rows_the_old_checks_reject`: at head, a chat with `takeover_at`, one delegation, items of each new kind (delegation scope, owner shareable, inferred `pending_review`) and one plain `fact`; downgrade to `0014`; the chat reads `'takeover'`, only the `fact` remains, `delegations` is gone.

**`tests/integration/test_p3_constraints.py`** (new; one test per guard):
- `test_disclosure_is_allowed_only_on_autonomous_actions`
- `test_a_nudge_action_is_never_autonomous`
- `test_only_one_autonomous_action_is_in_flight_per_conversation` — each of `proposed`, `approved`, `scheduled`, `executing` blocks a second; `succeeded`, `cancelled`, `delivery_uncertain` free the slot; an `agent_proposal` in the same chat is unaffected
- `test_an_autonomous_reply_is_unique_per_delegation_and_base_message` — also after `succeeded`; a row cancelled with `disclosure_missing` frees the pair; an `agent_proposal` with the same pair is allowed
- `test_a_scheduled_action_carries_both_times_in_order`
- `test_shareable_memory_must_be_owner_stated`
- `test_inferred_memory_needs_a_category_and_a_contact_or_chat_scope`
- `test_no_sensitive_category_can_be_stored` — parametrized over `SENSITIVE_MEMORY_CATEGORIES` (each rejected) and `MEMORY_CATEGORIES` (each accepted)
- `test_memory_scope_names_exactly_its_own_reference` — including `delegation` with and without `delegation_id`, and with a `contact_id`
- `test_delegation_scope_holds_only_shareable_knowledge`
- `test_pending_review_is_only_for_inferred_items`
- `test_one_live_delegation_per_conversation` — a second live link fails; after `ended_at` is set a new link succeeds
- `test_delegation_end_fields_follow_its_status` — active with `ended_at` fails; ended without a reason fails; an unknown reason fails
- `test_a_standing_delegation_has_no_end_and_one_chat`
- `test_an_active_delegation_keeps_its_spec`
- `test_a_delegation_window_ends_after_it_starts` — `ck_delegations_window`
- `test_a_confirmed_delegation_tool_call_is_saved_once` — `uq_delegations_run_id_tool_call_id`
- `test_automation_paused_reason_accepts_only_owner_or_invalid`
- `test_an_owner_task_tool_call_is_saved_once`
- `test_deleting_a_delegation_deletes_its_knowledge_and_unlinks_its_actions` — FK CASCADE on `memory_items` and links, SET NULL on `actions`

**Mirrors:** `tests/integration/test_p3_schema_mirrors.py::test_p3_check_constraints_match_the_model_tuples` (the quoted values in `pg_get_constraintdef` equal the §4 tuples for `memory_items` scope/kind/source/status/category, `delegations` kind/status/ended_reason, `automations.paused_reason`; the `ux_actions_autonomous_inflight` predicate lists `AUTONOMOUS_INFLIGHT_STATES`). `tests/unit/test_memory_categories.py::test_sensitive_categories_are_disjoint_from_the_allowed_list`. `tests/unit/test_delegation_models.py::test_a_delegation_ended_before_it_starts_is_cancelled` (`ended_status`).

**Registry:** `tests/integration/test_managed_data_deletion.py::test_every_text_or_jsonb_column_is_classified` passes unchanged with §5.

**`tests/integration/test_retention_p3.py`** (new):
- `test_ended_delegations_are_deleted_a_year_after_they_end` — links cascade; an old action's `delegation_id` is gone with the action first
- `test_an_active_delegation_is_never_swept`
- `test_delegation_knowledge_is_swept_once_its_delegation_is_over` — ended → deleted; active past `ends_at` → deleted; active inside its window → kept
- `test_unreviewed_suggestions_are_deleted_after_14_days` — an approved inferred item is kept
- `test_availability_minutes_are_deleted_after_90_days`
- `test_the_sweep_reports_p3_counts`

**Deletion canary (R1 #7), `tests/integration/test_data_deletion.py`** (extend): `seed` adds canary text to `delegations.spec` (topic and disclosure override), a delegation-scope item, an owner-scope and a contact-scope shareable item, an inferred `pending_review` item, an owner reminder task with `conversation_id` NULL and `refs` naming the chat and contact, and an automation with managed provenance. The existing `test_deleting_a_conversation_leaves_no_text_from_it_in_any_content_column`, `test_deleting_a_contact_leaves_no_text_about_them_in_any_content_column`, `test_deleting_everything_leaves_no_content_of_the_account` and `test_another_accounts_ids_are_not_found_by_deletion` must pass with these classes seeded. New:
- `test_deleting_a_conversation_ends_and_scrubs_the_delegations_naming_it`
- `test_deleting_a_contact_ends_a_delegation_that_names_them_only_in_refs`
- `test_ending_a_delegation_by_deletion_cancels_its_autonomous_actions_in_its_other_chats` — reason `delegation_ended`; owner cards there and an `executing` action untouched
- `test_an_ended_delegation_naming_a_deleted_chat_loses_its_spec`
- `test_deleting_a_conversation_deletes_owner_tasks_found_only_through_refs`
- `test_a_follow_up_or_one_off_reminder_whose_task_was_deleted_is_cancelled` — a recurring reminder without a task stays active
- `test_deleting_everything_deletes_every_delegation`
- `test_the_deletion_receipt_reports_ended_delegations_and_cancelled_automations`

**Managed scope, `tests/integration/test_managed_data_deletion.py`** (extend):
- `test_managed_deletion_ends_active_delegations_and_keeps_their_spec`
- `test_managed_deletion_deletes_inferred_items_and_managed_owner_tasks_and_automations` — untainted owner tasks and automations stay

**Post-restore, `tests/integration/test_post_restore.py`** (extend):
- `test_post_restore_ends_active_delegations_with_restored_from_backup` — links ended, delegation knowledge deleted, a scheduled autonomous action cancelled, and a second post-restore changes nothing
- `test_the_restore_notice_counts_the_ended_delegations`

**Shared primitives, `tests/integration/test_p3_end_primitives.py`** (new):
- `test_end_delegations_ends_only_active_ones_and_is_idempotent` — returns the ended ids; a second call returns `[]`; one `delegation_ended` audit row each; `cancelled` before `starts_at`, `ended` after
- `test_end_delegations_ends_links_and_deletes_delegation_knowledge`
- `test_cancel_pending_for_delegations_cancels_only_autonomous_pending_actions` — `scheduled` and `approved` cancelled; `executing`, a held reply (`agent_proposal`) with the same `delegation_id` and another delegation's action untouched
- `test_cancel_pending_for_delegations_limits_to_the_given_chats` — the supersede case

**Factories:** `tests/integration/p3_factories.py` with `seed_delegation(session, *, account_id, conversation_ids, **fields) -> Delegation` (writes `refs` and the link rows), `seed_memory_item(session, *, account_id, **fields) -> MemoryItem`, `seed_owner_task(session, *, account_id, **fields) -> Task`, `seed_autonomous_action(session, *, account_id, conversation_id, delegation_id, **fields) -> Action` and `seed_availability_minute(session, *, minute, polls=1) -> AvailabilityMinute`. Every helper flushes and never commits, refuses another account's ids with `NotInAccountError` (the `p2_factories.py` pattern) and uses synthetic text. `seed_autonomous_action` accepts any state, including `scheduled` (it then sets `scheduled_for` and `late_after`). Reminder, follow-up and scheduled-summary automations use the existing `p2_factories.seed_automation(kind=..., spec=..., provenance=...)`. Test: `tests/integration/test_p3_factories.py::test_p3_factories_refuse_another_accounts_ids`.

**Hygiene:** `tests/integration/test_log_hygiene_f0s.py::test_p3_sweep_and_deletion_log_counts_only` (canary text in every new class; the sweep and a deletion log no canary, name or spec text). It lives under `tests/integration/` because it needs the database; the F0-E per-unit convention (P3-interfaces §1.1) allows either directory.

## 10. Consumed by

Unit ids as in README §3 (M7-TXT's renderers are in M7-REM and M7-FUP; OPS-VIEWS is Senior QA's). "F0-S" alone means the privacy code of §6–§8 and J2's read seams.

| Table / column | Writes | Reads |
|---|---|---|
| `delegations` | M6-DELEG (create on confirm, with its `delegation_end` automation for a time-bounded delegation; supersede, `owner_ended`, disclosure-wording edits to `spec`); M6-LIFE (`time_elapsed`, `connection_disabled`, `consent_revoked`); F0-S (`end_delegations`, purge, restore, sweep) | F0-S `active_covering`, `list_active`, `list_current` (R89), `get`, `shareable_for`; M6-PLAN; M6-SCHED (release re-check); M6-TXA (`FOR UPDATE` at claim); M6-TAKE (confirmed_at reference); M6-ESC (`active_delegation` fact); M6-CATCH (window); STAT-ACT; RD-DEL; SET-P3 (`list_current` for the list, `DelegationControls` to end); M7-EXP; EV-AUT (synthetic accounts); OPS-TV3 (non-content columns only; never `spec`, R84); OPS-VIEWS; OPS-RT3s; QA-S6; QA-X |
| `delegation_conversations` | M6-DELEG (insert on confirm; supersede); F0-S `end_delegations` | F0-S `active_covering`; M6-TAKE (one notice per takeover: delegation, chat and resume epoch, R81); M6-ESC; STAT-ACT; SET-P3 (J12 Delegate row); OPS-TV3 (covering at claim); OPS-VIEWS |
| `availability_minutes` | OPS-AV (`PollObserver` implementation); F0-S sweep | OPS-AV `deploy/sql/nfr08.sql`; OPS-VIEWS (trial report); LIVE-P3 |
| `actions.delegation_id` | M6-PLAN (`delegation_auto` and held replies); M6-TXA, M6-SCHED (conversion held replies, `disclosure_missing` re-proposal) | M6-TXA (counters under the lock); M6-DELEG (supersede), M6-LIFE and F0-S (cancel by delegation); STAT-ACT (`Authorization.kind = "delegation"`); M7-EXP; OPS-TV3 |
| `actions.scheduled_for`, `late_after`, `ix_actions_scheduled` | M6-SCHED (`schedule_autonomous`, called by M6-PLAN's job) | M6-SCHED (release, due reconciler); M6-TXA (late check); STAT-ACT ("scheduled" is never "sent"); OPS-TV3 (overdue scheduled never sent) |
| `actions.trigger_automation_id` | NUDGE-POL (`propose(trigger=…)`) | M7-NUDGE; OPS-VIEWS (followups view) |
| `actions.disclosure_included` | M6-TXA | OPS-TV3; QA-S6 (RG-14 scenario) |
| `actions.policy_decision` key `limits` (`per_conversation_hour`, `per_delegation`: the two integers the claim used; R84, P3-interfaces §2.4) | M6-TXA (claim of a `delegation_auto` action only) | OPS-TV3 (checks the caps from this key, so it never reads `delegations.spec`); QA-S6 |
| `ux_actions_autonomous_inflight`, `ux_actions_delegation_base` | — | M6-PLAN (a unique violation is the idempotent "already proposed" outcome); M6-TXA (re-proposal after `disclosure_missing`) |
| `memory_items` `scope = 'delegation'`, `delegation_id`, `kind = 'shareable'` | M6-DELEG (knowledge on the card; `remember(kind=shareable)`); F0-S (purge at end, sweep) | F0-S `shareable_for` → DELEG-DRAFT, M6-PLAN, EV-AUT; M7-EXP |
| `memory_items` `source = 'agent_inferred'`, `status = 'pending_review'`, `category` | M7-MEX (insert after SENS); M7-MRV (approve, edit, discard) | SENS (category tuples); M7-MRV (review queue index); M7-EXP; F0-S (14-day sweep, managed deletion) |
| `conversations.takeover_at`, `autonomy_resumed_at` | M6-TAKE | M6-TAKE (loader input for `autonomy_not_paused`, used by M6-PLAN, M6-SCHED, M6-TXA; `autonomy_resumed_at` as epoch seconds, 0 when NULL, in the `takeover_paused` dedupe key, R81); STAT-ACT (per-chat takeover pauses); OPS-TV3 (no claim after a takeover) |
| `conversations.pending_delegation_at` | M6-PLAN (set through the `DelegationRequester`, cleared by `replies.delegated`); M6-SCHED (set again at ingestion when it cancels an autonomous action for a new contact message or a source edit) | M6-PLAN (reconciler) |
| `conversations.pending_memory_at` | M7-MEX | M7-MEX (reconciler) |
| `tasks.refs` | M7-REM, M7-FUP, M7-MACT (`create_task` action) | F0-S (scoped deletion); RD-M7 (`list_tasks` filters) |
| `tasks.tool_call_id`, `ux_tasks_run_id_tool_call_id` | M7-REM, M7-FUP | M7-REM, M7-FUP (idempotent retry) |
| `tasks.provenance` | M7-REM, M7-FUP, M7-MACT | M7-NUDGE (owner guidance only from an untainted follow-up); RD-M7 (taint); F0-S (managed deletion) |
| `automations.paused_reason` | M7-AUTO (`owner`; cleared on resume) | SET-P3 (pause/resume rows); RD-M7 (`list_automations`); M7-AUTO |
| `automations.provenance` | M7-REM, M7-FUP, M7-AUTO, M7-SSUM | M7-NUDGE; M7-SSUM (consent at fire); RD-M7; F0-S (managed deletion) |
| `DeletionReport` / receipt fields, restore-notice count | F0-S | QA-X (delete-contact across delegations, tasks and automations; restore with an active delegation); OPS-RT3s; LIVE-P3 |

## 11. Refinements

The F0-S scope in README §3 (parts (a)–(d)) is followed. These points make it exact; each is small, fails closed and has a named test in §9. The TL ruled R-1 (R10), R-4 (R13: the list goes to the owner under OD-15), R-6 (R17), R-11 (R12) and R-12 (R11) on 2026-10-28; the others are signed off with this contract. An objection becomes an edit here before CP1, never an ICR later.

| # | Refinement | Why |
|---|---|---|
| R-1 | `ux_actions_delegation_base` excludes rows cancelled with `disclosure_missing` (`state_reason IS DISTINCT FROM 'disclosure_missing'`). **TL-ruled (R10):** M6-TXA re-proposes once; a second failure becomes a held reply (OD-11) | M6-TXA re-proposes after that cancel (specs/M6-TXA.md); a plain partial unique would block it, while every other outcome, including `succeeded`, keeps blocking a second reply to the same message |
| R-2 | `delegations.spec` is nullable, with `ck_delegations_active_spec` | The end-of-delegation purge "scrubs their spec" (§7), which needs somewhere to go; an active delegation can never lose its rules |
| R-3 | Added guards: `ck_actions_trigger_not_autonomous`, `ck_actions_schedule_pair`, `ck_actions_late_after_order`, `ck_actions_scheduled_has_time`; `ck_delegations_standing_has_no_end`, `_window`, `_end_fields`, `_standing_one_chat`; `ck_memory_items_delegation_kind`, `_inferred_scope`, `_pending_review_source` | Each turns a contract sentence (OD-18, ARCH §10.3, the end-of-delegation purge, "inferred notes are about a contact or chat", the review flow) into a database fact. No P2 row violates them |
| R-4 | The allowed `category` list (`work`, `project`, `schedule`, `logistics`, `communication_preference`, `relationship_role`, `other_safe`) and `SENSITIVE_MEMORY_CATEGORIES` live in `memory/models.py` (OD-15). **TL-ruled (R13):** the allowed list goes to the owner as part of OD-15 and is settled before CP1 | The shape is fixed ("closed non-sensitive list + `other_safe`", OD-15) but not the values; SENS and M7-MEX need one tuple each |
| R-5 | `automations.paused_reason` CHECK is value-only, with no backfill; NULL on a paused row = digest off (digest) or invalid rule (other kinds) | M05's `_pause_invalid` (schedules and monitors), `_sync_digest` and `monitors.stop` write `status` without knowing the column; a status-coupled CHECK would break them |
| R-6 | F0-S touch points beyond the core file list of README §3: `actions/lifecycle.py` (the `delegation_ids` filter on `_change_pending` + `cancel_pending_for_delegations`); `automation/delegations_repo.py::end_delegations`; `inbox/backup_check.py` (one restore-notice line); `db/models.py` (`AvailabilityMinute`); `migrations/env.py` (model import); `tests/integration/test_p3_end_primitives.py`. **TL-ruled (R17)**, with the rest of the F0 scope growth listed in P3-interfaces §1.2 | One implementation of "end a delegation" and of "cancel its autonomous actions", shared by deletion, restore, M6-DELEG and M6-LIFE. `delegations_repo.py` is already an F0-S file; `lifecycle.py` is Josh-owned (M6-SCHED adds its bulk helpers later); `backup_check.py` belongs to no P3 unit; the file-ownership table in P3-interfaces §15 lists these touch points |
| R-7 | Scoped deletion also deletes queued notifications whose `refs.delegation_ids` name a scrubbed delegation | A queued catch-up must not render after its chat was deleted |
| R-8 | `status = 'cancelled'` means "ended before `starts_at`"; every ender uses the same `CASE` / `ended_status` | The value needs one meaning for every writer |
| R-9 | `tasks.provenance = '{}'` reads as managed and tainted for `created_by = 'agent'`; `automations.provenance = '{}'` reads as owner-authored | Existing rows get a safe reading without a data migration |
| R-10 | Receipt reports "Delegations ended" and "Reminders and follow-ups cancelled" separately from "Deleted" | Invariant 11: an ended delegation is not a deleted one |
| R-11 | The orphan step also cancels a one-off reminder whose task was deleted (not only follow-ups). **TL-ruled (R12):** one-off reminder text lives in `tasks.title`, always with a task; the M7-REM spec states it | A one-off reminder is a task plus an automation (ARCH §10.3); without its task it would fire with nothing to show |
| R-12 | Supersede cancels the older delegation's pending autonomous actions in the shared chats (`cancel_pending_for_delegations(..., conversation_ids=...)`, §2.2) in the same transaction, even when the older delegation stays active for its other chats. **TL-ruled (R11)** | Otherwise its in-flight action holds the one-in-flight index and the newer delegation's first reply in that chat is silently dropped; the release re-check would cancel it anyway, but only after the new reply was lost |

## Appendix A. ARCHITECTURE replacement text

The F0-S PR applies this text to `docs/ARCHITECTURE.md` as it stands on `main` after CP5 (the int/m05 version). It resolves the document conflicts 17–20 in `decisions.md` §C. "Replace" quotes the old text exactly; "Append" adds at the end of the named cell or paragraph. Rows not named here do not change.

### A.1 §10.1 Conventions

Replace the bullet that starts `- **Every tenant-owned table has` (ends `repositories require an account context.`) with:

```markdown
- **Every tenant-owned table has `account_id` (FK, NOT NULL, indexed, with an account-leading index)**; repositories require an account context. The only tables without it: `accounts` (the tenant root), `system_state` and `availability_minutes` (deployment state; the latter from P3), `foreign_connections` (non-owners) and `agent_run_steps` (scoped through `agent_runs`).
```

Replace the bullet that starts `- Content-bearing columns (the complete set` with:

```markdown
- Content-bearing columns (the complete set that retention, deletion and any future field-level encryption must cover): `messages.text` (a media caption is stored here too, so one column holds every message text — S02.2), `messages.media` (structural facts only, but tied to the message and purged with it), `messages.search_key`/`search_tsv`, `actions.text` (for an autonomous reply, including the application-inserted disclosure), `memory_items.content` (owner notes, shareable knowledge and inferred suggestions), `conversation_summaries.summary`, `tasks.title`/`details` (including owner reminders and to-dos), `conversation_triage.reasons` (F0), `control_turns.text`/`quoted_text` (S03.1), `tool_confirmations.args` (S03.1), `delegations.spec` (delegation rules: topic, behaviours, knowledge ids, disclosure override; revision 0015), `automations.spec` (owner-authored conditions, reminder text, follow-up expectations and summary scopes, often naming contacts), `contacts.display_name`/`username`, `inbound_updates.payload`. A new content-bearing column must be added to this list, to §10.4 and to the deletion cascade. Notifications, `delegation_conversations`, `availability_minutes` and every `refs` and `provenance` column contain no content.
```

### A.2 §10.2 (same PR, keeps the diagram true)

Append inside the `erDiagram` block, after `delegations ||--o{ actions : authorizes`:

```text
    delegations ||--o{ delegation_conversations : covers
    conversations ||--o{ delegation_conversations : "covered by"
    delegations ||--o{ memory_items : "scopes knowledge"
    automations ||--o{ actions : "nudge (J7)"
```

### A.3 §10.3 Tables

Replace the `conversations` row with:

```markdown
| `conversations` | `account_id`, `kind` (`business`), `business_chat_id`, UNIQUE(`account_id`,`business_chat_id`), `contact_id`, `mode` (ignore/observe/assist), `availability` (active/possibly_paused), `last_contact_msg_at`, `last_owner_msg_at`, `last_assistant_msg_at`, `coverage_started_at`, `autonomy_paused_reason` (takeover/ingest_error; `takeover` is legacy and never written after revision 0015), `pending_ai_since` NULL (content no AI run has seen yet; consumed once `ai_managed` consent is active, §14.3), `summary_gap_before` NULL, `created_at`, `updated_at`; later: `pending_draft_at` (M04), `pending_triage_at` and `pending_monitor_at` (M05), `takeover_at`, `autonomy_resumed_at`, `pending_delegation_at` and `pending_memory_at` (P3, revision 0015) | Reply window derived from `last_contact_msg_at`. Delegate is not a mode value (§8.2). As built through S02.4 (`src/assistant/conversations/models.py`). **P3:** `takeover_at` is the newest owner manual message; a takeover blocks autonomy only when `takeover_at > max(delegation confirmed_at, autonomy_resumed_at)` ("Resume here", ADR-19). Revision 0015 moved every legacy `autonomy_paused_reason = 'takeover'` to `takeover_at` = migration time. There is no `autonomy_paused_until` or `settings` column. |
```

Replace the `memory_items` row with:

```markdown
| `memory_items` | `account_id`, `scope` (owner/contact/conversation/delegation), `contact_id` NULL FK, `conversation_id` NULL FK, `delegation_id` NULL FK (ON DELETE CASCADE; CHECK: exactly the FK matching `scope` is set, none for `owner`), `kind` (instruction/preference/fact/style/shareable), `content`, `source` (owner_stated/agent_inferred), `status` (active/archived/pending_review), `category` NULL (a closed non-sensitive list plus `other_safe`; required for `agent_inferred`), `provenance` jsonb, `applies_to_replies` (bool, owner-set, default false), `run_id`, `tool_call_id`, `created_at`, `updated_at`, `archived_at` | Inferred items require review before use. Contact-facing loading rules in §7.3. **As built (S03.3, revision 0012):** status active/archived with `archived_at` (forgotten, deleted after 24 h), `provenance` = `{"tainted": bool, "managed": bool, "refs": {"conversation_ids": [...], "contact_ids": [...]}}` (flags and ids only; `refs` for items from other people's text), `run_id` (FK `agent_runs`, SET NULL) + `tool_call_id` UNIQUE for idempotent tool calls. **P3 (revision 0015):** the `delegation` scope and `shareable` kind (M06): shareable knowledge is always `owner_stated` (CHECK); `scope = delegation` holds only shareable knowledge for one delegation and is purged when it ends; owner-, contact- and conversation-scoped shareable items persist until deleted. `agent_inferred` and `pending_review` (M07): inferred items are contact- or conversation-scoped and carry a required `category` and `provenance.source_message_ids`; the sensitive categories (OD-15) are not in the CHECK, so they cannot be stored; a `pending_review` row never reaches a prompt (loaders read `active` only) and is deleted after 14 days. The column earlier drafts called `sensitivity` is `category`. Exact constraints: [P3 schema §3.2](engineering/p3/contracts/P3-schema.md). |
```

`tasks` row, three edits:
1. Key columns — Append after `` `closed_at` ``: `` , `refs` jsonb (ids of the chats and contacts the task names), `tool_call_id` (UNIQUE with `run_id` where set), `provenance` jsonb (flags and ids) — P3, revision 0015 ``.
2. Notes — Replace `a closed task is never reopened.` with `` triage never reopens a closed task (the owner can, with `update_task`, M07). ``
3. Notes — Append: `` **P3:** owner tasks (`created_by = 'owner'`: reminders, follow-ups, to-dos) are written with `refs` and `provenance`, so per-chat and per-contact deletion finds a task that names a contact without a `conversation_id`, and managed-data deletion finds owner text written by a tainted or managed run. ``

Replace the `delegations` row with these two rows:

```markdown
| `delegations` | `account_id`, `kind` (time_bounded/standing), `spec` jsonb NULL (validated `DelegationRules` with `schema_version`: chats, topic, behaviours, knowledge ids, escalation, limits, disclosure override; content; NULL once scrubbed by a deletion), `refs` jsonb (ids), `status` (active/ended/cancelled), `starts_at`, `ends_at` (NULL = standing), `confirmed_at`, `ended_at`, `ended_reason` (time_elapsed/owner_ended/superseded/data_deleted/connection_disabled/consent_revoked/restored_from_backup), `share_widened` bool, `run_id`, `tool_call_id`, UNIQUE(`run_id`,`tool_call_id`) | A row exists only after the owner confirmed the card; the unconfirmed spec is held in `tool_confirmations`, so there is no `draft` status. `cancelled` = ended before `starts_at`. A standing delegation has `ends_at` NULL and exactly one chat (CHECK). `ends_at` is enforced by policy at every decision, not by the `delegation_end` automation. **As built (P3, revision 0015):** [P3 schema §2.1](engineering/p3/contracts/P3-schema.md). |
| `delegation_conversations` | `account_id`, `delegation_id` FK CASCADE, `conversation_id` FK CASCADE, `created_at`, `ended_at` NULL; UNIQUE(`delegation_id`,`conversation_id`); partial UNIQUE(`conversation_id`) WHERE `ended_at` IS NULL | One row per covered chat: at most one live delegation per chat (a newer one supersedes on confirm, OD-7), and the indexed, fail-closed lookup behind `delegation_active`. Rows end in the same transaction as their delegation. P3 (revision 0015). |
```

`automations` row, two edits:
1. Key columns — Append after `` `confirmed_at` ``: `` , `paused_reason` NULL (owner/invalid; P3), `provenance` jsonb (flags and ids; P3) ``.
2. Notes — Append: `` **P3 (revision 0015):** `paused_reason` is read only while `status = paused`; NULL there means the owner switched the digest off (digest) or the stored rule no longer validates (other kinds). `provenance` marks owner text written by a tainted or managed run; managed-data deletion deletes those rows. A follow-up or one-off reminder automation whose task was deleted is cancelled by every deletion scope. ``

Replace the `actions` row with:

```markdown
| `actions` | `account_id`, `conversation_id`, `kind` (send_message/edit_message/mark_read/delete_own; `mark_read` is R3 and not built in Personal v1), `origin` (owner_dictated/agent_proposal/delegation_auto), `delegation_id` NULL (FK SET NULL), `trigger_automation_id` NULL (FK SET NULL), `run_id`, `tool_call_id`, `trigger_turn_id` NULL, `text`, `version` (int), `shown_through_message_id` (bigint: the highest `messages.id` of the conversation that the shown version accounted for), `base_message_id`, `state`, `policy_decision` jsonb, `approval_token` UNIQUE, `card_message_id`, `scheduled_for`, `late_after`, `disclosure_included` bool, `expires_at`, `attempt_id`, `attempt_started_at`, `telegram_message_id`, `identity_pending` bool, `error_class`, `created_at`, `decided_at`, `executed_at`, UNIQUE(`run_id`,`tool_call_id`), partial UNIQUE(`conversation_id`) WHERE `state` IN ('awaiting_approval','revising') | Lifecycle per §9.4; approval bound to `version` (§9.5). **P3 (revision 0015):** `delegation_id` on every `delegation_auto` action and on the autonomy-fallback held reply (OD-11); `scheduled_for`/`late_after` set when an autonomous action enters `scheduled`; `disclosure_included` set at claim and allowed only on `delegation_auto` (CHECK; PRD §11.4); `trigger_automation_id` links a J7 nudge card to its follow-up and is never set on `delegation_auto`. Partial UNIQUE(`conversation_id`) over the in-flight `delegation_auto` states (one autonomous action in flight per chat) and partial UNIQUE(`delegation_id`,`base_message_id`) for `delegation_auto` (one autonomous reply per delegation and message; a row cancelled for a missing disclosure is excluded, so it can be re-proposed). |
```

Insert after the `system_state` row:

```markdown
| `availability_minutes` | `minute` timestamptz PK (UTC, truncated to the minute), `polls` int | Non-tenant deployment record for NFR-08 (P3, revision 0015): one row per minute with a completed, committed poll, written after the `last_poll_ok` commit in its own transaction; 90-day retention; no content. |
```

Append after the paragraph that starts `**As built (F0, revisions 0013–0014):**`:

```markdown
**As built (P3 F0-S, revision 0015):** new tables `delegations`, `delegation_conversations` and `availability_minutes`; new columns on `actions`, `memory_items`, `conversations`, `tasks` and `automations`; the legacy-takeover data step. Exact columns, constraints and indexes are in [the P3 schema contract](engineering/p3/contracts/P3-schema.md).
```

### A.4 §10.4 Retention

Replace the `memory_items.content` row with:

```markdown
| `memory_items.content` | Until deleted; a forgotten (archived) item 24 h — the Undo window (S03.3, M03 D69). Unreviewed suggestions and delegation-scope knowledge: see their own rows (P3) | Hourly sweep deletes archived items; FK cascade from contact/conversation/delegation; managed-data deletion deletes contact- and conversation-scoped items (every inferred item is one, by CHECK) and every managed-origin item in any scope (the owner's own items stay, M03 D71); a bot-chat purge deletes that chat's and contact's items and every item whose `provenance.refs` names them; `memory/items.py::delete_for_conversation` / `delete_for_contact` are the helpers for S03.5's per-conversation/contact deletion; the P3 cascade also deletes the delegation-scope items of the delegations it ends or scrubs |
```

Replace the `delegations.spec` row with these five rows:

```markdown
| `delegations.spec` and rows | Active: until ended. Ended or cancelled: 1 year after `ended_at`, then the row is deleted (its chat links and any leftover delegation-scope knowledge cascade; `actions.delegation_id` becomes NULL) | Hourly sweep (after the `actions` rows); the P3 cascade clears `spec` on every delegation naming a deleted chat or contact and deletes every delegation on "everything"; post-restore ends active delegations (§15.6) |
| Delegation-scope knowledge (`memory_items` with `scope = delegation`) | Until its delegation ends. Saved shareable knowledge (`kind = shareable`, owner/contact/conversation scope) persists until deleted (PRD §13) | Deleted when the delegation ends; the hourly sweep deletes any left whose delegation is not active or is past `ends_at` |
| `memory_items` suggestions (`status = pending_review`, M07) | 14 days from creation if not reviewed (OD-16) | Hourly sweep; approve makes the item `active` (the `memory_items.content` row then applies), discard deletes it |
| `availability_minutes` | 90 days (content-free system table, NFR-08) | Hourly sweep |
| `tasks.refs`/`provenance`, `automations.provenance`/`paused_reason`, `delegation_conversations` | With their row (ids, flags and codes; no content) | The row's own rule |
```

Append after the paragraph that starts `**P2 cascade (F0):**`:

```markdown
**P3 cascade (F0-S, `privacy/p3_deletion.py::purge_p3`, right after the P2 cascade in every scope):** conversation/contact deletion and the bot-chat purge end every active delegation whose `refs` or chat links name the chat or its contact (`ended_reason = data_deleted`; its pending autonomous actions in its other chats are cancelled with `delegation_ended`), clear `spec` on every delegation naming them, delete those delegations' delegation-scope knowledge and queued notifications, and delete owner tasks whose `refs` name the chat or contact. Managed-data deletion ends every active delegation (`data_deleted`; the owner-authored spec stays) and deletes every `agent_inferred` item and every owner task or automation whose `provenance` is managed. Everything deletion deletes every delegation. In every scope, a follow-up or one-off reminder automation whose task no longer exists is cancelled. Receipts report delegations ended and automations cancelled as counts; an ended delegation is never reported as deleted.
```

### A.5 §15.6 Restore from backup

Append to the paragraph that starts `**As built (F0):** post-restore now implements the action step`:

```markdown
**As built (P3 F0-S), step 2b:** in the same transaction, every `active` delegation ends with `ended_reason = restored_from_backup` (`cancelled` if it had not started yet), its chat links end and its delegation-scope knowledge is deleted; nothing re-activates it, and autonomy is paused anyway (step 1). The notice gains "N delegation(s) were ended because of the restore. Set them up again if you still want them." (counts only; OD-12).
```

In the bullet that starts `- The operator then runs`, replace `` and every action in `executing` to `delivery_uncertain`; `` with `` and every action in `executing` to `delivery_uncertain`; (2b) ends every active delegation (`restored_from_backup`); ``.

In the bullet that starts `- It then sends the owner a notice`, replace `listing the cancelled actions, the gap,` with `listing the cancelled actions, the ended delegations, the gap,`.
