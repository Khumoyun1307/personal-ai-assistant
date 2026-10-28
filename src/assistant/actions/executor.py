"""At-most-once execution of an approved owner-voice action (ARCHITECTURE §9.4; S04.4).

The one place a business send happens (a confinement test checks it). The
protocol, exactly:

1. **Tx A** — lock account `FOR SHARE`, conversation `FOR SHARE`, the action
   `FOR UPDATE` (the global order), require `approved`, load the policy
   context from those locked rows and `decide` in the execution phase. A
   failed check moves the action to the state `execution_target` names (or
   leaves it `approved` and re-defers on `WAIT`); a pass claims it:
   `approved → executing` with a new `attempt_id`. The claim **commits before
   Telegram is called** (invariant 6).
2. **The call**, outside any transaction and holding no lock: one plain-text
   `sendMessage` through the gateway, which resolves the current connection.
3. **Tx B** — lock the conversation, then the action **of this attempt**
   (`executing` or `delivery_uncertain`), and record the outcome truthfully
   (invariant 11). An ambiguous outcome becomes `delivery_uncertain` and is
   never retried (invariant 6); nothing ever moves an action back from it.

`mark_uncertain` settles attempts that never reached Tx B (a crash or a cut
send) the same honest way. Every state change goes through
`lifecycle.transition`: conditional, audited, content-free.
"""

from __future__ import annotations

import html
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import connections
from assistant.accounts.models import Account, AuditLogEntry
from assistant.actions.config import ActionsConfig
from assistant.actions.lifecycle import sendable_length, text_hash, transition
from assistant.actions.models import Action
from assistant.actions.policy_loader import decision_record, load_context
from assistant.actions.ports import ApprovalCardPresenter, BusinessSender
from assistant.actions.states import ActionKind, ActionOrigin, ActionState, StateReason
from assistant.conversations.events import MessageContent
from assistant.conversations.models import Conversation, Message
from assistant.core.clock import Clock
from assistant.core.delivery import ErrorClass, SendResult
from assistant.core.jobs import EXECUTE_TASK, JobScheduler
from assistant.db.uow import transaction
from assistant.inbox.notify import AlertKind, OwnerAlerts
from assistant.memory.search import detect_script, guess_lang, normalize
from assistant.memory.summaries import contact_display_name
from assistant.observability.logging import get_logger
from assistant.policy.context import ActionFacts, CheckResult, Decision, Outcome, Phase
from assistant.policy.engine import decide, execution_target

logger = get_logger(__name__)

#: `inbox_drained` is a wait, not a denial (§9.3): the job comes back this soon.
WAIT_REDEFER = timedelta(seconds=3)
#: A 429 without `retry_after` is retried after this.
DEFAULT_RETRY_AFTER = timedelta(seconds=3)
#: The fifth rate-limited attempt on one action fails it (S04.4, TL decision),
#: counted from its `action_rate_limited` audit rows.
MAX_RATE_LIMITED_ATTEMPTS = 5
#: Beyond the request timeout and the throttle wait, before an attempt that
#: never reached Tx B counts as uncertain.
UNCERTAIN_MARGIN = timedelta(seconds=30)
#: Event of the connection-stale alert's dedupe (one per stale period).
STALE_ALERT_KEY = "connection_stale:{account_id}:{since}"
UNCERTAIN_ALERT_KEY = "uncertain:{action_id}"
#: The contact as the delivery-uncertain alert names them when no name is kept.
UNNAMED_CONTACT = "this contact"

_DEFINITE_FAILURES: dict[ErrorClass, StateReason] = {
    ErrorClass.PEER_UNAVAILABLE: StateReason.NOT_DELIVERED,
    ErrorClass.CONNECTION_INVALID: StateReason.CONNECTION_INVALID,
    ErrorClass.CHAT_UNREACHABLE: StateReason.NOT_DELIVERED,
    ErrorClass.FORBIDDEN: StateReason.NOT_DELIVERED,
    ErrorClass.BAD_REQUEST: StateReason.NOT_DELIVERED,
    ErrorClass.CONFLICT: StateReason.NOT_DELIVERED,
}


def _refused(check: str, code: str) -> Decision:
    """A Tx A refusal decided before the policy context could be loaded."""
    return Decision(Outcome.DENY, (CheckResult(check, False, code),))


def uncertain_after(
    configured: timedelta, *, request_timeout: timedelta, throttle_wait: timedelta
) -> timedelta:
    """`system.mark_uncertain`'s threshold: never below the time a claimed
    attempt can still be in flight (throttle wait, request timeout) plus a
    margin, whatever the setting says (S04.4 §3.4)."""
    return max(configured, request_timeout + throttle_wait + UNCERTAIN_MARGIN)


class ExecuteStatus(StrEnum):
    NOT_FOUND = "not_found"
    #: Not `approved` any more: another job claimed it, or it settled.
    SETTLED = "settled"
    #: A 429's `retry_at` is still ahead; the job comes back then.
    NOT_YET = "not_yet"
    #: The inbox has not caught up (`inbox_drained`); the job comes back in 3 s.
    WAITING = "waiting"
    #: A check failed at Tx A; the action moved to `state`, nothing was sent.
    DENIED = "denied"
    SENT = "sent"
    NOT_SENT = "not_sent"
    RATE_LIMITED = "rate_limited"
    UNCERTAIN = "uncertain"
    #: Tx B found no row of this attempt (settled by another path); nothing done.
    NO_MATCHING_ATTEMPT = "no_matching_attempt"
    #: Something failed after the claim; the row stays `executing` and
    #: `mark_uncertain` settles it. Never retried, never resent.
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ExecuteOutcome:
    status: ExecuteStatus
    state: ActionState | None = None
    reason: StateReason | None = None
    #: When the job must run again (WAIT, a future `retry_at`, a 429).
    redefer_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Claim:
    action_id: int
    account_id: int
    conversation_id: int
    text: str
    version: int
    attempt_id: str


@dataclass(frozen=True, slots=True)
class _Uncertain:
    action_id: int
    account_id: int
    conversation_id: int


class ActionExecutor:
    """`actions.execute` and `system.mark_uncertain` (S04.4 §3.2, §3.4)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        config: ActionsConfig,
        sender: BusinessSender,
        alerts: OwnerAlerts,
        scheduler: JobScheduler | None,
        presenter: ApprovalCardPresenter | None = None,
        new_attempt_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        """`config.mark_uncertain_after` is the effective threshold
        (`uncertain_after`, applied by bootstrap). `presenter` is S04.3's
        approval cards; None leaves cards alone (tests, or before S04.3)."""
        self._sf = session_factory
        self._clock = clock
        self._config = config
        self._sender = sender
        self._alerts = alerts
        self._scheduler = scheduler
        self._presenter = presenter
        self._new_attempt_id = new_attempt_id

    # ------------------------------------------------------------- execute
    async def execute(self, action_id: int) -> ExecuteOutcome:
        """Run the protocol once. An exception before the claim commits
        propagates (the job may retry: nothing was sent); after the claim
        nothing propagates, so no retry can ever send twice."""
        claimed = await self._tx_a(action_id)
        if isinstance(claimed, ExecuteOutcome):
            await self._redefer(action_id, claimed.redefer_at)
            return claimed
        try:
            result = await self._sender.send_business_text(
                account_id=claimed.account_id,
                conversation_id=claimed.conversation_id,
                text=claimed.text,
            )
        except Exception as error:
            # The gateway classifies every Telegram failure; this is a bug or
            # a crash inside it. The send may have happened: leave the row
            # `executing` for `mark_uncertain` (never resent).
            logger.error("action_send_raised", action_id=action_id, exc_type=type(error).__name__)
            return ExecuteOutcome(ExecuteStatus.INTERRUPTED, ActionState.EXECUTING)
        try:
            outcome = await self._tx_b(claimed, result)
        except Exception as error:
            logger.error(
                "action_outcome_not_recorded", action_id=action_id, exc_type=type(error).__name__
            )
            return ExecuteOutcome(ExecuteStatus.INTERRUPTED, ActionState.EXECUTING)
        await self._after_outcome(claimed, outcome)
        return outcome

    async def _tx_a(self, action_id: int) -> _Claim | ExecuteOutcome:
        async with transaction(self._sf) as uow:
            session = uow.session
            keys = (
                await session.execute(
                    select(Action.account_id, Action.conversation_id).where(Action.id == action_id)
                )
            ).one_or_none()
            if keys is None:
                return ExecuteOutcome(ExecuteStatus.NOT_FOUND)
            account_id, conversation_id = int(keys[0]), int(keys[1])
            # §9.4 lock order: account, conversation, (delegation: M06), action.
            await session.execute(
                select(Account.id).where(Account.id == account_id).with_for_update(read=True)
            )
            owned = await session.scalar(
                select(Conversation.id)
                .where(Conversation.id == conversation_id, Conversation.account_id == account_id)
                .with_for_update(read=True)
            )
            action = await session.scalar(
                select(Action)
                .where(Action.id == action_id, Action.account_id == account_id)
                .with_for_update()
            )
            if action is None:
                return ExecuteOutcome(ExecuteStatus.NOT_FOUND)
            now = self._clock.now()
            state = ActionState(action.state)
            if state is not ActionState.APPROVED:
                # A second job for one action, or one that settled meanwhile.
                return ExecuteOutcome(ExecuteStatus.SETTLED, state)
            if action.text is None:
                # P2-T37: a data deletion cleared the text; never sendable,
                # before any other rule.
                refusal = _refused("text_present", "data_deleted")
                return await self._deny(
                    session, action, now, refusal, ActionState.BLOCKED, StateReason.DATA_DELETED
                )
            if owned is None:
                # The action names a conversation that is not its account's:
                # fail closed (invariants 7, 12), whatever else would pass.
                refusal = _refused("conversation_owned", "not_owned")
                return await self._deny(
                    session, action, now, refusal, ActionState.BLOCKED, StateReason.POLICY_DENIED
                )
            if action.retry_at is not None and action.retry_at > now:
                return ExecuteOutcome(
                    ExecuteStatus.NOT_YET, ActionState.APPROVED, redefer_at=action.retry_at
                )
            decision = await self._decide(session, action, now)
            record = decision_record(decision, Phase.EXECUTION)
            if decision.outcome is Outcome.WAIT:
                await session.execute(
                    update(Action).where(Action.id == action.id).values(policy_decision=record)
                )
                logger.info("action_waiting_for_ingestion", action_id=action.id)
                return ExecuteOutcome(
                    ExecuteStatus.WAITING, ActionState.APPROVED, redefer_at=now + WAIT_REDEFER
                )
            if decision.outcome is not Outcome.ALLOW_AUTO:
                # DENY (REQUIRE_APPROVAL never arises at execution: fail closed).
                target = execution_target(decision)
                if target is None and decision.outcome is Outcome.DENY:
                    return ExecuteOutcome(ExecuteStatus.SETTLED, state)  # already claimed
                to_state, reason = target or (ActionState.BLOCKED, StateReason.POLICY_DENIED)
                return await self._deny(session, action, now, decision, to_state, reason)
            attempt = self._new_attempt_id()
            claimed = await transition(
                session,
                account_id=account_id,
                action_id=action.id,
                from_state=ActionState.APPROVED,
                to_state=ActionState.EXECUTING,
                reason=None,
                now=now,
                expected_version=action.version,
                values={
                    "attempt_id": attempt,
                    "attempt_started_at": now,
                    "sent_text_hash": text_hash(action.text),
                    "policy_decision": record,
                    "retry_at": None,
                },
            )
            if not claimed:
                return ExecuteOutcome(ExecuteStatus.SETTLED)
            claim = _Claim(
                action_id=action.id,
                account_id=account_id,
                conversation_id=conversation_id,
                text=action.text,
                version=action.version,
                attempt_id=attempt,
            )
        # Committed: the claim is durable before Telegram is called (§6.6).
        logger.info("action_claimed", action_id=claim.action_id, attempt_id=claim.attempt_id)
        return claim

    async def _decide(self, session: AsyncSession, action: Action, now: datetime) -> Decision:
        origin = ActionOrigin(action.origin)
        assert action.text is not None
        facts = ActionFacts(
            action_id=action.id,
            kind=ActionKind(action.kind),
            origin=origin,
            state=ActionState(action.state),
            version=action.version,
            text_length=sendable_length(action.text),
            created_at=action.created_at,
            expires_at=action.expires_at,
            shown_through_message_id=action.shown_through_message_id,
            owner_retry=False,
        )
        context = await load_context(
            session,
            account_id=action.account_id,
            conversation_id=action.conversation_id,
            facts=facts,
            base_message_id=action.base_message_id,
            target_message_id=action.target_message_id,
            # Decisions R3: an owner request is measured from its creation.
            owner_requested=(
                origin is ActionOrigin.OWNER_DICTATED or action.trigger_turn_id is not None
            ),
            phase=Phase.EXECUTION,
            now=now,
            config=self._config,
            ai_generated=origin is ActionOrigin.AGENT_PROPOSAL,
            process_run_id=self._config.process_run_id,
        )
        return decide(context)

    async def _deny(
        self,
        session: AsyncSession,
        action: Action,
        now: datetime,
        decision: Decision,
        to_state: ActionState,
        reason: StateReason,
    ) -> ExecuteOutcome:
        """Tx A said no: move the action, keep the execution-phase decision
        (codes only), and refresh its card. Nothing is sent."""
        failed = [check.name for check in decision.checks if not check.passed]
        applied = await transition(
            session,
            account_id=action.account_id,
            action_id=action.id,
            from_state=ActionState.APPROVED,
            to_state=to_state,
            reason=reason,
            now=now,
            expected_version=action.version,
            values={"policy_decision": decision_record(decision, Phase.EXECUTION)},
            details={"failed_rules": failed},
        )
        if not applied:
            return ExecuteOutcome(ExecuteStatus.SETTLED)
        self._sync_card(session, action.id)
        logger.info(
            "action_denied_at_execution",
            action_id=action.id,
            state=to_state.value,
            reason=reason.value,
            failed_rules=failed,
        )
        return ExecuteOutcome(ExecuteStatus.DENIED, to_state, reason)

    async def _tx_b(self, claim: _Claim, result: SendResult) -> ExecuteOutcome:
        async with transaction(self._sf) as uow:
            session = uow.session
            # §9.4 order: the conversation, then the action.
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == claim.conversation_id,
                    Conversation.account_id == claim.account_id,
                )
                .with_for_update()
            )
            row = (
                await session.execute(
                    select(Action.state, Action.text, Action.version)
                    .where(
                        Action.id == claim.action_id,
                        Action.account_id == claim.account_id,
                        Action.attempt_id == claim.attempt_id,
                        Action.state.in_(
                            [ActionState.EXECUTING.value, ActionState.DELIVERY_UNCERTAIN.value]
                        ),
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or conversation is None:
                # Never retried, never resent: this attempt is someone else's
                # to settle (or already settled).
                logger.warning("tx_b_no_matching_attempt", action_id=claim.action_id)
                return ExecuteOutcome(ExecuteStatus.NO_MATCHING_ATTEMPT)
            from_state = ActionState(row.state)
            late = from_state is ActionState.DELIVERY_UNCERTAIN
            outcome = await self._apply_outcome(
                session,
                claim,
                result,
                conversation=conversation,
                from_state=from_state,
                stored_text=row.text,
                late=late,
            )
            if outcome.state is not None and outcome.state is not from_state:
                self._sync_card(session, claim.action_id)
        logger.info(
            "action_outcome_recorded",
            action_id=claim.action_id,
            attempt_id=claim.attempt_id,
            status=outcome.status.value,
            error_class=result.error_class.value,
            error_code=result.error_code,
            late=late,
        )
        return outcome

    async def _apply_outcome(
        self,
        session: AsyncSession,
        claim: _Claim,
        result: SendResult,
        *,
        conversation: Conversation,
        from_state: ActionState,
        stored_text: str | None,
        late: bool,
    ) -> ExecuteOutcome:
        now = self._clock.now()
        error_class = result.error_class
        codes: dict[str, Any] = {"error_class": error_class.value, "error_code": result.error_code}

        async def move(
            to_state: ActionState,
            reason: StateReason | None,
            *,
            extra: dict[str, Any] | None = None,
            **values: Any,
        ) -> bool:
            return await transition(
                session,
                account_id=claim.account_id,
                action_id=claim.action_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
                now=now,
                values=values,
                details={"attempt_id": claim.attempt_id, **(extra or {})},
            )

        if result.ok:
            pending = not result.message_id or result.message_id <= 0
            telegram_id = None if pending else result.message_id
            await move(
                ActionState.SUCCEEDED,
                StateReason.SENT,
                extra={"identity_pending": True} if pending else None,
                telegram_message_id=telegram_id,
                identity_pending=pending,
                executed_at=now,
                error_class=None,
                error_code=None,
            )
            await self._record_message(
                session, claim, conversation, telegram_id=telegram_id, text=stored_text, now=now
            )
            return ExecuteOutcome(ExecuteStatus.SENT, ActionState.SUCCEEDED, StateReason.SENT)
        if result.delivery_uncertain:
            if late:
                return ExecuteOutcome(ExecuteStatus.UNCERTAIN, ActionState.DELIVERY_UNCERTAIN)
            await move(ActionState.DELIVERY_UNCERTAIN, StateReason.AMBIGUOUS, **codes)
            return ExecuteOutcome(
                ExecuteStatus.UNCERTAIN, ActionState.DELIVERY_UNCERTAIN, StateReason.AMBIGUOUS
            )
        # A definite failure: nothing was sent.
        if error_class is ErrorClass.PEER_UNAVAILABLE:
            conversation.availability = "possibly_paused"  # §6.6
            conversation.updated_at = now
        elif error_class is ErrorClass.CONNECTION_INVALID:
            await connections.mark_stale(session, claim.account_id, now)
        if late:
            # Decisions R4: a late definite result only ever fails the action —
            # never back to `approved`, never retried, never `expired`.
            await move(ActionState.FAILED, StateReason.NOT_DELIVERED, **codes)
            return ExecuteOutcome(
                ExecuteStatus.NOT_SENT, ActionState.FAILED, StateReason.NOT_DELIVERED
            )
        if error_class is ErrorClass.RATE_LIMITED:
            return await self._rate_limited(session, claim, result, stored_text, move, now)
        if error_class is ErrorClass.WINDOW_CLOSED:
            await move(ActionState.EXPIRED, StateReason.WINDOW_CLOSED, **codes)
            return ExecuteOutcome(
                ExecuteStatus.NOT_SENT, ActionState.EXPIRED, StateReason.WINDOW_CLOSED
            )
        reason = _DEFINITE_FAILURES.get(error_class, StateReason.NOT_DELIVERED)
        await move(ActionState.FAILED, reason, **codes)
        return ExecuteOutcome(ExecuteStatus.NOT_SENT, ActionState.FAILED, reason)

    async def _rate_limited(
        self,
        session: AsyncSession,
        claim: _Claim,
        result: SendResult,
        stored_text: str | None,
        move: Callable[..., Awaitable[bool]],
        now: datetime,
    ) -> ExecuteOutcome:
        codes: dict[str, Any] = {
            "error_class": result.error_class.value,
            "error_code": result.error_code,
        }
        earlier = await session.scalar(
            select(func.count())
            .select_from(AuditLogEntry)
            .where(
                AuditLogEntry.account_id == claim.account_id,
                AuditLogEntry.subject_type == "action",
                AuditLogEntry.subject_id == claim.action_id,
                AuditLogEntry.event == "action_rate_limited",
            )
        )
        if stored_text is None or int(earlier or 0) + 1 >= MAX_RATE_LIMITED_ATTEMPTS:
            # P2-T37: a text cleared by a deletion never becomes sendable
            # again; and the fifth 429 gives up (nothing was sent).
            await move(ActionState.FAILED, StateReason.NOT_DELIVERED, **codes)
            return ExecuteOutcome(
                ExecuteStatus.NOT_SENT, ActionState.FAILED, StateReason.NOT_DELIVERED
            )
        wait = (
            timedelta(seconds=result.retry_after)
            if result.retry_after is not None and result.retry_after > 0
            else DEFAULT_RETRY_AFTER
        )
        retry_at = now + wait
        # Not delivered: back to `approved` (its approval still stands), the
        # attempt closed, the job deferred to `retry_at` (TF §5).
        await move(
            ActionState.APPROVED,
            None,
            extra={"retry_after": int(wait.total_seconds())},
            attempt_id=None,
            attempt_started_at=None,
            retry_at=retry_at,
            **codes,
        )
        return ExecuteOutcome(ExecuteStatus.RATE_LIMITED, ActionState.APPROVED, redefer_at=retry_at)

    async def _record_message(
        self,
        session: AsyncSession,
        claim: _Claim,
        conversation: Conversation,
        *,
        telegram_id: int | None,
        text: str | None,
        now: datetime,
    ) -> None:
        """The `assistant_sent` row (§9.4 step 3). An echo processed first is
        absorbed by the upsert (LV-03: none observed; kept fail-closed). The
        stored text is the action's as it is now: a deletion that cleared it
        mid-send leaves the row without content, and an Ignore chat keeps
        metadata only."""
        keep = text is not None and conversation.mode != "ignore"
        content = MessageContent(content_type="text", text=text if keep else None)
        statement = insert(Message).values(
            account_id=claim.account_id,
            conversation_id=claim.conversation_id,
            telegram_message_id=telegram_id,
            direction="assistant_sent",
            observed_via="new",
            sent_at=now,
            content_type="text",
            text=content.text,
            search_key=normalize(text) if keep and text else None,
            script=detect_script(text) if keep and text else None,
            lang_guess=guess_lang(text) if keep and text else None,
            content_hash=content.content_hash if keep else None,
            action_id=claim.action_id,
            created_at=now,
            updated_at=now,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["conversation_id", "telegram_message_id"],
                set_={
                    "direction": "assistant_sent",
                    "action_id": statement.excluded.action_id,
                    "updated_at": now,
                },
            )
        )
        previous = conversation.last_assistant_msg_at
        conversation.last_assistant_msg_at = now if previous is None else max(previous, now)
        conversation.availability = "active"
        conversation.updated_at = now
        await session.flush()

    async def _after_outcome(self, claim: _Claim, outcome: ExecuteOutcome) -> None:
        if outcome.status is ExecuteStatus.UNCERTAIN and outcome.reason is StateReason.AMBIGUOUS:
            await self._alert_uncertain(
                _Uncertain(claim.action_id, claim.account_id, claim.conversation_id)
            )
        await self._redefer(claim.action_id, outcome.redefer_at)

    async def _redefer(self, action_id: int, at: datetime | None) -> None:
        if at is None:
            return
        if self._scheduler is None:
            logger.warning("action_redefer_skipped", action_id=action_id)
            return
        # No lock and no queueing lock: Tx A orders it (AGENTS §13); the
        # reconciler re-creates a defer lost here.
        await self._scheduler.defer(EXECUTE_TASK, args={"action_id": action_id}, schedule_at=at)

    def _sync_card(self, session: AsyncSession, action_id: int) -> None:
        if self._presenter is not None:
            self._presenter.sync_after_commit(session, action_id)

    async def _alert_uncertain(self, item: _Uncertain) -> None:
        async with self._sf() as session:
            name = await contact_display_name(session, item.account_id, item.conversation_id)
        await self._alerts.notify_owner(
            item.account_id,
            AlertKind.DELIVERY_UNCERTAIN,
            dedupe_key=UNCERTAIN_ALERT_KEY.format(action_id=item.action_id),
            contact=html.escape(name) if name else UNNAMED_CONTACT,
        )

    # -------------------------------------------------------- mark_uncertain
    async def mark_uncertain(self) -> int:
        """`system.mark_uncertain`: settle attempts stuck in `executing` and
        tell the owner, once per action; then the stale-connection alert."""
        now = self._clock.now()
        async with transaction(self._sf) as uow:
            settled = await mark_stale_attempts(
                uow.session, now=now, older_than=now - self._config.mark_uncertain_after
            )
            for item in settled:
                self._sync_card(uow.session, item.action_id)
            stale = await stale_connections(
                uow.session, older_than=now - self._config.connection_stale_alert_after
            )
        for item in settled:
            logger.warning("action_marked_uncertain", action_id=item.action_id)
            await self._alert_uncertain(item)
        minutes = int(self._config.connection_stale_alert_after.total_seconds() // 60)
        for account_id, since in stale:
            await self._alerts.notify_owner(
                account_id,
                AlertKind.CONNECTION_STALE,
                dedupe_key=STALE_ALERT_KEY.format(
                    account_id=account_id, since=int(since.timestamp())
                ),
                minutes=minutes,
            )
        return len(settled)


async def mark_stale_attempts(
    session: AsyncSession, *, now: datetime, older_than: datetime
) -> list[_Uncertain]:
    """One conditional UPDATE: `executing` attempts started before
    `older_than` become `delivery_uncertain` (`stale_executing`), with the
    same audit row the transition primitive writes (decisions R15)."""
    rows = (
        await session.execute(
            update(Action)
            .where(
                Action.state == ActionState.EXECUTING.value,
                Action.attempt_started_at < older_than,
            )
            .values(
                state=ActionState.DELIVERY_UNCERTAIN.value,
                state_reason=StateReason.STALE_EXECUTING.value,
                updated_at=now,
            )
            .returning(
                Action.id,
                Action.account_id,
                Action.conversation_id,
                Action.version,
                Action.attempt_id,
            )
            .execution_options(synchronize_session=False)
        )
    ).all()
    for action_id, account_id, _conversation_id, version, attempt_id in rows:
        session.add(
            AuditLogEntry(
                account_id=account_id,
                at=now,
                actor="system",
                event="action_delivery_uncertain",
                subject_type="action",
                subject_id=action_id,
                details={
                    "from": ActionState.EXECUTING.value,
                    "to": ActionState.DELIVERY_UNCERTAIN.value,
                    "version": version,
                    "reason": StateReason.STALE_EXECUTING.value,
                    "attempt_id": attempt_id,
                },
            )
        )
    await session.flush()
    return [
        _Uncertain(int(row[0]), int(row[1]), int(row[2]))
        for row in sorted(rows, key=lambda row: int(row[0]))
    ]


_STALE_CONNECTIONS = text(
    """
    SELECT c.account_id, max(a.at) AS since
    FROM app.business_connections AS c
    JOIN app.audit_log AS a
      ON a.account_id = c.account_id
     AND a.event = 'connection_marked_stale'
     AND a.subject_type = 'connection'
     AND a.subject_id = c.id
    WHERE c.status = 'stale'
    GROUP BY c.account_id
    HAVING max(a.at) < :older_than
    ORDER BY c.account_id
    """
)


async def stale_connections(
    session: AsyncSession, *, older_than: datetime
) -> list[tuple[int, datetime]]:
    """`(account_id, stale since)` of connections still stale since before
    `older_than` — the latest marking is the period's start, so the alert's
    dedupe key names one stale period."""
    rows = (await session.execute(_STALE_CONNECTIONS, {"older_than": older_than})).all()
    return [(int(row[0]), row[1]) for row in rows]
