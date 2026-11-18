"""Triage verdicts: what a triage run reads, and what its verdict stores
(ARCHITECTURE §6.5 steps 4-5, §10.3; S05.1, P2-T12).

Domain code only — the model call is `agent/triage.py`. A triage run reads
ONE conversation (§6 invariant 4): the **burst** — the OLDEST `BURST_LIMIT`
retained messages after the review cursor (C7, I2; the context says how many
newer ones wait for a later run) — and up to `EARLIER_LIMIT` messages before it
as context. The cursor (`review_progress`, falling back to a legacy verdict's
`covers_through_message_id`) only ever moves over messages a run examined, so
repeated bounded runs drain a backlog without skipping any retained message.
A chat without a cursor yet has no "already triaged" line: its burst starts at
the messages of the `FIRST_BURST_WINDOW` before its newest message, so switching
triage on never turns a chat's whole history into new alerts and tasks.

Deleted and expired messages are never read: the filter is the summaries'
(`memory/summaries.py`), the one definition of what an AI run may read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from sqlalchemy import ColumnElement, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.accounts.service import CONSENT_AI_MANAGED, active_consent
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.inbox import review_progress, tasks
from assistant.inbox.models import ConversationTriage, ReviewProgress
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import enqueue
from assistant.inbox.tasks import ResolutionEvidence, TaskView
from assistant.inbox.triage_trigger import TRIGGER_DIRECTIONS
from assistant.memory import summaries
from assistant.memory.summaries import SourceMessage
from assistant.memory.summaries import _retained as retained
from assistant.memory.summaries import _source as source_message

#: New messages one triage call reads (the oldest unreviewed, if more arrived).
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
    #: Retained messages after the burst, left for later runs because more than
    #: `BURST_LIMIT` were unreviewed. Never certified by this run.
    remaining: int = 0
    #: The cursor the burst starts after; None before the first review.
    after: int | None = None
    #: `conversations.content_revision` read in the same locked snapshot (C6).
    snapshot_revision: int = 0
    #: The progress row's `version` at the read; None when it did not exist.
    progress_version: int | None = None

    @property
    def through(self) -> int | None:
        """The newest message shown: the cursor and a stored verdict move here,
        and only here. The burst is contiguous from the cursor, so nothing
        retained lies between."""
        return self.burst[-1].message_id if self.burst else None

    @property
    def read_after(self) -> int | None:
        """Where the contiguity claim starts: the cursor, or just before the
        first message of a first review's window."""
        if self.after is not None:
            return self.after
        return self.burst[0].message_id - 1 if self.burst else None

    def hashes(self) -> dict[int, str | None]:
        """Every shown message's content hash, for the store re-check."""
        return {m.message_id: m.content_hash for m in (*self.earlier, *self.burst)}

    def directions(self) -> dict[int, str]:
        return {m.message_id: m.direction for m in (*self.earlier, *self.burst)}


@dataclass(frozen=True, slots=True)
class TriageState:
    """What triage knows of a conversation before a run: how far its verdict
    covers, the contact's priority (FR-TRI-04) and its open agent tasks."""

    #: The review cursor: progress position, else a legacy verdict's coverage.
    covers_through_message_id: int | None
    contact_priority: str
    open_tasks: tuple[TaskView, ...] = ()
    #: `review_progress.version`; None until a review has been settled or failed.
    progress_version: int | None = None
    #: Set while the reader is degraded: no new attempt before this time.
    retry_at: datetime | None = None


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
    #: Open task ids and row revisions captured before the model call.
    task_revisions: Mapping[int, str | None]
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
    resolutions: tuple[ResolutionEvidence, ...] = ()
    require_resolution_evidence: bool = False
    #: Review snapshot (C6/C7): the revision, cursor and progress version the
    #: burst was read at, and whether retained messages remain beyond it.
    snapshot_revision: int = 0
    read_after: int | None = None
    progress_version: int | None = None
    remaining: bool = False


class ApplyResult(StrEnum):
    """What storing a verdict did. Every value but STORED stores nothing."""

    STORED = "stored"
    RACED = "raced"
    """A newer verdict covers as far or further: it stands."""
    CHANGED = "changed"
    """A message or the open task snapshot changed meanwhile."""
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

    The burst is the OLDEST `BURST_LIMIT` unreviewed messages in ingestion
    order, so the cursor can move to its last message without passing a
    retained message nobody read (C7): when more are waiting, `remaining` counts
    them and later runs drain them. "Earlier" messages are context that was
    already reviewed (at or before the cursor), never new ones."""
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
        (await session.execute(newer.order_by(Message.id.asc()).limit(BURST_LIMIT))).scalars().all()
    )
    if not rows:
        return TriageSource(earlier=(), burst=(), after=covers_through)
    remaining = (
        await session.execute(
            select(func.count()).select_from(newer.where(Message.id > rows[-1].id).subquery())
        )
    ).scalar_one()
    burst = tuple(source_message(row) for row in rows)
    before = await session.execute(
        select(Message)
        .where(*retained(account_id, conversation_id))
        .where(Message.id < burst[0].message_id)
        .where(not_(is_new))
        .order_by(Message.id.desc())
        .limit(EARLIER_LIMIT)
    )
    earlier = tuple(source_message(row) for row in reversed(before.scalars().all()))
    return TriageSource(
        earlier=earlier, burst=burst, remaining=int(remaining), after=covers_through
    )


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
        alerts.append(_Alert(NotificationKind.TRIAGE_NEEDS_REPLY, Priority(verdict.urgency)))
    if verdict.escalation_category == "suspicious":
        priority = (
            Priority(verdict.urgency) if verdict.urgency in ("high", "urgent") else Priority.NORMAL
        )
        alerts.append(_Alert(NotificationKind.TRIAGE_SUSPICIOUS, priority))
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
        verdict_covers = await session.scalar(
            select(ConversationTriage.covers_through_message_id)
            .where(ConversationTriage.account_id == account_id)
            .where(ConversationTriage.conversation_id == conversation_id)
        )
        progress = await review_progress.get(
            session, account_id=account_id, conversation_id=conversation_id
        )
        position = review_progress.position_of(progress)
        # The cursor is the progress position; a legacy verdict without one
        # keeps its own coverage until the next settlement creates the row.
        covers = verdict_covers if position is None else position.message_id
        open_tasks = await tasks.open_for_conversation(
            session, account_id=account_id, conversation_id=conversation_id
        )
        return TriageState(
            covers,
            str(row[0]),
            tuple(open_tasks),
            progress_version=None if progress is None else progress.version,
            retry_at=(
                progress.retry_at
                if progress is not None and progress.status == "degraded"
                else None
            ),
        )

    async def settle(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        conversation_id: int,
        source: TriageSource,
        covers_through: int | None,
        now: datetime,
    ) -> ApplyResult:
        """No verdict to store: the burst held nothing a verdict could say. The
        cursor still moves over it (so a backlog of such messages drains instead
        of being re-read forever), but only after the same source check as a
        stored verdict, and the flag follows the §3.8 rule."""
        locked = await session.scalar(
            select(Account.id).where(Account.id == account_id).with_for_update(read=True)
        )
        conversation = await summaries.lock_conversation(session, account_id, conversation_id)
        if locked is None or conversation is None:
            return ApplyResult.CHANGED
        through = source.through
        if through is None:  # nothing new: the flag follows what was read
            await _settle_flag(session, conversation, covers_through)
            return ApplyResult.STORED
        is_bot = await session.scalar(
            select(Contact.is_bot).where(Contact.id == conversation.contact_id)
        )
        if is_bot or conversation.mode == "ignore":
            await _settle_flag(session, conversation, through)
            return ApplyResult.BOT_CHAT if is_bot else ApplyResult.IGNORED
        assert source.read_after is not None
        stale = await _stale(
            session,
            conversation,
            shown=source.hashes(),
            burst_ids=tuple(m.message_id for m in source.burst),
            after=source.read_after,
            through=through,
            snapshot_revision=source.snapshot_revision,
        )
        if stale is not None:
            return stale
        refused, _ = await _advance(
            session,
            conversation,
            after=source.read_after,
            through=through,
            expected_version=source.progress_version,
            snapshot_revision=source.snapshot_revision,
            backlog=source.remaining > 0,
            now=now,
        )
        if refused is not None:
            return refused
        await _settle_flag(session, conversation, through)
        return ApplyResult.STORED

    async def fail(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        conversation_id: int,
        source: TriageSource,
        error_class: str,
        retry_after: timedelta,
        now: datetime,
    ) -> None:
        """A run on this burst failed (refusal, invalid output, provider error):
        count the attempt. The cursor does not move; after `MAX_ATTEMPTS` the
        review is degraded and the batch stays recoverable pending work."""
        if source.through is None:
            return
        locked = await session.scalar(
            select(Account.id).where(Account.id == account_id).with_for_update(read=True)
        )
        conversation = await summaries.lock_conversation(session, account_id, conversation_id)
        if locked is None or conversation is None:
            return
        reason, last_error = _FAILURES.get(error_class, ("provider_failed", "provider_failed"))
        await review_progress.record_failure(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            first_message_id=source.burst[0].message_id,
            last_message_id=source.through,
            reason=reason,
            last_error=last_error,
            retry_after=retry_after,
            now=now,
        )

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
        first_shown = min(verdict.burst_message_ids, default=verdict.covers_through_message_id)
        after = verdict.read_after if verdict.read_after is not None else first_shown - 1
        stale = await _stale(
            session,
            conversation,
            shown=verdict.source,
            burst_ids=verdict.burst_message_ids,
            after=after,
            through=verdict.covers_through_message_id,
            snapshot_revision=verdict.snapshot_revision,
        )
        if stale is not None:
            return stale
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
        # Owner task changes take the same conversation lock. Re-read after
        # acquiring it so a verdict cannot overwrite a correction or its Undo,
        # including through the implicit first-source-message dedupe path.
        open_tasks = await tasks.open_for_conversation(
            session, account_id=account_id, conversation_id=conversation_id
        )
        if {task.task_id: task.revision for task in open_tasks} != verdict.task_revisions:
            return ApplyResult.CHANGED
        # Every check passed: only now does the cursor move (CAS on the version
        # the burst was read at), so a refused verdict never advances coverage.
        refused, progress = await _advance(
            session,
            conversation,
            after=after,
            through=verdict.covers_through_message_id,
            expected_version=verdict.progress_version,
            snapshot_revision=verdict.snapshot_revision,
            backlog=verdict.remaining,
            now=now,
        )
        if refused is not None:
            return refused
        assert progress is not None
        self._store_verdict(session, current, verdict, now, progress)

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
        closed: list[int] = []
        if verdict.require_resolution_evidence:
            for resolution in verdict.resolutions:
                if await tasks.close_with_evidence(
                    session,
                    account_id=account_id,
                    conversation_id=conversation_id,
                    resolution=resolution,
                    shown_new_ids=verdict.burst_message_ids,
                    suspicious=verdict.escalation_category == "suspicious",
                    clock=self._clock,
                ):
                    closed.append(resolution.task_id)
        else:
            for task_id in verdict.resolved_task_ids:
                if await tasks.close(
                    session,
                    account_id=account_id,
                    task_id=task_id,
                    reason=tasks.RESOLVED_BY_TRIAGE,
                    clock=self._clock,
                ):
                    closed.append(task_id)
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
                    task_ids=tuple(created),
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
            and await _contact_wrote(session, account_id, verdict.burst_message_ids)
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
        progress: ReviewProgress,
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
            "review_progress_id": progress.id,
            "review_version": progress.version,
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


_FAILURES: dict[str, tuple[Literal["provider_failed", "invalid_output"], str]] = {
    "invalid_output": ("invalid_output", "invalid_output"),
    "refusal": ("provider_failed", "refusal"),
    "content_filter": ("provider_failed", "refusal"),
    "max_tokens": ("provider_failed", "truncated"),
}


async def _stale(
    session: AsyncSession,
    conversation: Conversation,
    *,
    shown: Mapping[int, str | None],
    burst_ids: Collection[int],
    after: int,
    through: int,
    snapshot_revision: int,
) -> ApplyResult | None:
    """None when what the call read is still what is there (under the
    conversation lock); otherwise why not. C7: a changed source is discarded,
    never settled as current."""
    state = await review_progress.source_state(
        session,
        account_id=conversation.account_id,
        conversation_id=conversation.id,
        shown=dict(shown),
        contiguous_ids=burst_ids,
        after=after,
        through=through,
        snapshot_revision=snapshot_revision,
        current_revision=conversation.content_revision,
    )
    if state is review_progress.SourceState.EXPIRED:
        return ApplyResult.EXPIRED
    if state is review_progress.SourceState.CHANGED:
        return ApplyResult.CHANGED
    return None


async def _advance(
    session: AsyncSession,
    conversation: Conversation,
    *,
    after: int,
    through: int,
    expected_version: int | None,
    snapshot_revision: int,
    backlog: bool,
    now: datetime,
) -> tuple[ApplyResult | None, ReviewProgress | None]:
    """Move the cursor to `through` (compare-and-swap on the version the burst
    was read at); a result when it did not move, nothing written then."""
    lost = await review_progress.lost_ranges(
        session,
        account_id=conversation.account_id,
        conversation_id=conversation.id,
        after=after,
        through=through,
    )
    settlement, row = await review_progress.advance(
        session,
        account_id=conversation.account_id,
        conversation_id=conversation.id,
        expected_version=expected_version,
        through=through,
        snapshot_revision=snapshot_revision,
        current_revision=conversation.content_revision,
        lost=lost,
        backlog=backlog,
        now=now,
    )
    if settlement is review_progress.Settlement.APPLIED:
        return None, row
    if settlement is review_progress.Settlement.RACED:
        return ApplyResult.RACED, None
    return ApplyResult.CHANGED, None


async def _contact_wrote(
    session: AsyncSession, account_id: int, burst_message_ids: Collection[int]
) -> bool:
    """Owner messages never trigger drafts (RG-01): a burst of only the
    owner's own answer is triaged for task closure, never drafted for."""
    if not burst_message_ids:
        return False
    found = await session.scalar(
        select(Message.id)
        .where(Message.account_id == account_id)
        .where(Message.id.in_(burst_message_ids))
        .where(Message.direction == "contact")
        .limit(1)
    )
    return found is not None


async def _settle_flag(
    session: AsyncSession, conversation: Conversation, through: int | None
) -> None:
    """Clear `pending_triage_at` when nothing a run could read arrived after
    `through`; otherwise move it to the first such message (the summaries'
    rule: the wait counts from what triage has not seen).

    Only retained messages from a direction that sets the flag count: a
    deleted or content-less message (a location, a poll) is never shown, so
    it would keep the flag forever and the reconciler would re-create the job
    every minute (Viro on #92); an automatic message is read with the next
    burst, as when it arrives outside a run."""
    if conversation.pending_triage_at is None:
        return
    if through is None:  # nothing was ever there to triage
        conversation.pending_triage_at = None
        return
    conversation.pending_triage_at = await session.scalar(
        select(func.min(Message.created_at))
        .where(*retained(conversation.account_id, conversation.id))
        .where(Message.direction.in_([d.value for d in TRIGGER_DIRECTIONS]))
        .where(Message.id > through)
    )
