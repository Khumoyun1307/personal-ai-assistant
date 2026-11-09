"""Fixed, content-free facts shared by approval-card read paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.states import STATE_REASON_EXPLANATIONS, StateReason


def state_reason_fact(reason: str | None) -> tuple[str | None, str | None]:
    """Never expose arbitrary persisted strings as trusted explanations."""
    if reason is None:
        return None, None
    try:
        code = StateReason(reason)
    except ValueError:
        return "unknown", "The reason is unavailable."
    return code.value, STATE_REASON_EXPLANATIONS[code]


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
