"""Tool-less draft generation through the provider-neutral LLM port (S04.2).

This part has no proposal or delivery effects. Only validated structured text
leaves a run, together with the message ids the application actually showed it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    ConsentMissingError,
    active_consent,
)
from assistant.agent import prompt_registry
from assistant.agent.draft_context import DraftContext, build_draft_reply
from assistant.agent.draft_schemas import DraftResult, validate_draft
from assistant.agent.models import AgentRun
from assistant.agent.persistence import AgentRunRepository, LlmCallRepository, month_start
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.core.model_routes import processor_for_model
from assistant.db.uow import transaction
from assistant.llm.port import LLMClient, LLMError, LLMErrorKind, LLMRequest, StopReason
from assistant.memory import items, summaries
from assistant.memory.summaries import SourceMessage
from assistant.observability.logging import get_logger

logger = get_logger(__name__)
RUN_TYPE = "draft_reply"
_DRAFT_REASONS = frozenset(
    {
        "assist_ingestion",
        "triage_needs_reply",
        "redraft",
        "owner_request",
        "eval",
    }
)


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
class _Prepared:
    account_id: int
    run_id: int
    context: DraftContext
    prompt_label: str


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
        reason: str,
        guidance: str | None = None,
        run_tainted: bool = False,
        owner_run_id: int | None = None,
    ) -> DraftOutcome:
        if guidance is not None and run_tainted:
            raise ValueError("tainted runs cannot supply draft guidance")
        if reason not in _DRAFT_REASONS:
            raise ValueError("unknown draft reason")
        prepared = await self._prepare(
            account_id, conversation_id, reason, guidance, run_tainted, owner_run_id
        )
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

    async def _prepare(
        self,
        account_id: int,
        conversation_id: int,
        reason: str,
        guidance: str | None,
        run_tainted: bool,
        owner_run_id: int | None,
    ) -> _Prepared | DraftOutcome:
        prompt = prompt_registry.active(RUN_TYPE)
        async with transaction(self._session_factory) as uow:
            session = uow.session
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
            if not await self._consent_ok(session, account_id):
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
                guidance=guidance,
                run_tainted=run_tainted,
                now=self._clock.now(),
            )
            trigger = (
                f"draft:run:{owner_run_id}:conv:{conversation_id}"
                if owner_run_id is not None
                else f"draft:conv:{conversation_id}:{reason}"
            )
            run = AgentRunRepository(session, account_id).add(
                AgentRun(
                    run_type=RUN_TYPE,
                    trigger_ref=trigger,
                    model=self._config.model,
                    effort=self._config.effort,
                    prompt_version=prompt.label,
                    status="running",
                    started_at=self._clock.now(),
                )
            )
            await session.flush()
            return _Prepared(account_id, run.id, context, prompt.label)

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
            checked = validate_draft(validated, run_kind="draft")
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

    async def _consent_ok(self, session: AsyncSession, account_id: int) -> bool:
        processor = processor_for_model(self._config.model)
        if processor is None:
            return False
        for kind in (CONSENT_AI_CONTROL, CONSENT_AI_MANAGED):
            consent = await active_consent(session, account_id, kind=kind)
            if not consent.granted or processor not in consent.processors:
                return False
        return True

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
            status=status,
            error_class=error_class,
            calls=calls,
        )
