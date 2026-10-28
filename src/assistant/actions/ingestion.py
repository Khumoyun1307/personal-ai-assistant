"""`ActionsIngestionListener`: what a stored message does to drafts (S04.2 §3.8).

Runs inside the ingesting transaction, after `record_message` /
`record_deletion`, with the conversation row already locked (F0-interfaces
§5). It only writes through the given session and registers after-commit
work: the debounced `replies.draft` defer and control-chat card updates
(decisions R16) — never a business-chat call, never a model call, never a
commit. Everything is idempotent: a requeued update is a `DUPLICATE`
(nothing new) or finds the drafts already outdated (the bulk transitions
are conditional on the state).

- Ignore mode (`metadata_only`): nothing (switching to Ignore cancelled
  everything).
- A new contact message: live cards → `outdated`/`new_contact_message` (an
  approved one is stopped by `not_stale` at execution); in Assist with the
  ingestion trigger, `request_draft(ASSIST_INGESTION)`.
- A new owner_manual message: live and approved → `outdated`/`takeover` (the
  pause itself is `record_message` step 4).
- An owner_manual edit that changed content: live and approved →
  `outdated`/`owner_edit`; no pause (ARCHITECTURE §6.5 step 4).
- A contact edit of an action's base message: audit
  `source_edited_after_action` per action; unsent ones →
  `outdated`/`source_edited`.
- A deletion of an action's base message: unsent ones →
  `outdated`/`source_deleted`.
- A deletion of assistant_sent messages: audit `assistant_message_deleted`
  per message, never saying who deleted (TF LV-07, P2-T22).
- owner_automated, assistant_sent: nothing.

Deletions list only messages that still held content (`DeletionChange`): a
base message whose content was already gone is caught at execution instead
(`Freshness.base_message_present`), and a requeued deletion changes nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.config import ActionsConfig
from assistant.actions.drafts import request_draft
from assistant.actions.lifecycle import outdate_pending, transition
from assistant.actions.models import Action
from assistant.actions.ports import ApprovalCardPresenter
from assistant.actions.states import LIVE_CARD, ActionState, StateReason
from assistant.conversations.events import Direction
from assistant.conversations.listeners import DeletionChange, MessageChange
from assistant.conversations.models import Message
from assistant.core.clock import Clock
from assistant.core.jobs import DraftReason, JobScheduler
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

MODE_ASSIST = "assist"
EVENT_SOURCE_EDITED = "source_edited_after_action"
EVENT_SENT_DELETED = "assistant_message_deleted"
#: Base-message changes outdate live cards and unclaimed approvals;
#: `executing`, `delivery_uncertain` and terminal rows are never touched.
_BASE_OUTDATABLE = LIVE_CARD | {ActionState.APPROVED}


class ActionsIngestionListener:
    """The M04 listener, registered first in `_wire_m04` (F0-interfaces §5)."""

    def __init__(
        self,
        *,
        config: ActionsConfig,
        scheduler: JobScheduler,
        clock: Clock,
        presenter: ApprovalCardPresenter | None = None,
    ) -> None:
        self._config = config
        self._scheduler = scheduler
        self._clock = clock
        self._presenter = presenter

    async def on_message(self, session: AsyncSession, change: MessageChange) -> None:
        if change.metadata_only:
            return
        now = self._clock.now()
        affected: list[int] = []
        direction = change.direction
        if change.new_message and direction is Direction.CONTACT:
            affected = await self._outdate(
                session, change, StateReason.NEW_CONTACT_MESSAGE, include_approved=False, now=now
            )
            if change.mode == MODE_ASSIST and self._config.assist_draft_trigger == "ingestion":
                await request_draft(
                    session,
                    account_id=change.account_id,
                    conversation_id=change.conversation_id,
                    reason=DraftReason.ASSIST_INGESTION,
                    scheduler=self._scheduler,
                    clock=self._clock,
                    debounce=self._config.draft_debounce,
                )
        elif change.new_message and direction is Direction.OWNER_MANUAL:
            affected = await self._outdate(
                session, change, StateReason.TAKEOVER, include_approved=True, now=now
            )
        elif _content_edit(change) and direction is Direction.OWNER_MANUAL:
            affected = await self._outdate(
                session, change, StateReason.OWNER_EDIT, include_approved=True, now=now
            )
        elif _content_edit(change) and direction is Direction.CONTACT:
            affected = await self._base_edited(session, change, now=now)
        self._sync(session, affected)

    async def on_deletion(self, session: AsyncSession, change: DeletionChange) -> None:
        if change.metadata_only:
            return
        now = self._clock.now()
        affected: list[int] = []
        if change.message_ids:
            affected = await _outdate_based_on(
                session,
                account_id=change.account_id,
                conversation_id=change.conversation_id,
                message_ids=change.message_ids,
                reason=StateReason.SOURCE_DELETED,
                now=now,
            )
        if change.assistant_sent_ids:
            await self._sent_deleted(session, change, now=now)
        self._sync(session, affected)

    async def _outdate(
        self,
        session: AsyncSession,
        change: MessageChange,
        reason: StateReason,
        *,
        include_approved: bool,
        now: datetime,
    ) -> list[int]:
        return await outdate_pending(
            session,
            account_id=change.account_id,
            conversation_id=change.conversation_id,
            reason=reason,
            include_approved=include_approved,
            now=now,
        )

    async def _base_edited(
        self, session: AsyncSession, change: MessageChange, *, now: datetime
    ) -> list[int]:
        if change.message_id is None:
            return []
        based = (
            await session.scalars(
                select(Action.id)
                .where(
                    Action.account_id == change.account_id,
                    Action.conversation_id == change.conversation_id,
                    Action.base_message_id == change.message_id,
                )
                .order_by(Action.id)
            )
        ).all()
        for action_id in based:
            session.add(
                AuditLogEntry(
                    account_id=change.account_id,
                    at=now,
                    actor="system",
                    event=EVENT_SOURCE_EDITED,
                    subject_type="action",
                    subject_id=action_id,
                    details={"message_id": change.message_id},
                )
            )
        if not based:
            return []
        return await _outdate_based_on(
            session,
            account_id=change.account_id,
            conversation_id=change.conversation_id,
            message_ids=(change.message_id,),
            reason=StateReason.SOURCE_EDITED,
            now=now,
        )

    async def _sent_deleted(
        self, session: AsyncSession, change: DeletionChange, *, now: datetime
    ) -> None:
        rows = (
            await session.execute(
                select(Message.id, Message.action_id)
                .where(
                    Message.account_id == change.account_id,
                    Message.conversation_id == change.conversation_id,
                    Message.id.in_(change.assistant_sent_ids),
                )
                .order_by(Message.id)
            )
        ).all()
        for message_id, action_id in rows:
            session.add(
                AuditLogEntry(
                    account_id=change.account_id,
                    at=now,
                    actor="system",
                    event=EVENT_SENT_DELETED,
                    subject_type="conversation",
                    subject_id=change.conversation_id,
                    details={"message_id": message_id, "action_id": action_id},
                )
            )
        await session.flush()

    def _sync(self, session: AsyncSession, action_ids: Sequence[int]) -> None:
        if not action_ids:
            return
        logger.info("actions_outdated_by_ingestion", count=len(action_ids))
        if self._presenter is None:
            return  # S04.3's periodic card sync is the safety net
        for action_id in action_ids:
            self._presenter.sync_after_commit(session, action_id)


def _content_edit(change: MessageChange) -> bool:
    return change.is_edit and change.content_changed


async def _outdate_based_on(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    message_ids: Sequence[int],
    reason: StateReason,
    now: datetime,
) -> list[int]:
    """Outdate the not-yet-sent actions drafted from these messages, each
    through S04.1's conditional `transition` (one audit row per move)."""
    rows = (
        await session.execute(
            select(Action.id, Action.state, Action.version)
            .where(
                Action.account_id == account_id,
                Action.conversation_id == conversation_id,
                Action.base_message_id.in_(list(message_ids)),
                Action.state.in_([state.value for state in _BASE_OUTDATABLE]),
            )
            .order_by(Action.id)
        )
    ).all()
    moved: list[int] = []
    for action_id, state, version in rows:
        if await transition(
            session,
            account_id=account_id,
            action_id=action_id,
            from_state=ActionState(state),
            to_state=ActionState.OUTDATED,
            reason=reason,
            now=now,
            expected_version=version,
        ):
            moved.append(int(action_id))
    return moved
