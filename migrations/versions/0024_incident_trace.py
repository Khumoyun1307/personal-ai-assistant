"""Structural incident trace (I25, finding R4-10).

Hand-written. One new tenant table of ids and fixed codes; no content column,
no change to existing tables. Rows go with their run (ON DELETE CASCADE from
`agent_runs`, whose one-year retention sweep therefore covers them) and with
their account; references to deleted requests, actions, turns, conversations
or child runs become NULL. Downgrade drops the trace only.

The code lists are a frozen copy of `assistant.agent.models` at this revision;
`tests/unit/test_incident_trace.py` keeps the two equal.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# A new table that older executables never read or write.
COMPATIBILITY = "expand"

MAX_EVENTS = 64
RUN_STATUSES = ("running", "succeeded", "failed", "refused", "budget_exceeded")
ACTION_STATES = (
    "proposed",
    "awaiting_approval",
    "revising",
    "approved",
    "scheduled",
    "executing",
    "succeeded",
    "failed",
    "delivery_uncertain",
    "rejected",
    "outdated",
    "expired",
    "cancelled",
    "blocked",
)
STAGES = (
    "request",
    "instruction",
    "dropped",
    "taint",
    "recipient",
    "proposal",
    "observed",
    "child_run",
    "outcome",
    "run",
    "trace",
)
CODES: dict[str, tuple[str, ...]] = {
    "request": (
        "opened_by_turn",
        "explicit_reply",
        "pending_question",
        "focused_request",
        "ambiguous",
        "none",
        "unavailable",
        "other",
    ),
    "instruction": (
        "instruction",
        "literal_body",
        "constraint",
        "quote",
        "suggestion",
        "owner_turn",
        "model",
        "none",
        "other",
    ),
    "dropped": (
        "dropped_quoted",
        "dropped_tainted",
        "guidance_ignored",
        "forwarded_turn",
        "managed_access_off",
        "other",
    ),
    "taint": ("started_tainted", "tainted_by_tool", "other"),
    "recipient": (
        "unique_strong",
        "owner_choice",
        "explicit_reply",
        "unbound",
        "proposal_target",
        "other",
    ),
    "proposal": (
        "staged",
        "reused_exact",
        "revised",
        "stale",
        "idempotency_conflict",
        "invalid_arguments",
        "needs_input",
        "not_available",
        "blocked",
        "awaiting_approval",
        "not_drafted",
        "declined_by_provider",
        "conflict_existing_draft",
        "no_longer_pending",
        "revising",
        "not_revised",
        "no_live_card",
        "already_being_revised",
        "no_draft",
        "ok",
        "error",
        "other",
    ),
    "observed": (
        "not_attempted",
        "in_flight",
        "shown",
        "not_delivered",
        "uncertain",
        "unknown",
        "other",
    ),
    "child_run": (*RUN_STATUSES, "other"),
    "outcome": (*ACTION_STATES, "missing", "other"),
    "run": (*RUN_STATUSES, "other"),
    "trace": ("truncated", "other"),
}
REASONS = (
    "answered_not_answerable",
    "invalid_edit",
    "invalid_effect_key",
    "invalid_source",
    "length_not_met",
    "operation_mismatch",
    "recipient_mismatch",
    "source_unavailable",
    "target_mismatch",
    "content",
    "recipient",
    "target",
    "constraints",
    "artifact_unavailable",
    "fence_unavailable",
    "not_available",
    "not_found",
    "request_unavailable",
    "use_main_revision_schema",
    "request_changed",
    "version_changed",
    "existing_draft_conflict",
    "args_changed",
    "no_longer_pending",
    "chat_changed",
    "provider_refused",
    "stale",
    "managed_access_off",
    "superseded",
    "new_contact_message",
    "takeover",
    "owner_edit",
    "source_edited",
    "source_deleted",
    "rights_changed",
    "connection_disabled",
    "connection_invalid",
    "ignore_mode",
    "data_deleted",
    "consent_revoked",
    "ingest_error",
    "restored_from_backup",
    "window_closed",
    "max_age",
    "owner_rejected",
    "policy_denied",
    "sending_disabled",
    "too_long",
    "autonomy_paused",
    "sent",
    "not_delivered",
    "ambiguous",
    "stale_executing",
    "late_result",
    "other",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(value) for value in values)})"


def _code_check() -> str:
    return " OR ".join(
        f"(stage = '{stage}' AND {_in('code', codes)})" for stage, codes in CODES.items()
    )


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE app.incident_trace_events (
            id bigint GENERATED ALWAYS AS IDENTITY
                CONSTRAINT pk_incident_trace_events PRIMARY KEY,
            account_id bigint NOT NULL
                CONSTRAINT fk_incident_trace_events_account_id_accounts
                REFERENCES app.accounts(id) ON DELETE CASCADE,
            run_id bigint NOT NULL
                CONSTRAINT fk_incident_trace_events_run_id_agent_runs
                REFERENCES app.agent_runs(id) ON DELETE CASCADE,
            seq integer NOT NULL,
            stage text NOT NULL,
            code text NOT NULL,
            reason text,
            turn_id bigint CONSTRAINT fk_incident_trace_events_turn_id_control_turns
                REFERENCES app.control_turns(id) ON DELETE SET NULL,
            request_id bigint CONSTRAINT fk_incident_trace_events_request_id_owner_requests
                REFERENCES app.owner_requests(id) ON DELETE SET NULL,
            request_version integer,
            child_run_id bigint CONSTRAINT fk_incident_trace_events_child_run_id_agent_runs
                REFERENCES app.agent_runs(id) ON DELETE SET NULL,
            action_id bigint CONSTRAINT fk_incident_trace_events_action_id_actions
                REFERENCES app.actions(id) ON DELETE SET NULL,
            action_version integer,
            conversation_id bigint
                CONSTRAINT fk_incident_trace_events_conversation_id_conversations
                REFERENCES app.conversations(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_incident_trace_events_run_id_seq UNIQUE (run_id, seq),
            CONSTRAINT ck_incident_trace_events_seq CHECK (seq >= 1 AND seq <= {MAX_EVENTS}),
            CONSTRAINT ck_incident_trace_events_stage CHECK ({_in("stage", STAGES)}),
            CONSTRAINT ck_incident_trace_events_code CHECK ({_code_check()}),
            CONSTRAINT ck_incident_trace_events_reason CHECK ({_in("reason", REASONS)})
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_incident_trace_events_account_id_run_id "
        "ON app.incident_trace_events (account_id, run_id)"
    )


def downgrade() -> None:
    op.drop_table("incident_trace_events", schema="app")
