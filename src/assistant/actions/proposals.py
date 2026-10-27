"""Proposal contract; S04.1 implements policy and lifecycle decisions."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.config import ActionsConfig
from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.core.clock import Clock
from assistant.policy.context import Decision


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    account_id: int
    conversation_id: int
    origin: ActionOrigin
    kind: ActionKind
    text: str
    base_message_id: int | None
    shown_through_message_id: int | None
    run_id: int | None
    tool_call_id: str | None
    trigger_turn_id: int | None
    draft_run_id: int | None
    draft_flags: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ProposalResult:
    action_id: int
    state: ActionState
    reused: bool
    superseded_action_id: int | None
    decision: Decision


async def propose(
    session: AsyncSession, request: ProposalRequest, *, clock: Clock, config: ActionsConfig
) -> ProposalResult:
    raise NotImplementedError("Proposal service is implemented in S04.1")
