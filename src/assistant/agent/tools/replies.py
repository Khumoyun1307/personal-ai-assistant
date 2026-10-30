"""`propose_reply`, `propose_message` (ARCHITECTURE §8.2; S04.2 §3.5; P2-T09; PF-10-12).

Both are `propose` tools: they create an action through S04.1's proposal
service and the owner decides on its card. **Nothing here sends** (§6
invariants 1, 2): a result says `awaiting_approval` (a card is on its way),
`blocked` (the card offers copy-ready text), `not_drafted` or
`declined_by_provider` — never "sent" (RG-20), and never the draft's text
(the owner reads it on the card).

- `propose_reply` drafts synchronously inside the owner run (P2-T09) with
  the tool-less `draft_owner` prompt (PF-10). The owner asked, so it is
  never vetoed as "no reply needed" (owner decision D1, 2026-10-30). The
  application supplies the owner's own words: the current owner turn's text
  reaches the draft whenever that turn carries nothing third-party (not
  forwarded, not quoting, no managed content) — tainted run or not (PF-11,
  owner decision D2). The model's own `guidance` still reaches it only from
  an untainted run (decisions R2). `mode` is `reply` (answer the contact's
  message `answered_message_id`, or their newest) or `new_message` (a
  standalone message: no base message, nothing "answered"); a word range is
  checked by counting (one retry, then the honest count).
- `propose_message` proposes a given text with no model run (decisions R3)
  after a deterministic provenance check (PF-12): text found in the owner's
  own words of the current turn is `owner_dictated` (R-05, FR-DRF-08); text
  found in one of the last assistant replies is the assistant's suggestion
  the owner picked (`agent_proposal`, labelled so on the card); anything
  else is refused, pointing at `propose_reply` `mode='new_message'`.

Not gated by taint: the approval card is the gate (§7.1). Idempotent on
`(run_id, tool_call_id)`, and a retried owner turn reuses its live card
without drafting again. Ids are resolved within the account; a foreign or
unknown id reads "not found". Content-free observability (PF-04): the
executor's log line carries `instruction_source`, `mode`,
`has_answered_target`, `bounds_retry`, `bounds_met` (and `provenance` for
`propose_message`) — fixed codes and booleans, never text.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, ControlTurn
from assistant.actions.models import Action
from assistant.actions.proposals import ProposalRefused
from assistant.actions.states import LIVE_CARD, ActionOrigin, ActionState
from assistant.agent.draft_owner_schemas import WordBounds
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
from assistant.db.uow import transaction

PROPOSE_REPLY_FLAG = "tool.propose_reply"
PROPOSE_MESSAGE_FLAG = "tool.propose_message"
#: The owner's guidance for one draft (S04.2 §3.2).
GUIDANCE_CHARS = 400
#: The longest length in words a draft can be asked for.
MAX_WORDS = 1000
#: What every created proposal's result says about the card.
CARD_NOTE = "queued for the owner"
GUIDANCE_IGNORED = "tainted_run"
#: How many recent assistant replies a picked suggestion is looked up in.
SUGGESTION_TURNS = 6

#: Where the draft's instruction came from (PF-04, content-free).
InstructionSource = Literal["owner_turn", "model", "none", "dropped_tainted", "dropped_quoted"]

ANSWERED_RULE = (
    "answered_message_id must be one of this chat's recent contact messages (a [msg:<id>] "
    "a chat tool showed you), or null to answer the newest ones"
)
NEW_MESSAGE_ANSWERS_NOTHING = "a new_message answers no message: set answered_message_id to null"
WORDS_RULE = f"min_words and max_words are 1 to {MAX_WORDS}, or null"
WORDS_ORDER_RULE = "min_words must not be above max_words"
NOT_DICTATED_RULE = (
    "this text is neither in the owner's own message nor in one of your recent replies; "
    "to write a message from what the owner asked, use propose_reply with mode 'new_message'"
)


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


def _words(value: int | None) -> int | None:
    if value is not None and not 1 <= value <= MAX_WORDS:
        raise ArgumentRuleError(WORDS_RULE)
    return value


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
    mode: Literal["reply", "new_message"] = Field(
        description=(
            "'reply': answer the contact (answered_message_id, or their newest messages). "
            "'new_message': a new standalone message written from what the owner asked; "
            "it answers nothing in the chat."
        )
    )
    answered_message_id: OptionalId = Field(
        description=(
            "With mode 'reply': the contact's message to answer, a [msg:<id>] a chat tool "
            "showed you, when the owner names one; null for the newest. Null for new_message."
        )
    )
    guidance: Annotated[str | None, AfterValidator(_guidance)] = Field(
        description=(
            "Your short note on what the owner wants the message to say, at most 400 "
            "characters, or null. The owner's own message reaches the draft anyway."
        )
    )
    min_words: Annotated[int | None, AfterValidator(_words)] = Field(
        description="The owner's lowest length in words, or null."
    )
    max_words: Annotated[int | None, AfterValidator(_words)] = Field(
        description="The owner's highest length in words, or null."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.mode == "new_message" and self.answered_message_id is not None:
            raise ArgumentRuleError(NEW_MESSAGE_ANSWERS_NOTHING)
        if (
            self.min_words is not None
            and self.max_words is not None
            and self.min_words > self.max_words
        ):
            raise ArgumentRuleError(WORDS_ORDER_RULE)
        return self

    @property
    def bounds(self) -> WordBounds | None:
        if self.min_words is None and self.max_words is None:
            return None
        return WordBounds(self.min_words, self.max_words)


class ProposeMessageArgs(_OneChat):
    text: Annotated[str, AfterValidator(_dictated)] = Field(
        description=(
            "The exact message, word for word (1-4096 characters): the owner's own words "
            "from their message, or a text you suggested earlier that the owner chose."
        )
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
    extra: Mapping[str, Any] | None = None,
    log_detail: Mapping[str, Any] | None = None,
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
    if extra and not reused:
        data.update(extra)
    return ToolOutcome(
        status,
        data,
        contains_managed=True,
        untrusted=True,
        refs=ToolRefs(frozenset({target.conversation_id}), frozenset({target.contact_id})),
        log_detail=dict(log_detail or {}),
    )


def _not_drafted(
    target: _Target,
    reason: str,
    *,
    guidance_ignored: bool = False,
    status: str = "not_drafted",
    log_detail: Mapping[str, Any] | None = None,
) -> ToolOutcome:
    data: dict[str, Any] = {"conversation_id": target.conversation_id, "reason": reason}
    if guidance_ignored:
        data["guidance_ignored"] = GUIDANCE_IGNORED
    return ToolOutcome(
        status,
        data,
        contains_managed=True,
        refs=ToolRefs(frozenset({target.conversation_id}), frozenset({target.contact_id})),
        log_code=f"{status}:{reason}",
        log_detail=dict(log_detail or {}),
    )


def _not_available(refused: ProposalRefused | None = None) -> ToolOutcome:
    """The model is told "not available"; a refused proposal keeps its code
    in the executor's log line for operations (PR #106 review)."""
    code = f"proposal_{refused.code}" if refused is not None else "not_available"
    return ToolOutcome("error", {"reason": "not available"}, log_code=code)


def argument_error(field: str, rule: str, code: str) -> ToolOutcome:
    """A semantically invalid argument found by the handler, in the same
    shape as the executor's schema errors (field path and our fixed rule), so
    the model can correct the call. `code` goes to the log line only."""
    return ToolOutcome(
        "error",
        {
            "reason": "invalid arguments",
            "fields": [field],
            "problems": [{"field": field, "rule": rule}],
        },
        log_code=code,
        log_detail={"fields": [field]},
    )


# ------------------------------------------------------------ the owner's words (PF-11)
def owner_instruction(
    ctx: ToolContext, guidance: str | None
) -> tuple[str | None, str | None, InstructionSource]:
    """What reaches an owner-requested draft as the owner's wishes: the
    current owner turn's own words (supplied by the application; any run,
    tainted or not, when the turn carries nothing third-party), and the
    model's `guidance` from an untainted run only (decisions R2). Returns
    `(instruction, guidance, source)`; `source` is a content-free code."""
    turn = ctx.owner_turn
    instruction = turn.instruction if turn is not None else None
    passed_guidance = None if ctx.tainted else guidance
    source: InstructionSource
    if instruction is not None:
        source = "owner_turn"
    elif passed_guidance is not None:
        source = "model"
    elif (
        turn is not None
        and turn.text
        and (turn.third_party or turn.quoted or turn.contains_managed)
    ):
        # Forwarded, quoting or carrying managed content: never an instruction.
        source = "dropped_quoted"
    elif guidance is not None:
        source = "dropped_tainted"
    else:
        source = "none"
    return instruction, passed_guidance, source


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
        answered = args.answered_message_id
        if answered is not None and answered not in await drafter.answerable_ids(
            uow.session, ctx.account_id, target.conversation_id
        ):
            return argument_error("answered_message_id", ANSWERED_RULE, "answered_not_answerable")
    instruction, guidance, source = owner_instruction(ctx, args.guidance)
    # The owner's wishes did reach the draft unless both were dropped.
    ignored = args.guidance is not None and guidance is None and instruction is None
    bounds = args.bounds
    observed: dict[str, Any] = {
        "instruction_source": source,
        "mode": args.mode,
        "has_answered_target": answered is not None,
        "bounds_retry": False,
        "bounds_met": None,
    }
    for _attempt in range(2):
        outcome = await drafter.draft_owner(
            account_id=ctx.account_id,
            conversation_id=target.conversation_id,
            owner_run_id=ctx.run_id,
            mode=args.mode,
            instruction=instruction,
            guidance=guidance,
            run_tainted=ctx.tainted,
            answered_message_id=answered,
            bounds=bounds,
        )
        observed |= {"bounds_retry": outcome.bounds_retry, "bounds_met": outcome.bounds_met}
        if outcome.status is not DraftStatus.DRAFTED:
            return _owner_failure(target, outcome, ignored=ignored, observed=observed)
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
                    extra=_drafted_facts(args, outcome, instruction is not None),
                    log_detail=observed,
                )
        # The chat moved on while the model wrote: draft once more (§3.5 step 4).
    return _not_drafted(target, "chat_changed", guidance_ignored=ignored, log_detail=observed)


def _drafted_facts(
    args: ProposeReplyArgs, outcome: DraftOutcome, owner_words_used: bool
) -> dict[str, Any]:
    """What the model may relay about a staged draft: facts the application
    checked, never the text. A word range is reported with the exact count,
    met or not (PF-10: never a claim the count does not support)."""
    facts: dict[str, Any] = {"mode": args.mode, "owner_message_used": owner_words_used}
    if args.answered_message_id is not None:
        facts["answers_message_id"] = args.answered_message_id
    bounds = args.bounds
    if bounds is not None:
        facts["word_count"] = outcome.word_count
        facts["asked_length"] = bounds.describe()
        facts["length_met"] = outcome.bounds_met
    return facts


def _owner_failure(
    target: _Target, outcome: DraftOutcome, *, ignored: bool, observed: Mapping[str, Any]
) -> ToolOutcome:
    """An owner-requested draft that produced no text: the provider declined
    it, or it failed — said as it is, never as "no reply needed" (D1)."""
    if outcome.status is DraftStatus.REFUSED:
        return _not_drafted(
            target,
            "provider_refused",
            guidance_ignored=ignored,
            status="declined_by_provider",
            log_detail=observed,
        )
    if outcome.error_class == "answered_not_shown":
        # The message left the draft's window between the check and the run.
        return argument_error("answered_message_id", ANSWERED_RULE, "answered_not_shown")
    return _not_drafted(target, outcome.status.value, guidance_ignored=ignored, log_detail=observed)


# ------------------------------------------------------------ propose_message (PF-12)
#: Apostrophes and quote marks as one each (several look alike on purpose).
#: Uzbek writes o' and g' with any of the apostrophes.
_APOSTROPHES = "‘’‚‛ʼʻʹʽ`´′"  # noqa: RUF001
_QUOTE_MARKS = "“”„‟«»″"
_QUOTES = str.maketrans(
    {
        **dict.fromkeys(_APOSTROPHES, "'"),
        **dict.fromkeys(_QUOTE_MARKS, '"'),
        # Uzbek Cyrillic: HA WITH DESCENDER as HA (the Latin x/h fold's twin).
        "ҳ": "х",  # noqa: RUF001
    }
)
_EDGE = " '\".,!?;:…"


def normalise_for_match(text: str) -> str:
    """A text as the provenance check compares it: compatibility forms
    folded, case-folded, quotes and apostrophes unified, the Uzbek x/h
    spellings folded together, whitespace collapsed. Deterministic."""
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_QUOTES)
    return " ".join(folded.replace("x", "h").split())


def text_occurs_in(text: str, source: str | None) -> bool:
    """`text` (its surrounding quotes and end punctuation aside) is part of
    `source`, both normalised."""
    if not source:
        return False
    needle = normalise_for_match(text).strip(_EDGE)
    return bool(needle) and needle in normalise_for_match(source)


WrittenBy = Literal["owner", "assistant"]


async def _provenance(session: AsyncSession, ctx: ToolContext, text: str) -> WrittenBy | None:
    """Who wrote `text`: the owner (in the current turn's own words), the
    assistant (one of its last replies before that turn), or None."""
    turn = ctx.owner_turn
    if turn is not None and text_occurs_in(text, turn.own_words):
        return "owner"
    before = turn.id if turn is not None else ctx.turn_id
    statement = select(ControlTurn.text).where(
        ControlTurn.account_id == ctx.account_id,
        ControlTurn.role == "assistant",
        ControlTurn.text.is_not(None),
    )
    if before is not None:
        statement = statement.where(ControlTurn.id < before)
    replies = await session.scalars(
        statement.order_by(ControlTurn.id.desc()).limit(SUGGESTION_TURNS)
    )
    if any(text_occurs_in(text, reply) for reply in replies):
        return "assistant"
    return None


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
        written_by = await _provenance(uow.session, ctx, args.text)
        if written_by is None:
            return argument_error("text", NOT_DICTATED_RULE, "text_not_dictated")
        dictated = written_by == "owner"
        try:
            proposal = await propose_draft(
                uow.session,
                account_id=ctx.account_id,
                conversation_id=target.conversation_id,
                outcome=DraftOutcome(DraftStatus.DRAFTED),
                origin=ActionOrigin.OWNER_DICTATED if dictated else ActionOrigin.AGENT_PROPOSAL,
                run_id=ctx.run_id,
                tool_call_id=ctx.call_id or None,
                trigger_turn_id=ctx.turn_id,
                clock=services.clock,
                config=config,
                presenter=services.approval_cards,
                dictated_text=args.text if dictated else None,
                suggested_text=None if dictated else args.text,
            )
        except ProposalRefused as refused:
            return _not_available(refused)
        assert not isinstance(proposal, Stale)  # a given text is never stale
        return await _result(
            uow.session,
            ctx.account_id,
            target,
            proposal.action_id,
            reused=proposal.reused,
            replaced=proposal.superseded_action_id is not None,
            extra={"written_by": "owner" if dictated else "assistant_suggestion"},
            log_detail={"provenance": written_by},
        )


_RESULT_RULE = (
    "The result is never a send: 'awaiting_approval' means a card is on its way to "
    "the owner, 'blocked' means it cannot be sent (the card offers the text to copy), "
    "'not_drafted' means no draft was made (the reason says why), "
    "'declined_by_provider' means the AI provider refused to write it; 'reused' means "
    "the card already existed from an earlier call in this turn and this call created "
    "nothing new. Never say the message was sent."
)

PROPOSE_REPLY = ToolDefinition(
    name="propose_reply",
    description=(
        "Write a message to one chat for the owner's approval: the draft is written from "
        "that chat and the owner's own message, and the owner gets a card to approve, edit "
        "or reject it. Use when the owner asks you to reply, to answer a particular "
        "message (answered_message_id), or to write a new message about something "
        "(mode 'new_message'). It always drafts when the owner asked. Pass any length the "
        "owner gave in words as min_words/max_words; the result reports the real count. "
        + _RESULT_RULE
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
        "Propose an exact text as a message to one chat, for the owner's approval on a "
        "card. Use when the owner dictates the words ('tell Alex: ...'; pass their words "
        "exactly, never rephrased) or picks a text you suggested earlier. Any other text "
        "is refused: to compose a message from what the owner asked, use propose_reply "
        "with mode 'new_message'. " + _RESULT_RULE
    ),
    args_model=ProposeMessageArgs,
    capability=CapabilityClass.PROPOSE,
    handler=propose_message_tool,
    release_flag=PROPOSE_MESSAGE_FLAG,
    returns_managed=True,
)
