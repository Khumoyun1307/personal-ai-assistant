"""The `draft_reply` run, proposals from drafts, and the `replies.draft` job (S04.2).

- `Drafter` — tool-less draft generation through the provider-neutral LLM
  port (§3.3): `draft` (a new reply) and `revise` (a card's revision, P2-T08).
  Only validated structured text leaves a run, together with the message ids
  the application actually showed it (never ids from model output).
- `propose_draft` — turns a draft, or the owner's dictated text, into an
  action through S04.1's `propose()`, after a locked freshness check (§3.4).
- `AssistDraftJob` — the `replies.draft` job body: re-checks, the trailing
  debounce, one draft, at most one card per burst (§3.9).

Nothing here sends: a proposal is an `awaiting_approval` (or `blocked`) action
whose card the owner decides on (AGENTS §6 invariants 1, 2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    ConsentMissingError,
    active_consent,
)
from assistant.actions.config import ActionsConfig
from assistant.actions.drafts import FLAGGED_REASONS, JOB_REASONS, redraft_queueing_lock
from assistant.actions.models import Action
from assistant.actions.ports import ApprovalCardPresenter
from assistant.actions.proposals import (
    ProposalRefused,
    ProposalRequest,
    ProposalResult,
    propose,
)
from assistant.actions.states import LIVE_CARD, ActionKind, ActionOrigin, ActionState
from assistant.agent import prompt_registry
from assistant.agent.budget import check_budget_thresholds
from assistant.agent.draft_context import DraftContext, DraftRevision, build_draft_reply
from assistant.agent.draft_schemas import DraftResult, validate_draft
from assistant.agent.models import AgentRun
from assistant.agent.persistence import AgentRunRepository, LlmCallRepository, month_start
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.core.jobs import DRAFT_TASK, DraftReason, JobScheduler, draft_queueing_lock
from assistant.core.model_routes import processor_for_model
from assistant.core.reply_window import is_open
from assistant.db.uow import transaction
from assistant.inbox.notify import OwnerAlerts
from assistant.llm.port import LLMClient, LLMError, LLMErrorKind, LLMRequest, StopReason
from assistant.memory import items, summaries
from assistant.memory.summaries import SourceMessage
from assistant.observability.logging import get_logger

logger = get_logger(__name__)
RUN_TYPE = "draft_reply"


@dataclass(frozen=True, slots=True)
class DraftConfig:
    model: str
    effort: str | None = "low"
    max_output_tokens: int = 1000
    max_output_tokens_ceiling: int = 2000
    call_timeout_seconds: float = 60.0
    window_messages: int = 30
    monthly_budget_usd: Decimal | None = None


class DraftStatus(StrEnum):
    DRAFTED = "drafted"
    NOT_NEEDED = "not_needed"
    NOT_DRAFTABLE = "not_draftable"
    NO_CONSENT = "no_consent"
    OVER_BUDGET = "over_budget"
    REFUSED = "refused"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    status: DraftStatus
    result: DraftResult | None = None
    run_id: int | None = None
    base_message_id: int | None = None
    shown_through_message_id: int | None = None
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class _Request:
    account_id: int
    trigger: str
    run_kind: Literal["draft", "revise"]
    conversation_id: int | None = None
    guidance: str | None = None
    run_tainted: bool = False
    action_id: int | None = None
    turn_id: int | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    account_id: int
    run_id: int
    context: DraftContext
    prompt_label: str
    run_kind: Literal["draft", "revise"]


async def draft_consent_ok(session: AsyncSession, account_id: int, model: str) -> bool:
    """`ai_control` and `ai_managed` both active and both naming the processor
    that serves `model` (§6 invariant 9). The wrapper checks again on the call."""
    processor = processor_for_model(model)
    if processor is None:
        return False
    for kind in (CONSENT_AI_CONTROL, CONSENT_AI_MANAGED):
        consent = await active_consent(session, account_id, kind=kind)
        if not consent.granted or processor not in consent.processors:
            return False
    return True


class Drafter:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        clock: Clock,
        config: DraftConfig,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._clock = clock
        self._config = config

    @property
    def config(self) -> DraftConfig:
        return self._config

    async def draft(
        self,
        *,
        account_id: int,
        conversation_id: int,
        reason: DraftReason,
        guidance: str | None = None,
        run_tainted: bool = False,
        owner_run_id: int | None = None,
    ) -> DraftOutcome:
        """A new reply for one conversation. `reason` goes into the run's
        `trigger_ref`; `guidance` is the owner's own words, and a tainted run
        never passes it (decisions R2)."""
        if guidance is not None and run_tainted:
            raise ValueError("tainted runs cannot supply draft guidance")
        why = DraftReason(reason)  # ValueError for anything else
        trigger = (
            f"draft:run:{owner_run_id}:conv:{conversation_id}"
            if owner_run_id is not None
            else f"draft:conv:{conversation_id}:{why.value}"
        )
        return await self._run(
            _Request(
                account_id,
                trigger,
                "draft",
                conversation_id=conversation_id,
                guidance=guidance,
                run_tainted=run_tainted,
            )
        )

    async def revise(self, *, account_id: int, action_id: int, turn_id: int) -> DraftOutcome:
        """A revision of a card being edited (S04.3 calls it; P2-T08): the
        action must still be `revising` with this owner turn as its revision
        message. The application, not this run, decides which text becomes
        the new version (a `replacement` keeps the owner's words verbatim)."""
        return await self._run(
            _Request(
                account_id,
                f"revise:action:{action_id}:turn:{turn_id}",
                "revise",
                action_id=action_id,
                turn_id=turn_id,
            )
        )

    async def _run(self, request: _Request) -> DraftOutcome:
        prepared = await self._prepare(request)
        if isinstance(prepared, DraftOutcome):
            return prepared
        try:
            result, calls = await self._call(prepared)
            status = (
                "succeeded"
                if result.status in {DraftStatus.DRAFTED, DraftStatus.NOT_NEEDED}
                else "refused"
                if result.status is DraftStatus.REFUSED
                else "failed"
            )
            await self._finish(prepared, status, result.error_class, calls)
            return replace(
                result,
                run_id=prepared.run_id,
                base_message_id=prepared.context.base_message_id,
                shown_through_message_id=prepared.context.shown_through_message_id,
            )
        except BaseException as error:
            # Cancellation is included: shutdown must not leave a running row.
            await self._finish(prepared, "failed", f"exception:{type(error).__name__}", 0)
            raise

    async def _prepare(self, request: _Request) -> _Prepared | DraftOutcome:
        prompt = prompt_registry.active(RUN_TYPE)
        account_id = request.account_id
        async with transaction(self._session_factory) as uow:
            session = uow.session
            revision: DraftRevision | None = None
            conversation_id = request.conversation_id
            if request.run_kind == "revise":
                loaded = await _revision_inputs(session, request)
                if loaded is None:
                    return DraftOutcome(DraftStatus.NOT_DRAFTABLE)
                conversation_id, revision = loaded
            assert conversation_id is not None
            row = (
                await session.execute(
                    select(Conversation, Contact)
                    .join(Contact, Conversation.contact_id == Contact.id)
                    .where(
                        Conversation.account_id == account_id,
                        Conversation.id == conversation_id,
                        Contact.account_id == account_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise NotInAccountError("conversation not found in this account")
            conversation, contact = row
            if conversation.mode == "ignore" or contact.is_bot:
                return DraftOutcome(DraftStatus.NOT_DRAFTABLE)
            if not await draft_consent_ok(session, account_id, self._config.model):
                return DraftOutcome(DraftStatus.NO_CONSENT)
            if self._config.monthly_budget_usd is not None:
                spent = await LlmCallRepository(session, account_id).spend_since(
                    month_start(self._clock.now())
                )
                if spent >= self._config.monthly_budget_usd:
                    return DraftOutcome(DraftStatus.OVER_BUDGET)
            messages = await self._messages(session, account_id, conversation_id)
            if not messages:
                return DraftOutcome(DraftStatus.NOT_DRAFTABLE)
            account = await session.get(Account, account_id)
            if account is None:
                raise NotInAccountError("account not found")
            notes = await items.contact_facing(
                session, account_id, conversation_id, include_owner_preferences=True
            )
            previous = await summaries.rolling_summary(session, account_id, conversation_id)
            context = build_draft_reply(
                prompt=prompt,
                account=account,
                messages=messages,
                notes=notes,
                previous=previous.summary if previous is not None else None,
                contact_name=await summaries.contact_display_name(
                    session, account_id, conversation_id
                ),
                guidance=request.guidance,
                run_tainted=request.run_tainted,
                revision=revision,
                now=self._clock.now(),
            )
            run = AgentRunRepository(session, account_id).add(
                AgentRun(
                    run_type=RUN_TYPE,
                    trigger_ref=request.trigger,
                    model=self._config.model,
                    effort=self._config.effort,
                    prompt_version=prompt.label,
                    status="running",
                    started_at=self._clock.now(),
                )
            )
            await session.flush()
            return _Prepared(account_id, run.id, context, prompt.label, request.run_kind)

    async def _messages(
        self, session: AsyncSession, account_id: int, conversation_id: int
    ) -> list[SourceMessage]:
        rows = (
            await session.scalars(
                select(Message)
                .where(
                    Message.account_id == account_id,
                    Message.conversation_id == conversation_id,
                    Message.deleted_at.is_(None),
                    Message.purged_at.is_(None),
                    Message.direction.is_not(None),
                    Message.sent_at.is_not(None),
                    Message.text.is_not(None) | Message.media.is_not(None),
                )
                .order_by(Message.id.desc())
                .limit(min(30, max(1, self._config.window_messages)))
            )
        ).all()
        return [
            SourceMessage(
                row.id,
                str(row.direction),
                row.sent_at,
                row.content_type,
                row.text,
                (row.media or {}).get("kind"),
                row.edited_at,
                row.content_hash,
            )
            for row in reversed(rows)
            if row.sent_at is not None
        ]

    async def _call(self, prepared: _Prepared) -> tuple[DraftOutcome, int]:
        request = LLMRequest(
            account_id=prepared.account_id,
            purpose=RUN_TYPE,
            model=self._config.model,
            instructions=prepared.context.instructions,
            items=prepared.context.items,
            tools=(),
            effort=self._config.effort,
            run_id=prepared.run_id,
            max_output_tokens=self._config.max_output_tokens,
            timeout_seconds=self._config.call_timeout_seconds,
            prompt_cache_key=f"{RUN_TYPE}:{prepared.prompt_label}",
        )
        calls = 0
        try:
            calls += 1
            response = await self._llm.parse(request, DraftResult)
            larger = min(request.max_output_tokens * 2, self._config.max_output_tokens_ceiling)
            if response.stop_reason is StopReason.MAX_TOKENS and larger > request.max_output_tokens:
                calls += 1
                response = await self._llm.parse(
                    replace(request, max_output_tokens=larger), DraftResult
                )
            if response.stop_reason in {StopReason.REFUSAL, StopReason.CONTENT_FILTER}:
                return DraftOutcome(
                    DraftStatus.REFUSED, error_class=response.stop_reason.value
                ), calls
            if response.stop_reason is not StopReason.COMPLETED:
                return DraftOutcome(
                    DraftStatus.FAILED, error_class=response.stop_reason.value
                ), calls
            if response.parsed is None:
                return DraftOutcome(DraftStatus.INVALID, error_class="invalid_output"), calls
            validated = DraftResult.model_validate(response.parsed.model_dump())
            checked = validate_draft(validated, run_kind=prepared.run_kind)
            if not checked.valid:
                return DraftOutcome(DraftStatus.INVALID, error_class=checked.error_class), calls
            status = DraftStatus.DRAFTED if checked.result.needs_reply else DraftStatus.NOT_NEEDED
            return DraftOutcome(status, result=checked.result), calls
        except ConsentMissingError:
            return DraftOutcome(DraftStatus.NO_CONSENT, error_class="consent_missing"), calls
        except ValidationError:
            return DraftOutcome(DraftStatus.INVALID, error_class="invalid_output"), calls
        except LLMError as error:
            status = (
                DraftStatus.INVALID
                if error.kind is LLMErrorKind.INVALID_OUTPUT
                else DraftStatus.FAILED
            )
            return DraftOutcome(status, error_class=f"llm_{error.kind.value}"), calls

    async def _finish(
        self, prepared: _Prepared, status: str, error_class: str | None, calls: int
    ) -> None:
        async with transaction(self._session_factory) as uow:
            run = await AgentRunRepository(uow.session, prepared.account_id).get(prepared.run_id)
            if run is not None:
                run.status = status
                run.error_class = error_class
                run.iterations = calls
                run.finished_at = self._clock.now()
        logger.info(
            "draft_run_finished",
            account_id=prepared.account_id,
            run_id=prepared.run_id,
            run_kind=prepared.run_kind,
            status=status,
            error_class=error_class,
            calls=calls,
        )


async def _revision_inputs(
    session: AsyncSession, request: _Request
) -> tuple[int, DraftRevision] | None:
    """The action's conversation and current text, and the owner's revision
    message — or None when the action is no longer being revised with that
    turn (a stale `replies.revise` job). Account-scoped throughout."""
    action = (
        await session.execute(
            select(Action.conversation_id, Action.text).where(
                Action.account_id == request.account_id,
                Action.id == request.action_id,
                Action.state == ActionState.REVISING.value,
                Action.revision_turn_id == request.turn_id,
            )
        )
    ).one_or_none()
    if action is None or action.text is None:
        return None
    owner_message = await session.scalar(
        select(ControlTurn.text).where(
            ControlTurn.account_id == request.account_id,
            ControlTurn.id == request.turn_id,
            ControlTurn.role == "owner",
        )
    )
    if owner_message is None:
        return None
    return int(action.conversation_id), DraftRevision(action.text, owner_message)


# ------------------------------------------------------------ proposals (§3.4)
#: A language tag as the flags may store it: `draft_flags` is a non-content
#: column (decisions R8), so a model-written value that is not a short tag is
#: recorded as "und" (undetermined) instead.
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")


def draft_flags(result: DraftResult) -> dict[str, Any]:
    """The draft's non-content flags for `actions.draft_flags` (decisions R8):
    read by the card (S04.3) and by M06's `draft_safe`; in M04 they never
    allow anything — every draft needs the owner's approval."""
    language = result.language.strip()
    return {
        "needs_reply": result.needs_reply,
        "confidence": result.confidence,
        "sensitive_topic": result.sensitive_topic,
        "makes_commitment": result.makes_commitment,
        "should_escalate": result.should_escalate,
        "uses_only_permitted_information": result.uses_only_permitted_information,
        "language": language if _LANGUAGE_TAG.fullmatch(language) else "und",
    }


@dataclass(frozen=True, slots=True)
class Stale:
    """A contact message newer than anything the draft saw arrived while the
    model ran: the draft is discarded, nothing is proposed."""

    newest_contact_message_id: int


async def propose_draft(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    outcome: DraftOutcome,
    origin: ActionOrigin,
    run_id: int | None,
    tool_call_id: str | None,
    trigger_turn_id: int | None,
    clock: Clock,
    config: ActionsConfig,
    presenter: ApprovalCardPresenter | None,
    dictated_text: str | None = None,
) -> ProposalResult | Stale:
    """One proposal, in the caller's transaction (§3.4).

    Locks in the §9.4 order (account `FOR SHARE`, conversation `FOR UPDATE`),
    then checks freshness: a draft is `Stale` if a contact message newer than
    what it was shown exists (the listener had nothing to outdate yet while
    the model ran). An owner-dictated text (`dictated_text`, origin
    `owner_dictated`) needs no model run: it has no base message (decisions
    R3) and is shown through the newest message of any direction now.
    Then S04.1's `propose()` (which re-takes the same locks harmlessly) and,
    for a new action, the card after commit. Raises `ProposalRefused` and
    `NotInAccountError` from `propose()`."""
    await _lock_for_proposal(session, account_id, conversation_id)
    flags: Mapping[str, Any] | None
    if origin is ActionOrigin.OWNER_DICTATED:
        if dictated_text is None:
            raise ValueError("a dictated proposal needs the owner's text")
        text = dictated_text
        base_message_id = None
        flags = None
        shown_through = await _newest_message_id(session, account_id, conversation_id)
    else:
        result = outcome.result
        if outcome.status is not DraftStatus.DRAFTED or result is None:
            raise ValueError("only a drafted outcome can be proposed")
        newest_contact = await _newest_message_id(
            session, account_id, conversation_id, direction="contact"
        )
        seen = outcome.shown_through_message_id
        if newest_contact is not None and (seen is None or newest_contact > seen):
            return Stale(newest_contact)
        text = result.text
        base_message_id = outcome.base_message_id
        flags = draft_flags(result)
        shown_through = seen
    proposal = await propose(
        session,
        ProposalRequest(
            account_id=account_id,
            conversation_id=conversation_id,
            origin=origin,
            kind=ActionKind.SEND_MESSAGE,
            text=text,
            base_message_id=base_message_id,
            shown_through_message_id=shown_through,
            run_id=run_id,
            tool_call_id=tool_call_id,
            trigger_turn_id=trigger_turn_id,
            draft_run_id=outcome.run_id,
            draft_flags=flags,
        ),
        clock=clock,
        config=config,
    )
    if not proposal.reused and presenter is not None:
        presenter.present_after_commit(session, proposal.action_id)
        if proposal.superseded_action_id is not None:
            # One card per chat: the replaced card shows "Outdated" at once.
            presenter.sync_after_commit(session, proposal.superseded_action_id)
    return proposal


async def _lock_for_proposal(session: AsyncSession, account_id: int, conversation_id: int) -> None:
    found = await session.scalar(
        select(Account.id).where(Account.id == account_id).with_for_update(read=True)
    )
    if found is None:
        raise NotInAccountError("account not found")
    locked = await session.scalar(
        select(Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.account_id == account_id)
        .with_for_update()
    )
    if locked is None:
        raise NotInAccountError("conversation not found in this account")


async def _newest_message_id(
    session: AsyncSession, account_id: int, conversation_id: int, *, direction: str | None = None
) -> int | None:
    statement = select(func.max(Message.id)).where(
        Message.account_id == account_id, Message.conversation_id == conversation_id
    )
    if direction is not None:
        statement = statement.where(Message.direction == direction)
    newest: int | None = await session.scalar(statement)
    return newest


# ------------------------------------------------------------ the replies.draft job
EVENT_DRAFT_SKIPPED = "draft_skipped"
EVENT_DRAFT_FAILED = "draft_failed"


@dataclass(frozen=True, slots=True)
class _Checked:
    """What the job's first transaction decided."""

    code: str
    account_id: int | None = None
    later: datetime | None = None

    @property
    def proceed(self) -> bool:
        return self.code == "proceed"


class AssistDraftJob:
    """`replies.draft(conversation_id, reason)` (§3.9).

    Re-checks everything when it runs (AGENTS §13), holds no lock across the
    model call, and is safe to run twice: the flag is cleared in the same
    transaction as the proposal, so a retry after a committed proposal finds
    nothing to do. Every check failure except a missing consent clears the
    flag (decisions R17); consent keeps it, and the reconciler waits for it.
    A re-draft (the owner's tap) never reads or clears the flag."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        drafter: Drafter,
        scheduler: JobScheduler,
        clock: Clock,
        config: ActionsConfig,
        presenter: ApprovalCardPresenter | None = None,
        alerts: OwnerAlerts | None = None,
        monthly_budget_usd: Decimal | None = None,
    ) -> None:
        self._factory = session_factory
        self._drafter = drafter
        self._scheduler = scheduler
        self._clock = clock
        self._config = config
        self._presenter = presenter
        self._alerts = alerts
        self._budget = monthly_budget_usd

    async def run(self, conversation_id: int, reason: str) -> str:
        """The job; returns a content-free code (also logged)."""
        try:
            why = DraftReason(reason)
        except ValueError:
            why = None
        if why is None or why not in JOB_REASONS:
            logger.warning("draft_job_bad_reason", conversation_id=conversation_id)
            return "bad_reason"
        checked = await self._check(conversation_id, why)
        if checked.later is not None:
            await self._defer(conversation_id, why, at=checked.later)
        if not checked.proceed or checked.account_id is None:
            logger.info("draft_job_skipped", conversation_id=conversation_id, code=checked.code)
            return checked.code
        code = await self._draft(checked.account_id, conversation_id, why)
        logger.info("draft_job_finished", conversation_id=conversation_id, code=code)
        return code

    async def _check(self, conversation_id: int, why: DraftReason) -> _Checked:
        flagged = why in FLAGGED_REASONS
        now = self._clock.now()
        async with transaction(self._factory) as uow:
            session = uow.session
            row = (
                await session.execute(
                    select(Conversation, Contact.is_bot, Account.status)
                    .join(Contact, Contact.id == Conversation.contact_id)
                    .join(Account, Account.id == Conversation.account_id)
                    .where(Conversation.id == conversation_id)
                    .with_for_update(of=Conversation)
                )
            ).one_or_none()
            if row is None:
                return _Checked("not_found")
            conversation, is_bot, account_status = row
            account_id = conversation.account_id
            if flagged and conversation.pending_draft_at is None:
                return _Checked("no_flag")  # done already, or cleared meanwhile
            if not await draft_consent_ok(session, account_id, self._drafter.config.model):
                return _Checked("no_consent")  # the only failure that keeps the flag
            skip = _skip_code(
                why,
                account_active=account_status == "active",
                is_bot=bool(is_bot),
                mode=conversation.mode,
                trigger=self._config.assist_draft_trigger,
                window_open=is_open(conversation.last_contact_msg_at, now),
            )
            if skip is not None:
                if flagged:
                    conversation.pending_draft_at = None
                _audit(session, account_id, conversation_id, EVENT_DRAFT_SKIPPED, skip, why, now)
                return _Checked(skip, account_id)
            if not flagged:
                return _Checked("proceed", account_id)  # a re-draft: no debounce, always new
            newest = (
                await session.execute(
                    select(Message.id, Message.created_at)
                    .where(
                        Message.account_id == account_id,
                        Message.conversation_id == conversation_id,
                        Message.direction == "contact",
                    )
                    .order_by(Message.id.desc())
                    .limit(1)
                )
            ).one_or_none()
            if newest is not None and newest.created_at + self._config.draft_debounce > now:
                # Trailing debounce: the burst is still going.
                return _Checked(
                    "debounced", account_id, later=newest.created_at + self._config.draft_debounce
                )
            covered = await session.scalar(
                select(func.count())
                .select_from(Action)
                .where(
                    Action.account_id == account_id,
                    Action.conversation_id == conversation_id,
                    Action.state.in_([state.value for state in LIVE_CARD]),
                    Action.shown_through_message_id >= (newest.id if newest is not None else 0),
                )
            )
            if covered:
                conversation.pending_draft_at = None  # a live card already saw it all
                return _Checked("nothing_new", account_id)
            return _Checked("proceed", account_id)

    async def _draft(self, account_id: int, conversation_id: int, why: DraftReason) -> str:
        flagged = why in FLAGGED_REASONS
        try:
            outcome = await self._drafter.draft(
                account_id=account_id, conversation_id=conversation_id, reason=why
            )
        except NotInAccountError:
            return "not_found"
        try:
            return await self._settle(account_id, conversation_id, why, outcome, flagged=flagged)
        finally:
            if outcome.run_id is not None and self._alerts is not None:
                await check_budget_thresholds(
                    self._factory,
                    self._clock,
                    self._alerts,
                    account_id=account_id,
                    monthly_budget_usd=self._budget,
                )

    async def _settle(
        self,
        account_id: int,
        conversation_id: int,
        why: DraftReason,
        outcome: DraftOutcome,
        *,
        flagged: bool,
    ) -> str:
        now = self._clock.now()
        if outcome.status is DraftStatus.NO_CONSENT:
            return "no_consent"  # revoked meanwhile: the flag waits for consent
        stale = False
        async with transaction(self._factory) as uow:
            session = uow.session
            if outcome.status is DraftStatus.DRAFTED:
                try:
                    proposal = await propose_draft(
                        session,
                        account_id=account_id,
                        conversation_id=conversation_id,
                        outcome=outcome,
                        origin=ActionOrigin.AGENT_PROPOSAL,
                        run_id=None,
                        tool_call_id=None,
                        trigger_turn_id=None,
                        clock=self._clock,
                        config=self._config,
                        presenter=self._presenter,
                    )
                except ProposalRefused as refused:
                    code = f"proposal_{refused.code}"
                    await self._clear(session, conversation_id, flagged=flagged)
                    _audit(session, account_id, conversation_id, EVENT_DRAFT_FAILED, code, why, now)
                    return code
                if isinstance(proposal, Stale):
                    stale = True  # keep the flag; a newer burst is waiting
                else:
                    await self._clear(session, conversation_id, flagged=flagged)
                    return f"proposed_{proposal.state.value}"
            else:
                code = _outcome_code(outcome)
                await self._clear(
                    session,
                    conversation_id,
                    flagged=flagged,
                    unless_newer_than=outcome.shown_through_message_id,
                )
                event = (
                    EVENT_DRAFT_SKIPPED
                    if outcome.status
                    in {DraftStatus.NOT_NEEDED, DraftStatus.NOT_DRAFTABLE, DraftStatus.OVER_BUDGET}
                    else EVENT_DRAFT_FAILED
                )
                _audit(session, account_id, conversation_id, event, code, why, now)
                return code
        if stale:
            later = now + self._config.draft_debounce if flagged else None
            await self._defer(conversation_id, why, at=later)
        return "stale"

    async def _clear(
        self,
        session: AsyncSession,
        conversation_id: int,
        *,
        flagged: bool,
        unless_newer_than: int | None = None,
    ) -> None:
        """Clear the flag under the conversation lock — unless a contact
        message newer than what the run saw arrived meanwhile: its own job
        (deferred while this one ran) will draft for it."""
        if not flagged:
            return
        conversation = await session.scalar(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        )
        if conversation is None:
            return
        if unless_newer_than is not None:
            newer = await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.account_id == conversation.account_id,
                    Message.conversation_id == conversation_id,
                    Message.direction == "contact",
                    Message.id > unless_newer_than,
                )
            )
            if newer:
                return
        conversation.pending_draft_at = None

    async def _defer(self, conversation_id: int, why: DraftReason, *, at: datetime | None) -> None:
        """Re-defer this job (its own row is `doing`, so the queueing lock is
        free); a conflict means another one is already waiting."""
        lock = (
            draft_queueing_lock(conversation_id)
            if why in FLAGGED_REASONS
            else redraft_queueing_lock(conversation_id)
        )
        await self._scheduler.defer(
            DRAFT_TASK,
            args={"conversation_id": conversation_id, "reason": why.value},
            queueing_lock=lock,
            schedule_at=at,
        )


def _skip_code(
    why: DraftReason,
    *,
    account_active: bool,
    is_bot: bool,
    mode: str,
    trigger: str,
    window_open: bool,
) -> str | None:
    """Why this job must not draft now, or None. Pure."""
    if not account_active:
        return "account_inactive"
    if is_bot:
        return "bot_chat"
    if why is DraftReason.REDRAFT:
        # The owner's own tap on a card: any mode but Ignore, any trigger.
        if mode == "ignore":
            return "ignore_mode"
    else:
        if mode != "assist":
            return "not_assist"
        wanted = "ingestion" if why is DraftReason.ASSIST_INGESTION else "triage"
        if trigger != wanted:
            return "trigger_off"
    if not window_open:
        return "window_closed"
    return None


def _outcome_code(outcome: DraftOutcome) -> str:
    if outcome.status is DraftStatus.NOT_NEEDED and outcome.result is not None:
        return outcome.result.skip_reason or DraftStatus.NOT_NEEDED.value
    return outcome.status.value


def _audit(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    event: str,
    code: str,
    why: DraftReason,
    now: datetime,
) -> None:
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor="system",
            event=event,
            subject_type="conversation",
            subject_id=conversation_id,
            details={"code": code, "reason": why.value},
        )
    )
