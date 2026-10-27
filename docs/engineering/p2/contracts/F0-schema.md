# F0 contract — schema, content columns, retention, deletion cascade

*Normative for every P2 unit. Implemented by **F0** (Neryn). After CP1 this contract is frozen: changes go through an interface change request (ORG §5) and the schema steward assigns the next revision number. Conventions are the repository's own (`db/base.py`, migrations 0005/0011/0012): schema `app`, `bigint generated always as identity` primary keys, `timestamptz` UTC, enums as `text` + named CHECK constraints, FKs named by `_fk(...)` with explicit `ondelete`, hand-written and reviewed migrations with a data-handling note in the docstring (AGENTS §12).*

## 1. Revision plan

| Revision | File | Revises | Content |
|---|---|---|---|
| `0013` | `migrations/versions/0013_actions.py` | `0012` | M04: `actions`; `messages.action_id`; `conversations.pending_draft_at` |
| `0014` | `migrations/versions/0014_inbox_intelligence.py` | `0013` | M05: `contacts.priority`; `conversations.pending_triage_at`, `pending_monitor_at`; `tasks`; `conversation_triage`; `automations`; `automation_occurrences`; `digests`; `notifications` |

Rules:
- S03.7 adds no revision; verify `alembic heads` shows `0012` on `main` when F0 starts. If M03 lands another revision first, renumber to the next free numbers and record it in `decisions.md` §C.
- Exactly one head at all times. F0 adds `test_there_is_exactly_one_alembic_head` to `tests/integration/test_migrations.py`.
- Each revision gets an up/down reverse test in `test_migrations.py` in the style of `test_0012_memory_items_reverse` (`test_0013_actions_reverse`, `test_0014_inbox_intelligence_reverse`).
- ORM models mirror the DDL exactly (`Mapped[]`, `mapped_column`); `test_upgrade_creates_every_table_in_the_app_schema` and `test_every_tenant_table_has_an_account_id` must pass unchanged.

## 2. Revision 0013 — actions (M04)

### 2.1 `actions` (ORM: `src/assistant/actions/models.py::Action`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint identity | no | | PK |
| `account_id` | bigint | no | | FK `accounts.id` ON DELETE CASCADE |
| `conversation_id` | bigint | no | | FK `conversations.id` ON DELETE CASCADE |
| `kind` | text | no | | CHECK `kind IN ('send_message','edit_message','delete_own')` (`mark_read` is R3, not built) |
| `origin` | text | no | | CHECK `origin IN ('owner_dictated','agent_proposal','delegation_auto')` (`delegation_auto` is never created in P2; it exists for policy tests and M06) |
| `state` | text | no | `'proposed'` | CHECK `state IN ('proposed','awaiting_approval','revising','approved','scheduled','executing','succeeded','failed','delivery_uncertain','rejected','outdated','expired','cancelled','blocked')` |
| `state_reason` | text | yes | | Reason code (`actions/states.py::StateReason`); validated in code, no CHECK (the list grows). Never free text |
| `version` | integer | no | `1` | CHECK `version >= 1`; incremented by every edit/revision |
| `text` | text | yes | | **CONTENT.** The current version's owner-voice text. NULL after retention purge or deletion |
| `approved_text_hash` | text | yes | | SHA-256 hex of the exact text approved (P2-T07) |
| `sent_text_hash` | text | yes | | SHA-256 hex of the exact string passed to the gateway |
| `base_message_id` | bigint | yes | | FK `messages.id` ON DELETE SET NULL. The newest contact message the draft answers (NULL for owner-dictated text with no contact message) |
| `shown_through_message_id` | bigint | yes | | Highest `messages.id` of this conversation the current version accounted for (no FK; a watermark) |
| `target_message_id` | bigint | yes | | FK `messages.id` ON DELETE SET NULL. Only for `edit_message`/`delete_own`; must reference an `assistant_sent` row of the same conversation (checked by policy and gateway, not by a constraint) |
| `run_id` | bigint | yes | | FK `agent_runs.id` ON DELETE SET NULL. The owner run whose tool proposed it |
| `tool_call_id` | text | yes | | With `run_id`, the idempotency key |
| `draft_run_id` | bigint | yes | | FK `agent_runs.id` ON DELETE SET NULL. The `draft_reply` run that wrote the current version |
| `trigger_turn_id` | bigint | yes | | FK `control_turns.id` ON DELETE SET NULL. The owner turn that led to it (retried-turn reuse, §8.3 step 4) |
| `revision_turn_id` | bigint | yes | | FK `control_turns.id` ON DELETE SET NULL. The owner's edit message being applied while `revising` (R8) |
| `draft_flags` | jsonb | yes | | Content-free flags of the version's `DraftResult`: `needs_reply`, `confidence`, `sensitive_topic`, `makes_commitment`, `should_escalate`, `uses_only_permitted_information`, `language` (R8). NULL for owner-dictated text |
| `policy_decision` | jsonb | yes | | Latest decision, content-free: `{"phase": "proposal"\|"execution", "outcome": "...", "checks": [{"name": "...", "passed": true, "reason": "<code>"}]}` |
| `approval_token` | text | yes | | UNIQUE. Random base62 (`accounts/callbacks.new_token`), rotated on every version; NULL outside `awaiting_approval` |
| `card_message_id` | bigint | yes | | Control-chat message id of the approval card |
| `card_state` | text | yes | | The state the card message last showed; the card sync (S04.3) edits cards where `card_state` ≠ `state` |
| `expires_at` | timestamptz | yes | | `min(created_at + APPROVAL_MAX_AGE, window close − margin)` at proposal; a new version never extends it and only shortens it when the window now closes earlier (R7) |
| `attempt_id` | text | yes | | uuid4 hex of the current send attempt |
| `attempt_started_at` | timestamptz | yes | | |
| `retry_at` | timestamptz | yes | | Set after a `rate_limited` result (Telegram `retry_after`) |
| `telegram_message_id` | bigint | yes | | From a successful send; NULL with `identity_pending` for `message_id == 0` |
| `identity_pending` | boolean | no | `false` | |
| `error_class` | text | yes | | `core.delivery.ErrorClass` value of the last attempt |
| `error_code` | text | yes | | Telegram description code (e.g. `PEER_ID_INVALID`), never free text |
| `created_at` | timestamptz | no | `now()` | |
| `updated_at` | timestamptz | no | `now()` | |
| `decided_at` | timestamptz | yes | | Owner approved/rejected |
| `executed_at` | timestamptz | yes | | Terminal send outcome recorded |

Constraints and indexes:
- `uq_actions_run_id_tool_call_id` UNIQUE (`run_id`, `tool_call_id`) — NULLs distinct.
- `uq_actions_approval_token` UNIQUE (`approval_token`).
- `ux_actions_live_card` UNIQUE (`conversation_id`) WHERE `state IN ('awaiting_approval','revising')` — at most one live approval card per conversation (ARCHITECTURE §8.3).
- `ix_actions_account_id_created_at` (`account_id`, `created_at`) — activity log.
- `ix_actions_conversation_id_state` (`conversation_id`, `state`) — outdating and supersede.
- `ix_actions_inflight` (`state`, `attempt_started_at`) WHERE `state IN ('approved','executing','delivery_uncertain')` — reconciler, `mark_uncertain`.
- `ix_actions_expiring` (`expires_at`) WHERE `state IN ('awaiting_approval','revising','approved')` — `expire_actions`.
- `ix_actions_trigger_turn_id_conversation_id` (`trigger_turn_id`, `conversation_id`) WHERE `trigger_turn_id IS NOT NULL`.
- `ix_actions_card_unsynced` (`account_id`) WHERE `card_message_id IS NOT NULL AND card_state IS DISTINCT FROM state` — card sync.

### 2.2 Changes to existing tables

| Table | Column | Type | Notes |
|---|---|---|---|
| `messages` | `action_id` | bigint NULL | FK `actions.id` ON DELETE SET NULL; index `ix_messages_action_id` WHERE `action_id IS NOT NULL`. Set on the `assistant_sent` row created from a send result (ARCHITECTURE §9.4 Tx B) |
| `conversations` | `pending_draft_at` | timestamptz NULL | Assist drafting flag (S04.2); reconciled by `reconcile_pending` |

Downgrade: drop `messages.action_id`, `conversations.pending_draft_at`, then `actions`. Data-handling note in the docstring: the downgrade deletes every action and its text (owner-voice content).

## 3. Revision 0014 — inbox intelligence (M05)

### 3.1 Changes to existing tables

| Table | Column | Type | Notes |
|---|---|---|---|
| `contacts` | `priority` | text NOT NULL DEFAULT `'normal'` | CHECK `priority IN ('vip','normal','low')` (FR-TRI-04) |
| `conversations` | `pending_triage_at` | timestamptz NULL | Triage flag (§6.5 step 5; S05.1) |
| `conversations` | `pending_monitor_at` | timestamptz NULL | Semantic-monitor evaluation flag (S05.4) |

### 3.2 `tasks` (ORM: `src/assistant/inbox/models.py::Task`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint identity | no | | |
| `account_id` | bigint | no | | FK accounts CASCADE |
| `conversation_id` | bigint | yes | | FK conversations CASCADE |
| `kind` | text | no | | CHECK `kind IN ('owner_commitment','contact_commitment','open_question','follow_up','reminder','decision')` |
| `title` | text | no | | **CONTENT** |
| `details` | text | yes | | **CONTENT** |
| `due_at` | timestamptz | yes | | |
| `snoozed_until` | timestamptz | yes | | |
| `status` | text | no | `'open'` | CHECK `status IN ('open','done','cancelled','expired')` |
| `closed_reason` | text | yes | | Code: `resolved_by_triage`, `owner_done`, `owner_cancelled`, `expired`, `data_deleted` |
| `source_message_ids` | jsonb | no | `'[]'` | `messages.id` list (ids only) |
| `first_source_message_id` | bigint | yes | | Dedupe key part |
| `created_by` | text | no | | CHECK `created_by IN ('owner','agent')` (only `agent` in P2, P2-T19) |
| `run_id` | bigint | yes | | FK agent_runs SET NULL (the triage run) |
| `created_at`, `updated_at` | timestamptz | no | `now()` | |
| `closed_at` | timestamptz | yes | | |

Indexes: `ux_tasks_agent_dedupe` UNIQUE (`conversation_id`, `kind`, `first_source_message_id`) WHERE `created_by = 'agent'`; `ix_tasks_account_id_status_due_at` (`account_id`, `status`, `due_at`); `ix_tasks_conversation_id_status` (`conversation_id`, `status`).

### 3.3 `conversation_triage` (ORM: `inbox/models.py::ConversationTriage`) — P2-T12

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint identity | no | | |
| `account_id` | bigint | no | | FK accounts CASCADE |
| `conversation_id` | bigint | no | | FK conversations CASCADE; UNIQUE (`uq_conversation_triage_conversation_id`) |
| `covers_through_message_id` | bigint | no | | Highest `messages.id` the verdict saw |
| `needs_reply` | boolean | no | | |
| `contains_question` | boolean | no | | |
| `requests_decision` | boolean | no | | |
| `urgency` | text | no | | CHECK `urgency IN ('low','normal','high','urgent')` |
| `deadline_at` | timestamptz | yes | | |
| `escalation_category` | text | no | | CHECK `escalation_category IN ('none','urgent_decision','needs_owner','fyi','suspicious')` |
| `reasons` | jsonb | no | `'[]'` | **CONTENT.** Short model-written reasons (list of strings) |
| `language` | text | yes | | |
| `script` | text | yes | | CHECK `script IN ('latin','cyrillic','mixed','other')` when not NULL |
| `prompt_version`, `model` | text | no | | |
| `run_id` | bigint | yes | | FK agent_runs SET NULL |
| `created_at`, `updated_at` | timestamptz | no | `now()` | |

Indexes: an account-leading index, as ARCHITECTURE §10.1 requires for tenant tables (P2-T34; exact columns as built in 0014 and listed in the F0 PR).

### 3.4 `automations` (ORM: `src/assistant/automation/models.py::Automation`)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint identity | no | | |
| `account_id` | bigint | no | | FK accounts CASCADE |
| `kind` | text | no | | CHECK `kind IN ('reminder','monitor','follow_up','scheduled_summary','delegation_end','digest')` |
| `task_id` | bigint | yes | | FK tasks SET NULL |
| `spec` | jsonb | no | | **CONTENT.** A validated domain rule model with `schema_version` (`automation/rules.py`) |
| `refs` | jsonb | no | `'{}'` | `{"conversation_ids": [...], "contact_ids": [...]}` — what the spec names, for deletion lookup (ids only) |
| `status` | text | no | `'active'` | CHECK `status IN ('active','paused','completed','cancelled','expired')` |
| `rrule` | text | yes | | RFC 5545 rule (structural) |
| `run_at` | timestamptz | yes | | One-off time |
| `timezone` | text | no | | IANA name |
| `tz_policy` | text | no | | CHECK `tz_policy IN ('follow_owner','fixed')` |
| `next_run_at` | timestamptz | yes | | NULL for event-only automations (monitors) |
| `last_run_at` | timestamptz | yes | | |
| `expires_at` | timestamptz | yes | | |
| `confirmed_at` | timestamptz | yes | | |
| `run_id` | bigint | yes | | FK agent_runs SET NULL |
| `tool_call_id` | text | yes | | UNIQUE with `run_id` (`uq_automations_run_id_tool_call_id`) |
| `created_at`, `updated_at` | timestamptz | no | `now()` | |
| `ended_at` | timestamptz | yes | | Set on completed/cancelled/expired |

Indexes: `ix_automations_due` (`next_run_at`) WHERE `status = 'active' AND next_run_at IS NOT NULL`; `ix_automations_account_id_kind_status` (`account_id`, `kind`, `status`).

### 3.5 `automation_occurrences` (ORM: `automation/models.py::AutomationOccurrence`)

`id`; `account_id` (FK accounts CASCADE); `automation_id` (FK automations CASCADE); `occurrence_at` timestamptz NOT NULL; `status` text NOT NULL CHECK `IN ('claimed','fired','skipped')`; `note_code` text NULL (`late`, `skipped_obsolete`, `skipped_restore`); `fired_at` timestamptz NULL; `created_at`. UNIQUE (`automation_id`, `occurrence_at`); an account-leading index (ARCHITECTURE §10.1, P2-T34).

### 3.6 `digests` (ORM: `inbox/models.py::Digest`)

`id`; `account_id` (FK accounts CASCADE); `period_start`, `period_end` timestamptz NOT NULL; `status` text NOT NULL CHECK `IN ('building','sent','failed','uncertain')`; `message_id` bigint NULL; `sent_at` NULL; `created_at`, `updated_at`. UNIQUE (`account_id`, `period_start`, `period_end`).

### 3.7 `notifications` (ORM: `inbox/models.py::Notification`) — P2-T12: no content

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint identity | no | | |
| `account_id` | bigint | no | | FK accounts CASCADE |
| `kind` | text | no | | `inbox/notification_types.py::NotificationKind` value |
| `alert_class` | text | no | `'standard'` | CHECK `alert_class IN ('system','standard')`. `system` is never suppressed, batched, capped or delayed (ARCHITECTURE §17) |
| `priority` | text | no | | CHECK `priority IN ('low','normal','high','urgent')` |
| `status` | text | no | `'queued'` | CHECK `status IN ('queued','sent','suppressed','batched','failed','uncertain')` |
| `dedupe_key` | text | no | | ids/enums only; UNIQUE (`account_id`, `dedupe_key`) |
| `refs` | jsonb | no | `'{}'` | ids only: `conversation_ids`, `contact_ids`, `task_ids`, `message_ids`, `automation_ids`, `action_ids` |
| `conversation_id` | bigint | yes | | FK conversations SET NULL (cap/feedback queries) |
| `contact_id` | bigint | yes | | FK contacts SET NULL |
| `reason_code` | text | yes | | Why suppressed/batched/failed (code) |
| `bypass_quiet_hours` | boolean | no | `false` | |
| `deliver_after` | timestamptz | yes | | Quiet-hours deferral |
| `digest_id` | bigint | yes | | FK digests SET NULL |
| `message_id` | bigint | yes | | Control-chat message id once sent |
| `feedback` | text | yes | | CHECK `feedback IN ('useful','not_useful')` |
| `created_at`, `updated_at` | timestamptz | no | `now()` | |
| `sent_at` | timestamptz | yes | | |

Index: `ix_notifications_account_id_status_deliver_after` (`account_id`, `status`, `deliver_after`).

Downgrade: drop in reverse dependency order (notifications, digests, automation_occurrences, automations, conversation_triage, tasks; then the columns). Data-handling note: drops task titles/details, triage reasons and automation specs (content).

## 4. Content-column registry (`privacy/content_columns.py`)

Add to `CONTENT_COLUMNS`: `("actions","text")`, `("tasks","title")`, `("tasks","details")`, `("conversation_triage","reasons")`, `("automations","spec")`.

Add to `NON_CONTENT_COLUMNS` every other new `text`/`jsonb` column: `actions`: `kind`, `origin`, `state`, `state_reason`, `policy_decision`, `draft_flags`, `approval_token`, `approved_text_hash`, `sent_text_hash`, `tool_call_id`, `card_state`, `attempt_id`, `error_class`, `error_code`; `contacts.priority`; `tasks`: `kind`, `status`, `closed_reason`, `source_message_ids`, `created_by`; `conversation_triage`: `urgency`, `escalation_category`, `language`, `script`, `prompt_version`, `model`; `automations`: `kind`, `refs`, `status`, `rrule`, `timezone`, `tz_policy`, `tool_call_id`; `automation_occurrences`: `status`, `note_code`; `digests.status`; `notifications`: `kind`, `alert_class`, `priority`, `status`, `dedupe_key`, `refs`, `reason_code`, `feedback`. The existing registry test then passes against the live schema.

ARCHITECTURE §10.1's content list already names `actions.text`, `tasks.title/details` and `automations.spec`; F0 adds `conversation_triage.reasons` there and notes that `notifications` holds no content (P2-T12).

## 5. Retention (ARCHITECTURE §10.4 rows; `privacy/retention.py`)

| Data | Rule | Sweep implementation |
|---|---|---|
| `actions.text` | Same period as the account's message content (default 90 d, owner 14–90), from `created_at` | Set `text = NULL` for actions older than the period, in the per-account-group pattern of `sweep_message_content`; never touches `executing` rows |
| `actions` rows | 1 year (audit) | Delete rows with `created_at` older than `ACCOUNTING_RETENTION` (365 d) whose state is terminal or `delivery_uncertain` (R6) |
| `tasks` | Open: until deleted. Closed (`done`/`cancelled`/`expired`): 1 year after `closed_at` | Delete closed rows past 1 year |
| `conversation_triage` | Same period as message content, from `updated_at` | Delete rows not updated within the period |
| `automations.spec` | Until deleted; ended (`completed`/`cancelled`/`expired`) + 1 year | Delete ended rows past 1 year (occurrences cascade) |
| `automation_occurrences`, `digests` | 1 year (metadata) | Delete rows older than 1 year |
| `notifications` | 30 days | Delete rows older than 30 days |

`SweepReport` gains counters `action_texts`, `actions`, `tasks`, `triage_verdicts`, `automations`, `occurrences`, `digests`, `notifications`; `JobHandlers.retention_sweep` logs them (counts only).

## 6. Deletion cascade (`privacy/deletion.py`)

Each scope runs inside the existing deletion transaction, after the existing steps, under the locks it already holds (§9.4 order). Every step is a set-based statement scoped by `account_id`. Rows are cancelled before their text is cleared. A **live card** whose action is cancelled here is updated by the card sync (S04.3) within a minute; F0 sets `state`/`state_reason` and leaves `card_state` unchanged so the sync finds it.

| Scope | Actions | Tasks | Triage | Notifications | Automations |
|---|---|---|---|---|---|
| `delete_conversation_data` (and `purge_conversation`, the bot-chat purge) | In those conversations: states `proposed`/`awaiting_approval`/`revising`/`approved`/`scheduled` → `cancelled`, `state_reason = 'data_deleted'`, `approval_token = NULL`; then `text = NULL` for every action of those conversations (any state) | Delete rows of those conversations | Delete rows | Delete rows whose `conversation_id` is one of them or whose `refs.conversation_ids`/`refs.contact_ids` name them or their contact | Delete rows whose `refs` name one of them or their contact |
| `delete_contact_data` | As above, for each of the contact's conversations | As above | As above | As above, plus `contact_id`/`refs.contact_ids` = the contact | As above, plus `refs.contact_ids` = the contact |
| `delete_everything` | All of the account's actions: cancel the pending states, clear every `text` | Delete all | Delete all | Delete all | Delete every automation except kind `digest` (owner-written monitor text is content, R11) |
| `managed` (`delete_managed_data`) | Cancel the pending states, clear every `text` (drafts derive from managed content) | Delete rows `created_by = 'agent'` | Delete all | Delete all | Keep (owner-authored) |

The deletion receipt (`receipt_text`, `DeletionReport`) gains counts `actions`, `tasks`, `notifications`, `automations` (counts only). The audit row stays content-free.

**Tests (F0, in the existing files):** extend the content-scan helper used by `tests/integration/test_data_deletion.py` so it seeds actions, tasks, triage verdicts and automations with canary text and asserts, for each scope, that no canary from the deleted scope remains in any `CONTENT_COLUMNS` column and that other conversations and accounts keep theirs; a pending action in a deleted conversation ends `cancelled` with `state_reason = 'data_deleted'`; an `executing` action keeps its state; `test_managed_data_deletion.py` covers the managed column of the table; retention tests per row of §5 with `FakeClock`.

## 7. Post-restore (`privacy/post_restore.py`, ARCHITECTURE §15.6)

F0 implements step 2 for actions (the module docstring already reserves it): in the post-restore transaction, actions in `proposed`/`awaiting_approval`/`revising`/`approved`/`scheduled` → `cancelled` (`state_reason = 'restored_from_backup'`, token cleared) and `executing` → `delivery_uncertain` (`state_reason = 'restored_from_backup'`); the owner notice lists how many were cancelled and how many are uncertain, with their action ids and local creation times (ids and times only, no text — ARCHITECTURE §15.6 "listing the cancelled actions"). Step 4 (automations' `next_run_at`) belongs to S05.3b. Test: `test_post_restore.py::test_post_restore_cancels_pending_actions_and_marks_executing_uncertain`.
