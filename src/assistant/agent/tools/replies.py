"""`propose_reply`, `propose_message` (ARCHITECTURE §8.2; S04.2 §3.5; P2-T09).

Both are `propose` tools: they create an action through S04.1's proposal
service and the owner decides on its card. **Nothing here sends** (§6
invariants 1, 2): a result says `awaiting_approval` (a card is on its way),
`blocked` (the card offers copy-ready text) or `not_drafted` — never "sent"
(RG-20), and never the draft's text (the owner reads it on the card).

- `propose_reply` drafts synchronously inside the owner run (P2-T09) with
  the tool-less `draft_reply` run. The owner's `guidance` reaches the draft
  only from an untainted run (decisions R2, B3 E3); a tainted run drafts
  without it and the result says `guidance_ignored`. Guidance only shapes
  the draft: approval is only the card's button, and every send passes the
  policy check at execution.
- `propose_message` proposes the owner's exact words (R-05, FR-DRF-08): no
  model run, no base message (decisions R3).

Not gated by taint: the approval card is the gate (§7.1). Idempotent on
`(run_id, tool_call_id)`, and a retried owner turn reuses its live card
without drafting again. Ids are resolved within the account; a foreign or
unknown id reads "not found".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.actions.models import Action
from assistant.actions.proposals import ProposalRefused
from assistant.actions.states import LIVE_CARD, ActionOrigin, ActionState
from assistant.agent.drafting import DraftOutcome, DraftStatus, Stale, propose_draft
from assistant.agent.tools.common import (
    ONE_TARGET_RULE,
    ArgumentRuleError,
    OptionalId,
    local_time,
    name,
    require_contacts,
    require_conversations,
)
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.conversations import modes
from assistant.conversations.models import Contact, Conversation
from assistant.core.delivery import MAX_TEXT_LENGTH
from assistant.core.errors import NotInAccountError
from assistant.core.jobs import DraftReason
from assistant.db.uow import transaction

PROPOSE_REPLY_FLAG = "tool.propose_reply"
PROPOSE_MESSAGE_FLAG = "tool.propose_message"
#: The owner's guidance for one draft (S04.2 §3.2).
GUIDANCE_CHARS = 400
#: What every created proposal's result says about the card.
CARD_NOTE = "queued for the owner"
GUIDANCE_IGNORED = "tainted_run"


def _guidance(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not 1 <= len(stripped) <= GUIDANCE_CHARS:
        raise ArgumentRuleError(f"guidance must be 1 to {GUIDANCE_CHARS} characters, or null")
    return stripped


def _dictated(value: str) -> str:
    # Only the surrounding whitespace goes (Telegram drops it anyway); the
    # owner's words themselves are never rewritten (R-05).
    stripped = value.strip()
    if not 1 <= len(stripped) <= MAX_TEXT_LENGTH:
        raise ArgumentRuleError(f"text must be 1 to {MAX_TEXT_LENGTH} characters")
    return stripped


class _OneChat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: OptionalId = Field(description="The chat, or null to use contact_id.")
    contact_id: OptionalId = Field(description="The contact whose chat it is, or null.")

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.conversation_id is None) == (self.contact_id is None):
            raise ArgumentRuleError(ONE_TARGET_RULE)
        return self


class ProposeReplyArgs(_OneChat):
    guidance: Annotated[str | None, AfterValidator(_guidance)] = Field(
        description=(
            "The owner's own words on what the reply should say (e.g. 'say I can't come "
            "Friday, offer Monday'), at most 400 characters, or null."
        )
    )


class ProposeMessageArgs(_OneChat):
    text: Annotated[str, AfterValidator(_dictated)] = Field(
        description="The owner's exact message, word for word (1-4096 characters)."
    )


# ------------------------------------------------------------ shared steps
@dataclass(frozen=True, slots=True)
class _Target:
    conversation_id: int
    contact_id: int
    display_name: str | None
    is_bot: bool
    mode: str
    timezone: str


async def _target(session: AsyncSession, ctx: ToolContext, args: _OneChat) -> _Target:
    """The chat within the account; `NotInAccountError` otherwise ("not found")."""
    conversation_id = args.conversation_id
    if conversation_id is None:
        assert args.contact_id is not None
        await require_contacts(session, ctx.account_id, [args.contact_id])
        conversation_id = await modes.conversation_for_contact(
            session, ctx.account_id, args.contact_id
        )
    await require_conversations(session, ctx.account_id, [conversation_id])
    row = (
        await session.execute(
            select(
                Conversation.id,
                Conversation.contact_id,
                Contact.display_name,
                Contact.is_bot,
                Conversation.mode,
                Account.timezone,
            )
            .join(Contact, Contact.id == Conversation.contact_id)
            .join(Account, Account.id == Conversation.account_id)
            .where(Conversation.account_id == ctx.account_id, Conversation.id == conversation_id)
        )
    ).one_or_none()
    if row is None:
        raise NotInAccountError("conversation not found in this account")
    return _Target(int(row[0]), int(row[1]), row[2], bool(row[3]), str(row[4]), str(row[5]))


def _refused(target: _Target) -> ToolOutcome | None:
    """Ignore-mode and bot chats get no proposal (FR-OBS-04, FR-OBS-08)."""
    if target.is_bot:
        return _not_drafted(target, "bot_chat")
    if target.mode == modes.MODE_IGNORE:
        return _not_drafted(target, "ignored_chat")
    return None


async def _existing(session: AsyncSession, ctx: ToolContext, conversation_id: int) -> Action | None:
    """An action this call or this owner turn already created: a repeated
    call id (any state), or the turn's live card (a retried owner turn)."""
    if ctx.call_id:
        by_call = await session.scalar(
            select(Action).where(
                Action.account_id == ctx.account_id,
                Action.run_id == ctx.run_id,
                Action.tool_call_id == ctx.call_id,
            )
        )
        if by_call is not None:
            return by_call
    if ctx.turn_id is None:
        return None
    return await session.scalar(
        select(Action).where(
            Action.account_id == ctx.account_id,
            Action.conversation_id == conversation_id,
            Action.trigger_turn_id == ctx.turn_id,
            Action.state.in_([state.value for state in LIVE_CARD]),
        )
    )


def _status_of(state: ActionState) -> str:
    """The result's status: pending work is never reported as done (RG-20)."""
    if state in LIVE_CARD:
        return "awaiting_approval"
    if state is ActionState.BLOCKED:
        return "blocked"
    # A repeated call whose card moved on meanwhile: its state says where.
    return "no_longer_pending"


async def _result(
    session: AsyncSession,
    account_id: int,
    target: _Target,
    action_id: int,
    *,
    reused: bool = False,
    guidance_ignored: bool = False,
    replaced: bool = False,
) -> ToolOutcome:
    """The tool's result for one action. `reused`: this call created
    nothing — the action came from an earlier call of this run or this owner
    turn — so the model never reports a second card (PR #106 review)."""
    action = await session.scalar(
        select(Action).where(Action.account_id == account_id, Action.id == action_id)
    )
    if action is None:  # pragma: no cover - created or found in this transaction
        raise NotInAccountError("action")
    state = ActionState(action.state)
    status = _status_of(state)
    data: dict[str, Any] = {
        "action_id": action.id,
        "conversation_id": target.conversation_id,
        "contact_name": name(target.display_name),
        "card": CARD_NOTE,
        "sendable_until": (
            local_time(action.expires_at, target.timezone) if state in LIVE_CARD else None
        ),
    }
    if status == "blocked":
        data["reason"] = action.state_reason
    elif status == "no_longer_pending":
        data["state"] = state.value
    if reused:
        data["reused"] = True
    if guidance_ignored:
        data["guidance_ignored"] = GUIDANCE_IGNORED
    if replaced:
        # One card per chat: this proposal outdated the chat's earlier card.
        data["replaced_earlier_card"] = True
    return ToolOutcome(
        status,
        data,
        contains_managed=True,
        untrusted=True,
        refs=ToolRefs(frozenset({target.conversation_id}), frozenset({target.contact_id})),
    )


def _not_drafted(target: _Target, reason: str, *, guidance_ignored: bool = False) -> ToolOutcome:
    data: dict[str, Any] = {"conversation_id": target.conversation_id, "reason": reason}
    if guidance_ignored:
        data["guidance_ignored"] = GUIDANCE_IGNORED
    return ToolOutcome(
        "not_drafted",
        data,
        contains_managed=True,
        refs=ToolRefs(frozenset({target.conversation_id}), frozenset({target.contact_id})),
        log_code=f"not_drafted:{reason}",
    )


def _not_available(refused: ProposalRefused | None = None) -> ToolOutcome:
    """The model is told "not available"; a refused proposal keeps its code
    in the executor's log line for operations (PR #106 review)."""
    code = f"proposal_{refused.code}" if refused is not None else "not_available"
    return ToolOutcome("error", {"reason": "not available"}, log_code=code)


def _draft_code(outcome: DraftOutcome) -> str:
    if outcome.status is DraftStatus.NOT_NEEDED and outcome.result is not None:
        return outcome.result.skip_reason or DraftStatus.NOT_NEEDED.value
    return outcome.status.value


# ------------------------------------------------------------ propose_reply
async def propose_reply_tool(ctx: ToolContext, args: ProposeReplyArgs) -> ToolOutcome:
    services = ctx.services
    drafter, config = services.drafter, services.actions_config
    if drafter is None or config is None or services.approval_cards is None:
        return _not_available()
    async with transaction(services.session_factory) as uow:
        target = await _target(uow.session, ctx, args)
        if (refusal := _refused(target)) is not None:
            return refusal
        if (existing := await _existing(uow.session, ctx, target.conversation_id)) is not None:
            return await _result(uow.session, ctx.account_id, target, existing.id, reused=True)
    ignored = args.guidance is not None and ctx.tainted
    for _attempt in range(2):
        outcome = await drafter.draft(
            account_id=ctx.account_id,
            conversation_id=target.conversation_id,
            reason=DraftReason.OWNER_REQUEST,
            guidance=None if ctx.tainted else args.guidance,
            run_tainted=ctx.tainted,
            owner_run_id=ctx.run_id,
        )
        if outcome.status is not DraftStatus.DRAFTED:
            return _not_drafted(target, _draft_code(outcome), guidance_ignored=ignored)
        async with transaction(services.session_factory) as uow:
            try:
                proposal = await propose_draft(
                    uow.session,
                    account_id=ctx.account_id,
                    conversation_id=target.conversation_id,
                    outcome=outcome,
                    origin=ActionOrigin.AGENT_PROPOSAL,
                    run_id=ctx.run_id,
                    tool_call_id=ctx.call_id or None,
                    trigger_turn_id=ctx.turn_id,
                    clock=services.clock,
                    config=config,
                    presenter=services.approval_cards,
                )
            except ProposalRefused as refused:
                return _not_available(refused)
            if not isinstance(proposal, Stale):
                return await _result(
                    uow.session,
                    ctx.account_id,
                    target,
                    proposal.action_id,
                    reused=proposal.reused,
                    guidance_ignored=ignored,
                    replaced=proposal.superseded_action_id is not None,
                )
        # The chat moved on while the model wrote: draft once more (§3.5 step 4).
    return _not_drafted(target, "chat_changed", guidance_ignored=ignored)


# ------------------------------------------------------------ propose_message
async def propose_message_tool(ctx: ToolContext, args: ProposeMessageArgs) -> ToolOutcome:
    services = ctx.services
    config = services.actions_config
    if config is None or services.approval_cards is None:
        return _not_available()
    async with transaction(services.session_factory) as uow:
        target = await _target(uow.session, ctx, args)
        if (refusal := _refused(target)) is not None:
            return refusal
        if (existing := await _existing(uow.session, ctx, target.conversation_id)) is not None:
            return await _result(uow.session, ctx.account_id, target, existing.id, reused=True)
        try:
            proposal = await propose_draft(
                uow.session,
                account_id=ctx.account_id,
                conversation_id=target.conversation_id,
                outcome=DraftOutcome(DraftStatus.DRAFTED),
                origin=ActionOrigin.OWNER_DICTATED,
                run_id=ctx.run_id,
                tool_call_id=ctx.call_id or None,
                trigger_turn_id=ctx.turn_id,
                clock=services.clock,
                config=config,
                presenter=services.approval_cards,
                dictated_text=args.text,
            )
        except ProposalRefused as refused:
            return _not_available(refused)
        assert not isinstance(proposal, Stale)  # a dictated text is never stale
        return await _result(
            uow.session,
            ctx.account_id,
            target,
            proposal.action_id,
            reused=proposal.reused,
            replaced=proposal.superseded_action_id is not None,
        )


_RESULT_RULE = (
    "The result is never a send: 'awaiting_approval' means a card is on its way to "
    "the owner, 'blocked' means it cannot be sent (the card offers the text to copy), "
    "'not_drafted' means no draft was made; 'reused' means the card already existed "
    "from an earlier call in this turn and this call created nothing new. Never say "
    "the message was sent."
)

PROPOSE_REPLY = ToolDefinition(
    name="propose_reply",
    description=(
        "Draft a reply to one chat for the owner's approval: the draft is written from "
        "that chat only, and the owner gets a card to approve, edit or reject it. "
        "Use when the owner asks you to reply or to draft a reply; pass the owner's own "
        "wishes for its content as guidance. " + _RESULT_RULE
    ),
    args_model=ProposeReplyArgs,
    capability=CapabilityClass.PROPOSE,
    handler=propose_reply_tool,
    release_flag=PROPOSE_REPLY_FLAG,
    returns_managed=True,
)

PROPOSE_MESSAGE = ToolDefinition(
    name="propose_message",
    description=(
        "Propose the owner's exact words as a message to one chat, for the owner's "
        "approval on a card. Use when the owner dictates what to say ('tell Alex: ...'); "
        "pass their words exactly, never rephrased. " + _RESULT_RULE
    ),
    args_model=ProposeMessageArgs,
    capability=CapabilityClass.PROPOSE,
    handler=propose_message_tool,
    release_flag=PROPOSE_MESSAGE_FLAG,
    returns_managed=True,
)
