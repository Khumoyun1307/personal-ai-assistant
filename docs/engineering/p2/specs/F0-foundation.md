# F0 — Foundation: phase schema, privacy cascade, contracts, hot-file refactor

| | |
|---|---|
| **Owner** | Neryn · **Peer review** Viro (code), Eng4 (privacy cascade, M05 schema) · **QA** Antony · **Sign-off** TL · **Merge** owner (to `main`) |
| **Size** | M · **Critical path** yes — every other unit builds on it |
| **Branch** | `p2/f0-foundation` from `main` after "M03 OK" → PR to `main` |
| **Normative inputs** | [contracts/F0-schema.md](../contracts/F0-schema.md), [contracts/F0-interfaces.md](../contracts/F0-interfaces.md), [decisions.md](../decisions.md) P2-T01…T05, T12, T16 |

## 1. Goal

Land, in one PR, everything that both milestones build on, so that M04 and M05 can proceed in parallel without editing the same files: the phase schema, the privacy obligations of every new content column, the shared types and ports, and registration points in the files both streams would otherwise edit. **No product behaviour changes:** every existing test stays green unchanged, and with all new flags off the running system behaves exactly as before.

## 2. Scope

**In:**
1. Migrations `0013_actions` and `0014_inbox_intelligence` and their ORM models, exactly as [F0-schema §2–§3](../contracts/F0-schema.md).
2. Content-column registry, retention rules and sweep counters, deletion cascade for every scope, post-restore step 2 for actions ([F0-schema §4–§7](../contracts/F0-schema.md)).
3. `core/reply_window.py` (and `conversations/window.py` delegating), `core/delivery.py` (with re-exports), `core/jobs.py` P2 constants and `DraftReason` ([F0-interfaces §3, §4, §6.1](../contracts/F0-interfaces.md)).
4. `actions/` package skeleton: `models.py`, `states.py` (complete), `ports.py` (protocols), `config.py` (`ActionsConfig` with every field of F0-interfaces §10, including `assist_draft_trigger`), `drafts.py` (`assist_trigger(flags)` implemented; `request_draft` left to S04.2), `lifecycle.py` bulk helpers (implemented), `proposals.py` (dataclasses and a `propose` stub raising `NotImplementedError`). Test `tests/unit/test_assist_trigger.py::test_the_assist_trigger_switch_table` (all eight flag combinations).
5. `policy/context.py` (types only; `decide` not implemented).
6. `conversations/listeners.py`; `BusinessPipeline` calling listeners; `record_message`/`record_deletion` outcomes extended ([F0-interfaces §5](../contracts/F0-interfaces.md)).
7. Registration points: `jobs/tasks_actions.py`, `jobs/tasks_inbox.py`, `jobs/tasks_automation.py` (empty register functions), optional handler groups on `JobHandlers`, the reconciler registry, `telegram/control/routes.py` and its use in `ControlDispatcher`, `_wire_m04`/`_wire_m05` blocks in `bootstrap.py` ([F0-interfaces §6.2, §6.3, §7](../contracts/F0-interfaces.md)).
8. `inbox/notification_types.py`; `inbox/notifications.py` with the `enqueue` signature (stub raising `NotImplementedError`); `AlertKind` additions; `automation/rules.py` (`RuleBase`) and `automation/repository.py` (implemented).
9. Settings in `config.py` and `.env.example`; M05 preference keys, validators and defaults in `accounts/preferences.py`, accepted by `preferences.update` and returned by `preferences.current` with the existing audit/Undo behaviour ([F0-interfaces §10](../contracts/F0-interfaces.md)).
10. Test support: `tests/fakes/approvals.py` (`RecordingApprovalPresenter`); `FakeTelegramSender` records `business_connection_id` (already a parameter — assert it) and gains `delete_business_messages`; `tests/integration/p2_factories.py` with account-scoped seed helpers for actions, tasks, triage verdicts, automations, notifications (used by every P2 unit and by QA).
11. Tool plumbing ([F0-interfaces §9](../contracts/F0-interfaces.md)): optional `ToolServices` fields (`drafter`, `approval_cards`, `actions_config`, `notifications_enabled`) and `ToolContext.turn_id`, set by `OwnerRequestRuntime` (a one-line change in `agent/runtime.py`); tests `test_tool_context_carries_the_answered_turn_id` and `test_a_tool_whose_service_is_missing_answers_not_available`.
12. **Ingestion barrier reset (R1):** `bootstrap.running_process` generates `process_run_id` (uuid4) and commits `system_state['ingestion_barrier'] = {"process_run_id": <id>, "caught_up_at": null}` **before any role starts**; the id is passed to the poller and to `ActionsConfig`/the policy loader. (Setting `caught_up_at` is S04.4's poller change; reading it is S04.1's.) Test `test_startup_resets_the_ingestion_barrier_before_any_role_runs`.
13. **Pure states (R5):** `actions/states.py` and `actions/__init__.py` import only the standard library; add the import-linter contract `actions-states-is-pure` (source `assistant.actions.states`; forbidden `sqlalchemy`, `assistant.db`).
14. **Panel facts for M05 (R14):** `conversations/visibility.py::ConversationPanel` gains `contact_id` and `contact_priority`, filled by `conversation_panel(...)`; test `test_the_conversation_panel_carries_the_contact_priority`.
15. Optional hygiene from the handoff: `privacy/requests.py` raises a dedicated `SubjectNotFound` instead of catching `LookupError` broadly (M03 follow-up 5).

**Out:** policy rules and `decide` (S04.1); the poller setting `caught_up_at` (S04.4); `propose` logic (S04.1); executor, gateway business methods (S04.4); any job body; any prompt; any tool; any UI; any owner-facing text beyond `StateReason` explanations.

## 3. Implementation notes

- **Order that keeps you unblocked:** core moves (window, delivery) → migrations + models → registry/retention/cascade/post-restore + their tests → listeners → registries and bootstrap blocks → settings/preferences → fakes and factories → docs. Each step is independently testable; parallelise internally as you like.
- **`core/delivery.py` move:** keep `telegram.errors.ErrorClass`/`classify_exception` and `telegram.gateway.SendResult` importable (re-export) so no existing import changes; grep for every import and leave them working.
- **`conversations/window.py`** keeps `REPLY_WINDOW`, `SAFETY_MARGIN`, `window_anchor`, `window_closes_at`, `window_open` as its public API; their bodies call `core.reply_window`. `test_reply_window.py` must pass unchanged.
- **Listeners:** `BusinessPipeline.__init__` gains `listeners: Sequence[IngestionListener] = ()`; build `MessageChange` from the event, the extended `IngestOutcome` and the conversation's mode; call listeners only on the live path (never for `ignored`/`rejected` rows, never from `on_ignored`, never from `on_dead_letter`). Deletions: call `on_deletion` after `record_deletion` with the ids whose content was removed (for the rows' `direction` you may read the rows before the purge inside `record_deletion`).
- **Bulk lifecycle helpers** are conditional set-based `UPDATE … WHERE account_id = :a AND state IN (…) RETURNING id`, then one `audit_log` row per affected action (`action_outdated` / `action_cancelled`, `details = {"reason": …, "from": …}`), `approval_token = NULL` for anything leaving `awaiting_approval`. They never touch `executing`, `delivery_uncertain` or terminal rows. `include_approved=True` also outdates `approved` (used by takeover in S04.2/S04.4).
- **Deletion cascade:** add the new steps to the existing scope functions inside their transactions, after the existing steps and under the locks already held. Use `cancel_pending` + `clear_texts` for actions. The receipt gains counts. Keep every statement account-scoped.
- **Reconciler registry:** existing `reconcile_pending` logic unchanged; registered reconcilers run after it in the same transaction; their `DeferRequest`s are deferred after the commit; a defer that returns `None` (queueing lock held) is fine.
- **Control routes:** keep every existing branch of `dispatch` and `dispatch_callback` exactly as is; consult `ControlRoutes` only when no existing branch handled the message/press/command. Unknown kinds keep today's behaviour (`handle_card_press` answers "no longer active").
- **Settings:** add to `Settings` with bounds as listed; add each to `.env.example` with a comment; extend `test_config.py`.
- **Migrations:** hand-written, docstring with the data-handling note; use the `_fk` helper pattern of 0005; partial indexes with `postgresql_where`. Check `alembic heads` is `0012` on `main` before numbering.

## 4. Tests (write each red first, then green)

`tests/integration/test_migrations.py`: `test_0013_actions_reverse`, `test_0014_inbox_intelligence_reverse`, `test_there_is_exactly_one_alembic_head`, `test_one_live_card_per_conversation_is_enforced_by_the_database`, `test_an_agent_task_is_unique_per_conversation_kind_and_first_source`, `test_notification_dedupe_key_is_unique_per_account`.

`tests/unit/test_action_states.py`: `test_transitions_match_the_architecture_diagram` (the full expected pair set written out), `test_settled_states_have_no_way_back_to_sending`, `test_delivery_uncertain_moves_only_to_succeeded_or_failed`, `test_every_state_reason_has_an_owner_explanation`, `test_state_sets_cover_the_lifecycle` (`LIVE_CARD ⊂ PENDING`; `PENDING ∩ TERMINAL = ∅`; every state in `PENDING ∪ TERMINAL ∪ {executing, delivery_uncertain}`, R6), `test_executing_may_expire_and_proposed_may_be_cancelled` (R4).

`tests/unit/test_reply_window.py` (extend): `test_core_window_and_conversation_window_agree`.

`tests/integration/test_action_lifecycle_bulk.py`: `test_outdate_pending_moves_only_pending_states_and_audits_each`, `test_outdate_pending_includes_approved_only_when_asked`, `test_cancel_pending_never_touches_executing_uncertain_or_terminal`, `test_clear_texts_never_crosses_accounts` (RG-21 style), `test_bulk_helpers_clear_the_approval_token`.

`tests/integration/test_ingestion_listeners.py`: `test_listeners_run_in_order_inside_the_ingesting_transaction`, `test_a_listener_error_fails_the_row_like_any_handler_error`, `test_a_deletion_reaches_listeners_with_its_assistant_sent_ids`, `test_ignored_and_rejected_updates_reach_no_listener`, `test_an_ignore_mode_message_reaches_listeners_as_metadata_only`.

`tests/integration/test_control_routes.py`: `test_a_registered_callback_kind_is_dispatched_with_the_press`, `test_an_unknown_kind_still_answers_no_longer_active`, `test_a_registered_pending_input_consumes_the_next_owner_message`, `test_a_forwarded_message_never_answers_a_registered_pending_input`, `test_a_command_cancels_a_registered_pending_input`, `test_a_registered_command_runs`, `test_existing_cards_and_commands_behave_as_before` (spot-check consent, tool confirmation, undo, panels, `/pause`), `test_business_updates_never_reach_registered_routes` (RG-03 extended to the registries: a `business_message` whose text looks like a registered command, or that arrives while a registered pending input is open, reaches no route handler, even from the owner).

`tests/integration/test_jobs.py` (extend): `test_registered_reconcilers_run_in_the_reconcile_transaction_and_defer_after_commit`, `test_handler_groups_left_out_register_no_tasks`.

Privacy: `tests/integration/test_data_deletion.py` — for each of conversation, contact, everything: `test_<scope>_deletion_clears_p2_content_columns` (canaries in `actions.text`, `tasks.title/details`, `conversation_triage.reasons`, `automations.spec`), `test_a_pending_action_in_a_deleted_conversation_is_cancelled_with_reason`, `test_an_executing_action_keeps_its_state_when_its_chat_is_deleted`, `test_other_conversations_and_accounts_keep_their_p2_rows`; `tests/integration/test_managed_data_deletion.py` — `test_managed_deletion_clears_action_texts_agent_tasks_and_triage_and_keeps_automations`; `tests/integration/test_retention_p2.py` — one test per row of F0-schema §5; `tests/integration/test_post_restore.py` — `test_post_restore_cancels_pending_actions_and_marks_executing_uncertain`; the existing content-columns registry test passes with the new lists.

`tests/unit/test_config.py` (extend) for every new setting's default and bounds; `tests/unit/test_preferences_m05.py`: `test_quiet_hours_parse_and_default`, `test_digest_time_off_and_default`, `test_alert_cap_bounds`, `test_an_invalid_stored_value_reads_as_the_default`.

Layering: `lint-imports` passes; add a unit test `test_policy_imports_nothing_that_reaches_the_database` that imports `assistant.policy.context` in a fresh interpreter and asserts `sqlalchemy` is not in `sys.modules`.

## 5. Acceptance criteria

1. `alembic upgrade head` / `downgrade 0012` / `upgrade head` round-trips on a scratch database; one head.
2. Every new `text`/`jsonb` column is classified; the registry test passes against the live schema.
3. For each deletion scope, no canary from the deleted scope remains in any content column, and nothing outside the scope changed.
4. `post-restore` with seeded actions leaves no action in a pending state and none `executing`.
5. A business message reaches registered listeners in order inside the ingesting transaction; with no listeners registered, ingestion is unchanged (existing ingestion tests green).
6. Existing control-chat behaviour is unchanged; a registered kind/command/pending input is dispatched.
7. All P2 types, constants, ports, settings and flags named in F0-interfaces exist with the given names and signatures (`mypy --strict` clean).
8. CI green on the full suite. No content in logs (existing log-hygiene suite green).

## 6. Docs (in this PR)

ARCHITECTURE: §5.1 module map (new modules), §6.5 (listener hook after step 7, "as built F0"), §10.1 (add `conversation_triage.reasons`; `notifications` holds no content), §10.3 (rows for `actions` additions `card_state`, hashes; `tasks`, `conversation_triage`, `automations` `refs`, `notifications` as built), §10.4 (P2 rows), §12.2 (task names, marked "registered by the owning unit"), §15.6 (actions step as built). `.env.example`. The PR description lists anything that differs from the contracts, for the TL to log.

## 7. Risks

- **Behaviour drift in the refactors** (dispatcher, pipeline, reconcile): keep existing branches untouched and let the existing tests prove it.
- **Deletion-cascade gaps:** the canary scan over every content column is the guard; Eng4 reviews it independently.
- **Time:** F0 is on everyone's critical path. If it grows beyond ~2 days, split the PR into F0a (schema, models, privacy, core moves) and F0b (registries, settings, fakes) and tell the TL; F0a unblocks the most.
