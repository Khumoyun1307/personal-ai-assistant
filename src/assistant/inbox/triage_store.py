"""Triage verdicts: what a triage run reads, and what its verdict stores
(ARCHITECTURE §6.5 steps 4-5, §10.3; S05.1, P2-T12).

Domain code only — the model call is `agent/triage.py`. A triage run reads
ONE conversation (§6 invariant 4): the **burst** — that conversation's
retained messages newer than its current verdict's
`covers_through_message_id` (the newest `BURST_LIMIT`; the context says when
older ones were left out) — and up to `EARLIER_LIMIT` messages before it as
context. A chat without a verdict yet has no "already triaged" line: its
burst is the messages of the `FIRST_BURST_WINDOW` before its newest message,
so switching triage on never turns a chat's whole history into new alerts
and tasks.

Deleted and expired messages are never read: the filter is the summaries'
(`memory/summaries.py`), the one definition of what an AI run may read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.accounts.service import CONSENT_AI_MANAGED, active_consent
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.inbox import tasks
from assistant.inbox.models import ConversationTriage
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import enqueue
from assistant.inbox.tasks import TaskView
from assistant.memory import summaries
from assistant.memory.summaries import SourceMessage, StoreResult
from assistant.memory.summaries import _changed_source as changed_source
from assistant.memory.summaries import _retained as retained
from assistant.memory.summaries import _source as source_message

#: New messages one triage call reads (the newest, if more arrived).
BURST_LIMIT = 60
#: Earlier messages shown before the burst, for context.
EARLIER_LIMIT = 20
#: Without a verdict, the burst reaches back this far from the newest message.
FIRST_BURST_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class TriageSource:
    """What one triage call reads, oldest first."""

    earlier: tuple[SourceMessage, ...]
    burst: tuple[SourceMessage, ...]
    #: Burst messages left out because more than `BURST_LIMIT` arrived.
    omitted: int = 0

    @property
    def through(self) -> int | None:
        """The newest message shown: what a stored verdict covers."""
        return self.burst[-1].message_id if self.burst else None

    def hashes(self) -> dict[int, str | None]:
        """Every shown message's content hash, for the store re-check."""
        return {m.message_id: m.content_hash for m in (*self.earlier, *self.burst)}

    def directions(self) -> dict[int, str]:
        return {m.message_id: m.direction for m in (*self.earlier, *self.burst)}


@dataclass(frozen=True, slots=True)
class TriageState:
    """What triage knows of a conversation before a run: how far its verdict
    covers, the contact's priority (FR-TRI-04) and its open agent tasks."""

    covers_through_message_id: int | None
    contact_priority: str
    open_tasks: tuple[TaskView, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    """A task a verdict creates, or updates when `existing_task_id` is set."""

    kind: str
    title: str
    due_at: datetime | None
    source_message_id: int
    existing_task_id: int | None


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    """A validated verdict, as the effects transaction stores it: ids, enums,
    times, and the short reasons and titles (content columns)."""

    account_id: int
    conversation_id: int
    run_id: int
    prompt_version: str
    model: str
    #: Every message the model read → its content hash (the store re-check).
    source: Mapping[int, str | None]
    covers_through_message_id: int
    burst_message_ids: tuple[int, ...]
    needs_reply: bool
    contains_question: bool
    requests_decision: bool
    urgency: str
    deadline_at: datetime | None
    escalation_category: str
    reasons: tuple[str, ...]
    language: str | None
    script: str | None
    tasks: tuple[TaskCandidate, ...]
    resolved_task_ids: tuple[int, ...]


class ApplyResult(StrEnum):
    """What storing a verdict did. Every value but STORED stores nothing."""

    STORED = "stored"
    RACED = "raced"
    """A newer verdict covers as far or further: it stands."""
    CHANGED = "changed"
    """A message the model read was deleted or edited meanwhile."""
    EXPIRED = "expired"
    """A message the model read expired under the retention setting."""
    IGNORED = "ignored"
    """The chat was set to Ignore mode."""
    BOT_CHAT = "bot_chat"
    """The chat turned out to be a chat with a bot."""
    NO_CONSENT = "no_consent"
    """`ai_managed` was revoked while the model ran."""

    @property
    def discarded(self) -> bool:
        return self not in (ApplyResult.STORED, ApplyResult.RACED)


async def burst_source(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    covers_through: int | None,
) -> TriageSource:
    """The burst after `covers_through` and the messages before it (§3.2).
    Account-scoped; one conversation; retained messages only.

    "Earlier" messages are never new ones: when more than `BURST_LIMIT`
    arrived, the left-out new messages are counted in `omitted`, not shown
    as context (they were never triaged)."""
    is_new: ColumnElement[bool]
    if covers_through is not None:
        is_new = Message.id > covers_through
    else:
        newest = (
            await session.execute(
                select(func.max(Message.sent_at)).where(*retained(account_id, conversation_id))
            )
        ).scalar_one_or_none()
        if newest is None:
            return TriageSource(earlier=(), burst=())
        is_new = Message.sent_at >= newest - FIRST_BURST_WINDOW
    newer = select(Message).where(*retained(account_id, conversation_id)).where(is_new)
    rows = list(
        (await session.execute(newer.order_by(Message.id.desc()).limit(BURST_LIMIT + 1)))
        .scalars()
        .all()
    )
    if not rows:
        return TriageSource(earlier=(), burst=())
    omitted = 0
    if len(rows) > BURST_LIMIT:
        rows = rows[:BURST_LIMIT]
        omitted = (
            await session.execute(
                select(func.count()).select_from(newer.where(Message.id < rows[-1].id).subquery())
            )
        ).scalar_one()
    burst = tuple(source_message(row) for row in reversed(rows))
    before = await session.execute(
        select(Message)
        .where(*retained(account_id, conversation_id))
        .where(Message.id < burst[0].message_id)
        .where(not_(is_new))
        .order_by(Message.id.desc())
        .limit(EARLIER_LIMIT)
    )
    earlier = tuple(source_message(row) for row in reversed(before.scalars().all()))
    return TriageSource(earlier=earlier, burst=burst, omitted=int(omitted))


# ------------------------------------------------------------------ the verdict
@dataclass(frozen=True, slots=True)
class TriageView:
    """A conversation's latest verdict as the attention list and the digest
    read it. `reasons` are model-written from contact text: shown wrapped."""

    conversation_id: int
    covers_through_message_id: int
    needs_reply: bool
    contains_question: bool
    requests_decision: bool
    urgency: str
    deadline_at: datetime | None
    escalation_category: str
    reasons: tuple[str, ...]
    updated_at: datetime


async def latest(
    session: AsyncSession, *, account_id: int, conversation_ids: Collection[int]
) -> dict[int, TriageView]:
    """The current verdict of each of these conversations that has one."""
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(ConversationTriage)
        .where(ConversationTriage.account_id == account_id)
        .where(ConversationTriage.conversation_id.in_(sorted(conversation_ids)))
    )
    return {
        row.conversation_id: TriageView(
            conversation_id=row.conversation_id,
            covers_through_message_id=row.covers_through_message_id,
            needs_reply=row.needs_reply,
            contains_question=row.contains_question,
            requests_decision=row.requests_decision,
            urgency=row.urgency,
            deadline_at=row.deadline_at,
            escalation_category=row.escalation_category,
            reasons=tuple(r for r in row.reasons or () if isinstance(r, str)),
            updated_at=row.updated_at,
        )
        for row in rows.scalars()
    }


#: Asks for an Assist draft of one conversation in the caller's transaction
#: (`actions.drafts.request_draft`, S04.2). Injected, so this package does not
#: depend on M04 code; None until it is wired (CP4), and only ever called
#: while the Assist trigger switch is "triage" (P2-O6, S05.1 §3.7).
DraftRequester = Callable[[AsyncSession, int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Alert:
    kind: NotificationKind
    priority: Priority


def verdict_alerts(verdict: TriageVerdict) -> tuple[_Alert, ...]:
    """What a verdict tells the owner (S05.1 §3.5 step 6): at most one of
    urgent / decision / needs reply, plus a suspicious-burst alert. Nothing
    for an FYI or a quiet verdict that needs no reply."""
    alerts: list[_Alert] = []
    if verdict.escalation_category == "urgent_decision" or verdict.urgency == "urgent":
        alerts.append(_Alert(NotificationKind.TRIAGE_URGENT, Priority.URGENT))
    elif verdict.requests_decision:
        alerts.append(_Alert(NotificationKind.TRIAGE_DECISION, Priority.HIGH))
    elif verdict.needs_reply:
        alerts.append(_Alert(NotificationKind.TRIAGE_NEEDS_REPLY, Priority.NORMAL))
    if verdict.escalation_category == "suspicious":
        alerts.append(_Alert(NotificationKind.TRIAGE_SUSPICIOUS, Priority.NORMAL))
    return tuple(alerts)


class TriageRepository:
    """The production `TriageStore` (agent/triage.py): verdict state in, the
    effects transaction out (S05.1 §3.5). No model call, no Telegram call;
    alerts are enqueued (S05.2), deferred after the commit."""

    def __init__(
        self,
        *,
        clock: Clock,
        scheduler: JobScheduler | None,
        assist_draft_trigger: str = "off",
        draft_requester: DraftRequester | None = None,
    ) -> None:
        self._clock = clock
        self._scheduler = scheduler
        self._assist_trigger = assist_draft_trigger
        self._draft_requester = draft_requester

    async def state(
        self, session: AsyncSession, *, account_id: int, conversation_id: int
    ) -> TriageState | None:
        row = (
            await session.execute(
                select(Contact.priority)
                .join(Conversation, Conversation.contact_id == Contact.id)
                .where(Conversation.account_id == account_id)
                .where(Conversation.id == conversation_id)
            )
        ).first()
        if row is None:
            return None
        covers = await session.scalar(
            select(ConversationTriage.covers_through_message_id)
            .where(ConversationTriage.account_id == account_id)
            .where(ConversationTriage.conversation_id == conversation_id)
        )
        open_tasks = await tasks.open_for_conversation(
            session, account_id=account_id, conversation_id=conversation_id
        )
        return TriageState(covers, str(row[0]), tuple(open_tasks))

    async def settle(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        conversation_id: int,
        through: int | None,
    ) -> None:
        conversation = await summaries.lock_conversation(session, account_id, conversation_id)
        if conversation is not None:
            await _settle_flag(session, conversation, through)

    async def apply(
        self, session: AsyncSession, verdict: TriageVerdict, *, now: datetime
    ) -> ApplyResult:
        """Store a verdict and its effects in the caller's transaction, after
        the §9.4 locks and a fresh re-check of everything the model read; or
        store nothing and say why."""
        account_id, conversation_id = verdict.account_id, verdict.conversation_id
        locked = await session.scalar(
            select(Account.id).where(Account.id == account_id).with_for_update(read=True)
        )
        conversation = await summaries.lock_conversation(session, account_id, conversation_id)
        if locked is None or conversation is None:
            return ApplyResult.CHANGED
        is_bot = await session.scalar(
            select(Contact.is_bot).where(Contact.id == conversation.contact_id)
        )
        if is_bot:
            return ApplyResult.BOT_CHAT
        if conversation.mode == "ignore":
            return ApplyResult.IGNORED
        if not (await active_consent(session, account_id, kind=CONSENT_AI_MANAGED)).granted:
            return ApplyResult.NO_CONSENT
        changed = await changed_source(session, account_id, conversation_id, verdict.source)
        if changed is not None:
            return ApplyResult.EXPIRED if changed is StoreResult.EXPIRED else ApplyResult.CHANGED
        current = (
            await session.execute(
                select(ConversationTriage)
                .where(ConversationTriage.account_id == account_id)
                .where(ConversationTriage.conversation_id == conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            current is not None
            and current.covers_through_message_id >= verdict.covers_through_message_id
        ):
            return ApplyResult.RACED
        self._store_verdict(session, current, verdict, now)

        created: list[int] = []
        for candidate in verdict.tasks:
            task_id, inserted = await tasks.upsert_from_candidate(
                session,
                account_id=account_id,
                conversation_id=conversation_id,
                kind=candidate.kind,
                title=candidate.title,
                due_at=candidate.due_at,
                source_message_ids=(candidate.source_message_id,),
                run_id=verdict.run_id,
                existing_task_id=candidate.existing_task_id,
                clock=self._clock,
            )
            if inserted:
                created.append(task_id)
        closed = [
            task_id
            for task_id in verdict.resolved_task_ids
            if await tasks.close(
                session,
                account_id=account_id,
                task_id=task_id,
                reason=tasks.RESOLVED_BY_TRIAGE,
                clock=self._clock,
            )
        ]
        contact_id = conversation.contact_id
        for task_id in closed:
            await enqueue(
                session,
                account_id=account_id,
                kind=NotificationKind.TASK_CLOSED,
                priority=Priority.LOW,
                dedupe_key=f"task_closed:{task_id}",
                refs=NotificationRefs(
                    conversation_ids=(conversation_id,),
                    contact_ids=(contact_id,),
                    task_ids=(task_id,),
                ),
                clock=self._clock,
                conversation_id=conversation_id,
                contact_id=contact_id,
                scheduler=self._scheduler,
            )
        alerts = verdict_alerts(verdict)
        for alert in alerts:
            await enqueue(
                session,
                account_id=account_id,
                kind=alert.kind,
                priority=alert.priority,
                dedupe_key=(
                    f"triage:{conversation_id}:{verdict.covers_through_message_id}:{alert.kind}"
                ),
                refs=NotificationRefs(
                    conversation_ids=(conversation_id,),
                    contact_ids=(contact_id,),
                    message_ids=verdict.burst_message_ids,
                    task_ids=(*created, *closed),
                ),
                clock=self._clock,
                conversation_id=conversation_id,
                contact_id=contact_id,
                scheduler=self._scheduler,
            )
        if (
            self._assist_trigger == "triage"
            and self._draft_requester is not None
            and conversation.mode == "assist"
            and verdict.needs_reply
            and verdict.escalation_category != "suspicious"
        ):
            await self._draft_requester(session, account_id, conversation_id)
        await _settle_flag(session, conversation, verdict.covers_through_message_id)
        session.add(
            AuditLogEntry(
                account_id=account_id,
                at=now,
                actor="agent",
                event=EVENT_TRIAGE_COMPLETED,
                subject_type="conversation",
                subject_id=conversation_id,
                details={
                    "run_id": verdict.run_id,
                    "covers_through": verdict.covers_through_message_id,
                    "tasks_created": len(created),
                    "tasks_closed": len(closed),
                    "alerts": [str(alert.kind) for alert in alerts],
                },
            )
        )
        await session.flush()
        return ApplyResult.STORED

    @staticmethod
    def _store_verdict(
        session: AsyncSession,
        current: ConversationTriage | None,
        verdict: TriageVerdict,
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {
            "covers_through_message_id": verdict.covers_through_message_id,
            "needs_reply": verdict.needs_reply,
            "contains_question": verdict.contains_question,
            "requests_decision": verdict.requests_decision,
            "urgency": verdict.urgency,
            "deadline_at": verdict.deadline_at,
            "escalation_category": verdict.escalation_category,
            "reasons": list(verdict.reasons),
            "language": verdict.language,
            "script": verdict.script,
            "prompt_version": verdict.prompt_version,
            "model": verdict.model,
            "run_id": verdict.run_id,
            "updated_at": now,
        }
        if current is None:
            session.add(
                ConversationTriage(
                    account_id=verdict.account_id,
                    conversation_id=verdict.conversation_id,
                    created_at=now,
                    **values,
                )
            )
        else:
            for key, value in values.items():
                setattr(current, key, value)


EVENT_TRIAGE_COMPLETED = "triage_completed"


async def _settle_flag(
    session: AsyncSession, conversation: Conversation, through: int | None
) -> None:
    """Clear `pending_triage_at` when nothing arrived after `through`;
    otherwise move it to the first message after it (the summaries' rule:
    the wait counts from what triage has not seen)."""
    if conversation.pending_triage_at is None:
        return
    if through is None:  # nothing was ever there to triage
        conversation.pending_triage_at = None
        return
    conversation.pending_triage_at = await session.scalar(
        select(func.min(Message.created_at))
        .where(Message.account_id == conversation.account_id)
        .where(Message.conversation_id == conversation.id)
        .where(Message.id > through)
    )
