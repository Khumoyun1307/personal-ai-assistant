"""Agent run accounting (ARCHITECTURE §10.3).

These tables exist to make model usage auditable and billable: what ran, which
prompt version, how many tokens, what it cost. None of them store content —
`agent_run_steps` keeps a digest of tool arguments, never the arguments. The
one exception is `tool_confirmations.args`, held only until the owner decides
(see the class).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk

RUN_TYPES = (
    "owner_request",
    "triage",
    "draft_reply",
    "summarize",
    "extract_memory",
    "evaluate_monitor",
    "interpret_instruction",
)
RUN_STATUSES = ("running", "succeeded", "failed", "refused", "budget_exceeded")


class AgentRun(Base):
    """One agent invocation."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "run_type IN " + str(RUN_TYPES).replace('"', "'"), name="ck_agent_runs_run_type"
        ),
        CheckConstraint(
            "status IN " + str(RUN_STATUSES).replace('"', "'"), name="ck_agent_runs_status"
        ),
        Index("ix_agent_runs_account_id_started_at", "account_id", "started_at"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    #: What triggered the run: an id or enum, never content.
    trigger_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Set when the run saw forwarded/quoted third-party text or a tool result
    #: carrying contact-authored text. A tainted run cannot change state
    #: without an owner confirmation card (§6 invariant 3b).
    tainted: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    #: Survives deletion of source fences: text already in a model context
    #: cannot acquire fresh proof after privacy erasure. Content-free, monotonic.
    sources_invalidated: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    started_at: Mapped[CreatedAt]
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentRunStep(Base):
    """One tool call within a run. Content-free by construction."""

    __tablename__ = "agent_run_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_run_steps_run_id_seq"),)

    id: Mapped[IdPk]
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: A hash of the arguments, so a retry can be recognised as the same call
    #: without storing what was passed.
    args_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[CreatedAt]


CONFIRMATION_STATUSES = (
    "pending",
    "confirmed",
    "applying",
    "applied",
    "failed",
    "rejected",
    "expired",
)


class ToolConfirmation(Base):
    """A model-initiated state change held for the owner's tap (§7.1, S03.1).

    In a tainted run a `configure`/`control` call is not executed: its
    validated arguments wait here and the owner gets a confirmation card.
    Confirm → `agent.apply_confirmation` runs it once (`confirmed` →
    `applied` / `failed`); Reject → `rejected`; no tap → `expired`.

    CONTENT-BEARING: `args` (the model may have put contact text in them).
    Nulled as soon as the call is decided, applied or expired (§10.4).
    """

    __tablename__ = "tool_confirmations"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_tool_confirmations_run_id_call_id"),
        CheckConstraint(
            "status IN " + str(CONFIRMATION_STATUSES).replace('"', "'"),
            name="ck_tool_confirmations_status",
        ),
        Index("ix_tool_confirmations_account_id_status", "account_id", "status"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    created_at: Mapped[CreatedAt]
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


#: I25 structural incident trace (ARCHITECTURE §17, finding R4-10). Every
#: stage has a closed code list; anything outside it is stored as `other`,
#: so no free text can ever reach a row. A migration freezes a copy of these
#: lists; `tests/integration/test_incident_trace.py` compares them with the
#: CHECK constraints of the migrated database (a new code needs a new migration).
TRACE_STAGES = (
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
_ACTION_STATES = (
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
TRACE_CODES: dict[str, tuple[str, ...]] = {
    # How the run found its durable request (C1 continuation selection).
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
    # Where the owner's instruction came from: C1 source purposes (turn id
    # beside them), or the legacy writer's instruction source.
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
    # A channel of the owner's wishes or context that did not reach the work.
    "dropped": (
        "dropped_quoted",
        "dropped_tainted",
        "guidance_ignored",
        "forwarded_turn",
        "managed_access_off",
        "other",
    ),
    "taint": ("started_tainted", "tainted_by_tool", "other"),
    # Recipient selection: the request binding (C5 basis) or the proposal's target.
    "recipient": (
        "unique_strong",
        "owner_choice",
        "explicit_reply",
        "unbound",
        "proposal_target",
        "other",
    ),
    # A proposal/revision/read result: C2 proposal_status or the wire status.
    "proposal": (
        "staged",
        "reused_exact",
        "revised",
        "stale",
        "idempotency_conflict",
        "proposal_conflict",
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
    # The action version a tool result showed the model, with C3 presentation.
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
    # The action state observed when the run finished.
    "outcome": (*_ACTION_STATES, "missing", "other"),
    "run": (*RUN_STATUSES, "other"),
    "trace": ("truncated", "other"),
}
TRACE_REASONS = (
    # C2 / proposal fixed reasons.
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
    # C1 continuation codes.
    "stale",
    "managed_access_off",
    # Action state reasons (actions.states.StateReason).
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
#: Rows per run, enforced by CHECK; the writer ends a full trace with one
#: `trace/truncated` row (I25: bounded in count).
MAX_TRACE_EVENTS_PER_RUN = 64
#: Nullable references (ON DELETE SET NULL), each with a partial index.
TRACE_REFERENCE_COLUMNS = ("turn_id", "request_id", "child_run_id", "action_id", "conversation_id")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(value) for value in values)})"


def trace_code_check() -> str:
    """The (stage, code) pairing as one SQL predicate."""
    return " OR ".join(
        f"(stage = '{stage}' AND {_in('code', codes)})" for stage, codes in TRACE_CODES.items()
    )


class IncidentTraceEvent(Base):
    """One structural decision of a run (I25). Ids and fixed codes only.

    Retention follows the run (ON DELETE CASCADE from `agent_runs`, one year);
    account deletion cascades; references to deleted rows become NULL.
    """

    __tablename__ = "incident_trace_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_incident_trace_events_run_id_seq"),
        CheckConstraint(
            f"seq >= 1 AND seq <= {MAX_TRACE_EVENTS_PER_RUN}", name="ck_incident_trace_events_seq"
        ),
        CheckConstraint(_in("stage", TRACE_STAGES), name="ck_incident_trace_events_stage"),
        CheckConstraint(trace_code_check(), name="ck_incident_trace_events_code"),
        CheckConstraint(_in("reason", TRACE_REASONS), name="ck_incident_trace_events_reason"),
        Index("ix_incident_trace_events_account_id_run_id", "account_id", "run_id"),
        # SET NULL references: hard deletes and report lookups use these.
        *(
            Index(
                f"ix_incident_trace_events_{column}",
                column,
                postgresql_where=text(f"{column} IS NOT NULL"),
            )
            for column in TRACE_REFERENCE_COLUMNS
        ),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    turn_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.control_turns.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.owner_requests.id", ondelete="SET NULL"), nullable=True
    )
    request_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    child_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    action_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.actions.id", ondelete="SET NULL"), nullable=True
    )
    action_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]


class LlmCall(Base):
    """One provider request, for budgeting (NFR-10)."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_account_id_created_at", "account_id", "created_at"),)

    id: Mapped[IdPk]
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: USD. Numeric, not float: money must not drift.
    cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
