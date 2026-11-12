"""Fixed, content-free facts shared by approval-card read paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.states import STATE_REASON_EXPLANATIONS, ActionState, StateReason


def state_reason_fact(reason: str | None) -> tuple[str | None, str | None]:
    """Never expose arbitrary persisted strings as trusted explanations."""
    if reason is None:
        return None, None
    try:
        code = StateReason(reason)
    except ValueError:
        return "unknown", "The reason is unavailable."
    return code.value, STATE_REASON_EXPLANATIONS[code]


def warning_reason_fact(reason: str | None) -> tuple[str | None, str | None]:
    explanations = {
        "source_unavailable": "A source is no longer available; check the chat.",
        "request_changed": "The owner request changed after this draft was made.",
        "owner_intent_superseded": "A later owner request takes precedence.",
    }
    if reason in explanations:
        assert reason is not None
        return reason, explanations[reason]
    return state_reason_fact(reason)


def draft_mode(flags: Mapping[str, Any] | None) -> str | None:
    mode = (flags or {}).get("mode")
    return mode if isinstance(mode, str) and mode in ("reply", "new_message", "revise") else None


@dataclass(frozen=True, slots=True)
class PresentationFact:
    outcome: str | None = None
    version: int | None = None


async def presentation_facts(
    session: AsyncSession, *, account_id: int, action_ids: Sequence[int]
) -> dict[int, PresentationFact]:
    """Latest recorded claim/resolution per card, including its version.

    A claim without a resolution says only 'claimed'; it does not prove the
    owner saw the card. Older-version outcomes remain explicitly versioned.
    """
    if not action_ids:
        return {}
    latest_ids = (
        select(func.max(AuditLogEntry.id))
        .where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id.in_(action_ids),
            AuditLogEntry.event.in_(
                ("approval_card_presentation_claimed", "approval_card_presentation_resolved")
            ),
        )
        .group_by(AuditLogEntry.subject_id)
    )
    rows = await session.execute(
        select(AuditLogEntry.subject_id, AuditLogEntry.event, AuditLogEntry.details).where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.id.in_(latest_ids),
        )
    )
    facts: dict[int, PresentationFact] = {}
    for action_id, event, details in rows:
        if not isinstance(details, dict):
            continue
        version = details.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            continue
        outcome = details.get("outcome")
        if event == "approval_card_presentation_claimed":
            outcome = "claimed"
        if outcome in ("claimed", "shown", "not_delivered"):
            facts[action_id] = PresentationFact(outcome, version)
    return facts


Presentation = Literal[
    "not_attempted", "in_flight", "shown", "not_delivered", "uncertain", "unknown"
]
PRESENTATION_TIMEOUT = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class ReceiptPresentation:
    outcome: Presentation = "not_attempted"
    version: int | None = None
    state: str | None = None
    shown_version: int | None = None
    shown_state: str | None = None


async def receipt_presentations(
    session: AsyncSession, *, account_id: int, action_ids: Sequence[int], now: datetime
) -> dict[int, ReceiptPresentation]:
    """C3 evidence, including successful in-place edits and unresolved attempts.

    The legacy Q3 reader remains unchanged for production-context parity.
    Pointers alone never certify delivery. An unresolved split send cannot
    become shown merely because its full-text companion reached Telegram.
    """
    if not action_ids:
        return {}
    shown_rows = await session.execute(
        select(AuditLogEntry.subject_id, AuditLogEntry.details)
        .where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id.in_(action_ids),
            AuditLogEntry.event == "card_presented",
        )
        .order_by(AuditLogEntry.subject_id, AuditLogEntry.id.desc())
        .ext(distinct_on(AuditLogEntry.subject_id))
    )
    shown: dict[int, tuple[int, str]] = {}
    states = {s.value for s in ActionState}
    for action_id, details in shown_rows:
        version, state = details.get("version"), details.get("state")
        if type(version) is int and version > 0 and isinstance(state, str) and state in states:
            shown[action_id] = (version, state)
    rows = await session.execute(
        select(
            AuditLogEntry.subject_id, AuditLogEntry.event, AuditLogEntry.details, AuditLogEntry.at
        )
        .where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id.in_(action_ids),
            AuditLogEntry.event.in_(
                (
                    "approval_card_presentation_claimed",
                    "approval_card_presentation_resolved",
                    "card_presented",
                    "card_presentation_failed",
                )
            ),
        )
        .order_by(AuditLogEntry.subject_id, AuditLogEntry.id.desc())
        .ext(distinct_on(AuditLogEntry.subject_id))
    )
    facts: dict[int, ReceiptPresentation] = {}
    for action_id, event, details, at in rows:
        if not isinstance(details, dict):
            continue
        version = details.get("version")
        if type(version) is not int or version < 1:
            continue
        state = details.get("state")
        if not isinstance(state, str) or state not in {s.value for s in ActionState}:
            state = None
        if event == "approval_card_presentation_claimed":
            outcome: Presentation = "in_flight" if now - at < PRESENTATION_TIMEOUT else "uncertain"
        elif event == "card_presented":
            outcome = "shown"
        elif details.get("outcome") in ("shown", "not_delivered", "uncertain"):
            raw = details["outcome"]
            if raw == "shown":
                outcome = "shown"
            elif raw == "not_delivered":
                outcome = "not_delivered"
            else:
                outcome = "uncertain"
        else:
            outcome = "unknown"
        success = shown.get(action_id)
        facts[action_id] = ReceiptPresentation(
            outcome,
            version,
            state,
            None if success is None else success[0],
            None if success is None else success[1],
        )
    return facts
