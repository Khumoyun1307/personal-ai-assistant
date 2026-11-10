"""Owner reply proposals and character-exact source copying (H3 / C1 / C2).

`propose_message` takes request/version/recipient and retained source ids;
application code validates account, request membership, purpose, role, whole
field SHA-256 and Unicode code-point offsets before copying the slices in
order. Dictation may come from earlier turns of the active request (D-S2,
2026-11-09). No model-supplied text, fuzzy match, normalization, punctuation
repair, strip or invented separator can establish authorship. A selected
assistant artifact needs its own source and a separate owner selection
instruction explicitly bound to those ordered source ids; chronology alone
does not select it. It remains generated. Scoped ids accompany source spans
in continuation context. Quotes/forwards never become owner words.

Invalid or unavailable sources return C2's nullable no-action result with
the v10 wire status `not_drafted`; `proposal_status` carries the C2 outcome.
Created cards keep `awaiting_approval` and `action_id` on the wire. Exact
retries reuse the artifact; changed requests conflict honestly (Q4),
including changed source ids and answered target. Proposals and literal
revisions share the durable run/call effect key across fresh executors.
Changed retries retain the original action receipt without staging text;
effect identity rows are separate from the action's lifecycle history.
Nothing here sends: the owner must tap the current card and execution must
pass policy again. Logs and receipts never contain the copied body.

The legacy `propose_reply` writer is retained until H4/H6 cutover. H3 changes
no used prompt and implements no model-written proposal tool. H9 records
per-version authorship/source ids and preserves generated-text classification.

The application supplies the owner's own current words to that writer even
in a tainted run, including the classified own-text component beside a quote
(D-S3). Model-written guidance is passed only from an untainted run. Source
copying likewise keeps validated owner words available while tainted; its
approval card remains the gate. Results containing contact names are
untrusted; content-free receipts do not introduce taint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.actions.authorship import VersionAuthorship, current_authorship, is_ai_generated
from assistant.actions.models import Action
from assistant.actions.proposals import ProposalRefused
from assistant.actions.request_models import OwnerRequest
from assistant.actions.requests import PositiveId, lock_requests
from assistant.actions.source_copy import CopyUnavailable, copy_request_body
from assistant.actions.states import LIVE_CARD, ActionOrigin, ActionState
from assistant.agent.draft_owner_schemas import WordBounds
from assistant.agent.drafting import DraftOutcome, DraftStatus, Stale, propose_draft
from assistant.agent.persistence import args_digest
from assistant.agent.tools.common import (
    ONE_TARGET_RULE,
    ArgumentRuleError,
    OptionalId,
    local_time,
    name,
    require_contacts,
    require_conversations,
)
from assistant.agent.tools.effects import find_effect, record_effect
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.conversations import modes
from assistant.conversations.models import Contact, Conversation
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
REQUEST_DIGEST = "proposal_request_digest"
EFFECT_KEY_CHARS = 256
CONFLICT_RULE = (
    "The existing draft was not changed and this request was not staged. "
    "Use revise_draft when offered, or ask the owner how to proceed."
)
GUIDANCE_IGNORED = "tainted_run"

#: Where the draft's instruction came from (PF-04, content-free).
InstructionSource = Literal["owner_turn", "model", "none", "dropped_tainted", "dropped_quoted"]

ANSWERED_RULE = (
    "answered_message_id must be one of this chat's recent contact messages (a [msg:<id>] "
    "a chat tool showed you), or null to answer the newest ones"
)
NEW_MESSAGE_ANSWERS_NOTHING = "a new_message answers no message: set answered_message_id to null"
WORDS_RULE = f"min_words and max_words are 1 to {MAX_WORDS}, or null"
WORDS_ORDER_RULE = "min_words must not be above max_words"


def _guidance(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not 1 <= len(stripped) <= GUIDANCE_CHARS:
        raise ArgumentRuleError(f"guidance must be 1 to {GUIDANCE_CHARS} characters, or null")
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
            "showed you, when the owner names one and you know its id. Null otherwise: the "
            "draft writer then finds the message the owner's words point at, or answers the "
            "newest. Null for new_message."
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


def _source_ids(value: list[int]) -> list[int]:
    if not 1 <= len(value) <= 64:
        raise ArgumentRuleError("source_ids must contain 1 to 64 ids")
    return value


# Keep length constraints out of the provider's strict-mode schema.
LiteralSourceIds = Annotated[list[PositiveId], AfterValidator(_source_ids)]


class ProposeMessageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: PositiveId
    request_version: PositiveId
    conversation_id: PositiveId
    source_ids: LiteralSourceIds
    answered_message_id: PositiveId | None = Field(
        description=(
            "The contact's message this text answers, a [msg:<id>] a chat tool showed you, "
            "when the owner names an earlier message; null otherwise."
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


def _request_digest(args: ProposeReplyArgs | ProposeMessageArgs) -> str:
    # Target aliases resolve to the same conversation before this comparison.
    excluded = {"conversation_id", "contact_id"} if isinstance(args, ProposeReplyArgs) else set()
    return args_digest(
        {
            "tool": "propose_reply" if isinstance(args, ProposeReplyArgs) else "propose_message",
            "request": args.model_dump(exclude=excluded),
        }
    )


async def _reuse_or_conflict(
    session: AsyncSession,
    ctx: ToolContext,
    target: _Target,
    action: Action,
    args: ProposeReplyArgs | ProposeMessageArgs,
) -> ToolOutcome:
    same_call = bool(ctx.call_id) and (action.run_id, action.tool_call_id) == (
        ctx.run_id,
        ctx.call_id,
    )
    if same_call or (action.draft_flags or {}).get(REQUEST_DIGEST) == _request_digest(args):
        return await _result(session, ctx.account_id, target, action.id, reused=True)
    # Missing metadata (older cards) cannot establish an identical request.
    return ToolOutcome(
        "conflict_existing_draft",
        {
            "action_id": action.id,
            "conversation_id": target.conversation_id,
            "state": action.state,
            "rule": CONFLICT_RULE,
        },
        contains_managed=True,
        refs=ToolRefs(frozenset({target.conversation_id}), frozenset({target.contact_id})),
    )


async def _record_request(
    session: AsyncSession,
    ctx: ToolContext,
    action_id: int,
    args: ProposeReplyArgs | ProposeMessageArgs,
) -> None:
    action = await session.scalar(
        select(Action).where(Action.account_id == ctx.account_id, Action.id == action_id)
    )
    if action is None:  # pragma: no cover - just created in this transaction
        raise NotInAccountError("action")
    # A hash only, committed with the action; no additional content is stored.
    action.draft_flags = {**(action.draft_flags or {}), REQUEST_DIGEST: _request_digest(args)}
    if isinstance(args, ProposeMessageArgs) and ctx.call_id and action.state == "awaiting_approval":
        # The version's flags may be replaced by a later revision. Keep the
        # effect key/digest independently so its exact retry stays factual.
        await record_effect(
            session,
            ctx,
            action_id=action.id,
            version=action.version,
            semantic_digest=_request_digest(args),
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
    replaced_action_id: int | None = None,
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
    if replaced_action_id is not None:
        # One card per chat: this proposal outdated the chat's earlier card —
        # named, so the agent can tell the owner which draft it replaced (to
        # change a live draft instead, `revise_draft`, PF-13).
        data["replaced_earlier_card"] = True
        data["replaced_action_id"] = replaced_action_id
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
    tainted or not, including the own-text component of a classified quote), and the
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
        # Unclassified or forwarded text: never an owner instruction (D-S3).
        source = "dropped_quoted"
    elif guidance is not None:
        source = "dropped_tainted"
    else:
        source = "none"
    return instruction, passed_guidance, source


# ------------------------------------------------------------ propose_reply
async def propose_reply_tool(ctx: ToolContext, args: ProposeReplyArgs) -> ToolOutcome:
    if "tool.propose_owner_message" in ctx.services.enabled_flags:
        return ToolOutcome(
            "not_available",
            {"reason": "use_propose_owner_message"},
            log_code="legacy_writer_retired",
        )
    services = ctx.services
    drafter, config = services.drafter, services.actions_config
    if drafter is None or config is None or services.approval_cards is None:
        return _not_available()
    async with transaction(services.session_factory) as uow:
        target = await _target(uow.session, ctx, args)
        if (refusal := _refused(target)) is not None:
            return refusal
        if (existing := await _existing(uow.session, ctx, target.conversation_id)) is not None:
            return await _reuse_or_conflict(uow.session, ctx, target, existing, args)
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
        "answered_by": None,
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
        observed |= {
            "bounds_retry": outcome.bounds_retry,
            "bounds_met": outcome.bounds_met,
            "answered_by": outcome.answered_by,
        }
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
                if proposal.reused:
                    existing = await uow.session.scalar(
                        select(Action).where(
                            Action.account_id == ctx.account_id, Action.id == proposal.action_id
                        )
                    )
                    assert existing is not None
                    return await _reuse_or_conflict(uow.session, ctx, target, existing, args)
                await _record_request(uow.session, ctx, proposal.action_id, args)
                return await _result(
                    uow.session,
                    ctx.account_id,
                    target,
                    proposal.action_id,
                    reused=proposal.reused,
                    guidance_ignored=ignored,
                    replaced_action_id=proposal.superseded_action_id,
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
    if outcome.answered_by is not None:
        # Named by the call, or found by the draft writer in the owner's words.
        facts["answers_message_id"] = outcome.base_message_id
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


# ------------------------------------------------------------ propose_message (H3 / C2)
def source_failure(
    status: str,
    reason: str,
    request: OwnerRequest | None = None,
) -> ToolOutcome:
    """A C2 no-action result using v10's existing status vocabulary."""
    return ToolOutcome(
        "not_drafted",
        {
            "proposal_status": status,
            "reason": reason,
            "request_id": None if request is None else request.id,
            "request_version": None if request is None else request.version,
            "action": None,
            "effect_version": None,
            "requested_change_applied": False,
        },
        log_code=reason,
    )


async def _source_receipt(
    session: AsyncSession,
    ctx: ToolContext,
    target: _Target,
    action: Action,
    args: ProposeMessageArgs,
    *,
    request: OwnerRequest,
    staged: bool = False,
) -> ToolOutcome:
    if action.text is None:
        return source_failure("not_available", "artifact_unavailable", request)
    same_call = bool(ctx.call_id) and (action.run_id, action.tool_call_id) == (
        ctx.run_id,
        ctx.call_id,
    )
    recorded = await find_effect(session, ctx)
    effect = recorded.details if recorded is not None and recorded.subject_id == action.id else None
    same_call = same_call or effect is not None
    digest = _request_digest(args)
    # A committed call key takes precedence over mutable card flags. In
    # particular, a revision key cannot replay the card's original proposal.
    exact = (
        effect.get("semantic_digest") == digest
        if effect is not None
        else (action.draft_flags or {}).get(REQUEST_DIGEST) == digest
    )
    blocked = staged and action.state == "blocked"
    proposal_status = (
        "blocked"
        if blocked
        else "staged"
        if staged
        else "reused_exact"
        if exact
        else "idempotency_conflict"
        if same_call
        else "proposal_conflict"
    )
    status = "not_drafted"
    data: dict[str, Any] = {
        "proposal_status": proposal_status,
        "reason": None if staged or exact else "existing_draft_conflict",
        "request_id": args.request_id,
        "request_version": request.version,
        "action": {
            "action_id": action.id,
            "conversation_id": action.conversation_id,
            "version": action.version,
            "state": action.state,
        },
        "effect_version": (
            action.version
            if staged and not blocked
            else effect.get("effect_version")
            if effect is not None and exact
            else None
        ),
        "requested_change_applied": staged and not blocked,
    }
    # Managed consent and instruction authority are independent (D-S3).
    # IDs/statuses alone do not taint; a returned contact name does.
    untrusted = False
    # Transitional facts for existing card consumers. The nullable action
    # union above is authoritative; a no-action result never contains these.
    if staged or exact:
        proof = current_authorship(action.authorship, version=action.version)
        written_by = (
            "owner"
            if not is_ai_generated(action.authorship, version=action.version)
            else "assistant_suggestion"
            if proof is not None and proof.kind == "agent_proposal_selected"
            else "assistant"
        )
        facts = await _result(
            session,
            ctx.account_id,
            target,
            action.id,
            reused=not staged,
            extra={"written_by": written_by},
        )
        data.update(facts.data)
        untrusted = facts.taints
        if facts.status in {"awaiting_approval", "blocked"}:
            status = facts.status
        else:
            data["reason"] = "no_longer_pending"
    else:
        data.update({"action_id": action.id, "state": action.state, "rule": CONFLICT_RULE})
    return ToolOutcome(
        status,
        data,
        contains_managed=True,
        untrusted=untrusted,
        refs=ToolRefs(frozenset({target.conversation_id}), frozenset({target.contact_id})),
    )


async def propose_message_tool(ctx: ToolContext, args: ProposeMessageArgs) -> ToolOutcome:
    services = ctx.services
    config = services.actions_config
    if config is None or services.approval_cards is None:
        return source_failure("not_available", "not_available")
    if not ctx.call_id or len(ctx.call_id) > EFFECT_KEY_CHARS:
        return source_failure("invalid_arguments", "invalid_effect_key")
    async with transaction(services.session_factory) as uow:
        session = uow.session
        if not await lock_requests(session, account_id=ctx.account_id):
            return source_failure("not_available", "not_found")
        request = await session.scalar(
            select(OwnerRequest)
            .where(OwnerRequest.account_id == ctx.account_id, OwnerRequest.id == args.request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if request is None:
            return source_failure("not_available", "not_found")
        try:
            target = await _target(
                session,
                ctx,
                _OneChat(conversation_id=args.conversation_id, contact_id=None),
            )
        except NotInAccountError:
            return source_failure("not_available", "not_found", request)
        effect = await find_effect(session, ctx)
        if effect is not None:
            artifact = await session.scalar(
                select(Action).where(
                    Action.account_id == ctx.account_id,
                    Action.id == effect.subject_id,
                )
            )
            if artifact is None:
                return source_failure("not_available", "artifact_unavailable", request)
            # A changed retry still refers to the original effect's artifact;
            # it must not lose that receipt or borrow the newly requested chat.
            if artifact.conversation_id != target.conversation_id:
                target = await _target(
                    session,
                    ctx,
                    _OneChat(conversation_id=artifact.conversation_id, contact_id=None),
                )
            return await _source_receipt(session, ctx, target, artifact, args, request=request)
        # Completed exact retries precede current request/source validation:
        # retention must not cause a retry to regenerate or recreate text.
        existing = await _existing(session, ctx, target.conversation_id)
        same_call = (
            existing is not None
            and bool(ctx.call_id)
            and (existing.run_id, existing.tool_call_id) == (ctx.run_id, ctx.call_id)
        )
        if existing is not None and (
            same_call or (existing.draft_flags or {}).get(REQUEST_DIGEST) == _request_digest(args)
        ):
            return await _source_receipt(session, ctx, target, existing, args, request=request)
        if existing is None and request.action_id is not None:
            existing = await session.scalar(
                select(Action).where(
                    Action.account_id == ctx.account_id,
                    Action.id == request.action_id,
                )
            )
            if existing is not None and (existing.draft_flags or {}).get(
                REQUEST_DIGEST
            ) == _request_digest(args):
                return await _source_receipt(session, ctx, target, existing, args, request=request)
        copied = await copy_request_body(
            session,
            account_id=ctx.account_id,
            request_id=request.id,
            source_ids=args.source_ids,
        )
        if isinstance(copied, CopyUnavailable):
            return source_failure("invalid_arguments", copied.reason, request)
        if existing is not None:
            return await _source_receipt(session, ctx, target, existing, args, request=request)
        if request.version != args.request_version:
            return source_failure("stale", "request_changed", request)
        if (
            request.state not in ("ready", "preparing")
            or request.expires_at <= services.clock.now()
        ):
            return source_failure("not_available", "request_unavailable", request)
        if (request.conversation_id, request.contact_id) != (
            target.conversation_id,
            target.contact_id,
        ):
            return source_failure("invalid_arguments", "recipient_mismatch", request)
        if (refusal := _refused(target)) is not None:
            return source_failure("not_available", str(refusal.data["reason"]), request)
        answered = args.answered_message_id
        if answered is not None and (
            services.drafter is None
            or answered
            not in await services.drafter.answerable_ids(
                session,
                ctx.account_id,
                target.conversation_id,
            )
        ):
            return source_failure("invalid_arguments", "answered_not_answerable", request)
        if request.target_message_id != answered:
            return source_failure("invalid_arguments", "target_mismatch", request)
        words = len(copied.text.split())
        minimum = request.constraints.get("min_words")
        maximum = request.constraints.get("max_words")
        if (isinstance(minimum, int) and words < minimum) or (
            isinstance(maximum, int) and words > maximum
        ):
            return source_failure("invalid_arguments", "length_not_met", request)
        dictated = copied.written_by == "owner"
        try:
            proposal = await propose_draft(
                session,
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
                dictated_text=copied.text if dictated else None,
                suggested_text=None if dictated else copied.text,
                answered_message_id=answered,
                authorship=VersionAuthorship(
                    version=1,
                    kind="owner_dictated" if dictated else "agent_proposal_selected",
                    source_ids=list(copied.source_ids),
                    parent_version=None,
                    retains_generated_text=not dictated,
                ),
            )
        except ProposalRefused as refused:
            return source_failure("not_available", refused.code, request)
        assert not isinstance(proposal, Stale)
        action = await session.scalar(
            select(Action).where(
                Action.account_id == ctx.account_id,
                Action.id == proposal.action_id,
            )
        )
        assert action is not None
        if not proposal.reused:
            await _record_request(session, ctx, action.id, args)
            request.action_id = action.id
            request.action_version = action.version
            request.state = (
                "awaiting_approval" if action.state == "awaiting_approval" else "suspended"
            )
            request.focused = action.state == "awaiting_approval"
            request.missing_slot = None
            request.version += 1
            request.updated_at = services.clock.now()
            if action.expires_at is not None:
                request.expires_at = action.expires_at
        outcome = await _source_receipt(
            session,
            ctx,
            target,
            action,
            args,
            request=request,
            staged=not proposal.reused,
        )
        return ToolOutcome(
            outcome.status,
            {**outcome.data, **({} if answered is None else {"answers_message_id": answered})},
            contains_managed=outcome.contains_managed,
            untrusted=outcome.untrusted,
            refs=outcome.refs,
            log_detail={
                "provenance": copied.written_by,
                "has_answered_target": answered is not None,
            },
        )


_RESULT_RULE = (
    "The result is never a send: 'awaiting_approval' means a card is on its way to "
    "the owner, 'blocked' means it cannot be sent (the card offers the text to copy), "
    "'not_drafted' means no draft was made (the reason says why), "
    "'declined_by_provider' means the AI provider refused to write it; 'reused' means "
    "the card already existed from an earlier call in this turn and this call created "
    "nothing new. 'conflict_existing_draft' means this request differs from the existing "
    "draft's request: nothing was changed or staged; use revise_draft when offered or "
    "ask the owner how to proceed. Never say the message was sent."
)

PROPOSE_REPLY = ToolDefinition(
    name="propose_reply",
    description=(
        "Write a message to one chat for the owner's approval: the draft is written from "
        "that chat and the owner's own message, and the owner gets a card to approve, edit "
        "or reject it. Use when the owner asks you to reply, to answer a particular "
        "message, or to write a new message about something (mode 'new_message'). When the "
        "owner names an earlier message, pass its answered_message_id if a chat tool showed "
        "you its id; otherwise leave it null and the draft writer, which reads the chat, "
        "picks the message the owner meant. It always drafts when the owner asked. Pass any "
        "length the owner gave in words as min_words/max_words; the result reports the real "
        "count. " + _RESULT_RULE
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
        "Copy exact literal-body source_ids from the selected request to its recipient's "
        "approval card. Supply request_id, request_version, conversation_id and the "
        "answered_message_id selected by that request (null for a standalone message). "
        "Sources can be from earlier owner turns; do not ask for a re-paste. Pass no text. "
        "The application copies source characters, without adding separators. An assistant "
        "suggestion needs its exact request source and separate owner selection proof; "
        "it remains assistant-written. Missing/invalid sources produce action=null, "
        "effect_version=null, requested_change_applied=false; preserve the request and "
        "report the missing source. Never regenerate as a fallback. 'awaiting_approval' "
        "means a card needs the owner's tap; 'blocked' means it cannot be sent; "
        "'not_drafted' means nothing new was made (say why from reason). 'reused' means "
        "the card already existed. proposal_status carries the detailed C2 outcome; "
        "a conflict never means the requested change was applied. Never say sent."
    ),
    args_model=ProposeMessageArgs,
    capability=CapabilityClass.PROPOSE,
    handler=propose_message_tool,
    release_flag=PROPOSE_MESSAGE_FLAG,
    returns_managed=True,
)
