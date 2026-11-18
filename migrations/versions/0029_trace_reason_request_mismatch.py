"""Trace reason code `request_mismatch` (H8, found by the I25/H8 merge).

Hand-written. Replaces the reason CHECK of `app.incident_trace_events` with a
list that also accepts `request_mismatch`, the fixed reason of the
re-draft/revision tool when its request does not match the run's request. No
column, table or data changes. Downgrade maps any `request_mismatch` row to
`other` and restores the revision 0027 list.

Like 0027, the lists are frozen copies of `assistant.agent.models` at this
revision; `tests/integration/test_incident_trace.py` compares the model with
the migrated database, so a later code change needs a new revision.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

# A CHECK that only widens the accepted values: older executables never write
# the new code, and they are not affected by accepting it.
COMPATIBILITY = "expand"

CONSTRAINT = "ck_incident_trace_events_reason"
PREVIOUS_REASONS = (
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
    "revision_not_applied",
    "owner_intent_superseded",
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
REASONS = (*PREVIOUS_REASONS[:7], "request_mismatch", *PREVIOUS_REASONS[7:])


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(value) for value in values)})"


def _replace_check(values: tuple[str, ...]) -> None:
    op.execute(f"ALTER TABLE app.incident_trace_events DROP CONSTRAINT {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE app.incident_trace_events "
        f"ADD CONSTRAINT {CONSTRAINT} CHECK ({_in('reason', values)})"
    )


def upgrade() -> None:
    _replace_check(REASONS)


def downgrade() -> None:
    op.execute(
        "UPDATE app.incident_trace_events SET reason = 'other' WHERE reason = 'request_mismatch'"
    )
    _replace_check(PREVIOUS_REASONS)
