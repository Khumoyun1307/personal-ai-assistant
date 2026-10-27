"""The content-bearing columns, as a checked registry (ARCHITECTURE §10.1).

Every `text`, `jsonb` and `tsvector` column in the `app` schema is listed either here as
content or in `NON_CONTENT` as something structural (an enum, an id, a
digest). A test compares both sets with the live schema, so a new text column
cannot appear without someone deciding which it is — and the deletion
cascade and the retention sweep can never silently miss one.
"""

from __future__ import annotations

#: (table, column) pairs that may hold message text, captions, names or raw
#: payloads. Each has a §10.4 retention row and is covered by deletion.
CONTENT_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("contacts", "display_name"),
        ("contacts", "username"),
        ("messages", "text"),
        ("messages", "media"),
        ("messages", "search_key"),
        # Generated from `search_key` (revision 0007): purged with it.
        ("messages", "search_tsv"),
        ("control_turns", "text"),
        # Text the owner quoted or replied to from another chat (S03.1).
        ("control_turns", "quoted_text"),
        # A held model call's arguments, until the owner decides (S03.1).
        ("tool_confirmations", "args"),
        ("inbound_updates", "payload"),
        # Model-written summaries of one managed chat (S03.2).
        ("conversation_summaries", "summary"),
        # Owner instructions, preferences, facts and contact notes (S03.3).
        ("memory_items", "content"),
        ("actions", "text"),
        ("tasks", "title"),
        ("tasks", "details"),
        ("conversation_triage", "reasons"),
        ("automations", "spec"),
    }
)

#: Structural text/jsonb columns: enums, ids, digests, settings, counters.
#: Deliberately explicit — "everything else" would defeat the check.
NON_CONTENT_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("actions", "kind"),
        ("actions", "origin"),
        ("actions", "state"),
        ("actions", "state_reason"),
        ("actions", "policy_decision"),
        ("actions", "draft_flags"),
        ("actions", "approval_token"),
        ("actions", "approved_text_hash"),
        ("actions", "sent_text_hash"),
        ("actions", "tool_call_id"),
        ("actions", "card_state"),
        ("actions", "attempt_id"),
        ("actions", "error_class"),
        ("actions", "error_code"),
        ("contacts", "priority"),
        ("tasks", "kind"),
        ("tasks", "status"),
        ("tasks", "closed_reason"),
        ("tasks", "source_message_ids"),
        ("tasks", "created_by"),
        ("conversation_triage", "urgency"),
        ("conversation_triage", "escalation_category"),
        ("conversation_triage", "language"),
        ("conversation_triage", "script"),
        ("conversation_triage", "prompt_version"),
        ("conversation_triage", "model"),
        ("automations", "kind"),
        ("automations", "refs"),
        ("automations", "status"),
        ("automations", "rrule"),
        ("automations", "timezone"),
        ("automations", "tz_policy"),
        ("automations", "tool_call_id"),
        ("automation_occurrences", "status"),
        ("automation_occurrences", "note_code"),
        ("digests", "status"),
        ("notifications", "kind"),
        ("notifications", "alert_class"),
        ("notifications", "priority"),
        ("notifications", "status"),
        ("notifications", "dedupe_key"),
        ("notifications", "refs"),
        ("notifications", "reason_code"),
        ("notifications", "feedback"),
        ("accounts", "status"),
        ("accounts", "timezone"),
        ("accounts", "locale_pref"),
        ("accounts", "settings"),
        ("accounts", "policy_notice_version"),
        ("consents", "kind"),
        ("consents", "policy_version"),
        ("consents", "processors"),
        ("business_connections", "connection_id"),
        ("business_connections", "rights"),
        ("business_connections", "capability_tier"),
        ("business_connections", "status"),
        ("business_connection_events", "connection_id"),
        ("business_connection_events", "payload_digest"),
        ("business_connection_events", "rights"),
        ("business_connection_events", "change"),
        ("business_connection_events", "source"),
        ("foreign_connections", "connection_id"),
        ("control_turns", "role"),
        ("control_turns", "refs"),
        ("pending_inputs", "kind"),
        ("pending_inputs", "token"),
        ("audit_log", "actor"),
        ("audit_log", "event"),
        ("audit_log", "subject_type"),
        ("audit_log", "details"),
        ("agent_runs", "run_type"),
        ("agent_runs", "trigger_ref"),
        ("agent_runs", "model"),
        ("agent_runs", "effort"),
        ("agent_runs", "prompt_version"),
        ("agent_runs", "status"),
        ("agent_runs", "error_class"),
        ("agent_run_steps", "tool_name"),
        ("agent_run_steps", "args_digest"),
        ("agent_run_steps", "status"),
        ("tool_confirmations", "tool_call_id"),
        ("tool_confirmations", "tool_name"),
        ("tool_confirmations", "status"),
        ("llm_calls", "purpose"),
        ("llm_calls", "model"),
        ("llm_calls", "request_id"),
        ("inbound_updates", "kind"),
        ("inbound_updates", "status"),
        ("inbound_updates", "error_code"),
        ("system_state", "key"),
        ("system_state", "value"),
        ("conversations", "kind"),
        ("conversations", "mode"),
        ("conversations", "availability"),
        ("conversations", "autonomy_paused_reason"),
        ("messages", "direction"),
        ("messages", "observed_via"),
        ("messages", "content_type"),
        ("messages", "script"),
        ("messages", "lang_guess"),
        ("messages", "content_hash"),
        ("coverage_gaps", "kind"),
        ("coverage_gaps", "certainty"),
        ("conversation_summaries", "kind"),
        ("conversation_summaries", "prompt_version"),
        ("conversation_summaries", "model"),
        # A hash over (message id, updated_at) pairs: no content goes in.
        ("conversation_summaries", "source_digest"),
        ("memory_items", "scope"),
        ("memory_items", "kind"),
        ("memory_items", "source"),
        ("memory_items", "status"),
        # Flags and ids only: `{"tainted", "managed", "refs": {conversation
        # and contact ids}}` — no content.
        ("memory_items", "provenance"),
        ("memory_items", "tool_call_id"),
        # Alembic's own bookkeeping table.
        ("alembic_version", "version_num"),
    }
)
