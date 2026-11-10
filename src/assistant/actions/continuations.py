"""Account-local control artifact selection. No text matching or authority."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry, ControlTurn, PendingInput
from assistant.actions.models import Action
from assistant.actions.request_models import TERMINAL_REQUEST_STATES, OwnerRequest
from assistant.actions.requests import Continuation, SourceUnavailable, load_request, load_source
from assistant.conversations.models import Contact, Conversation


def _ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if type(item) is int and 0 < item <= 2**63 - 1)


async def _request(
    session: AsyncSession, *, account_id: int, rows: list[OwnerRequest], now: datetime
) -> Continuation:
    if not rows:
        return Continuation("unavailable", code="not_available")
    if len(rows) > 1:
        return Continuation("ambiguous", request_ids=tuple(row.id for row in rows))
    row = rows[0]
    if row.state in TERMINAL_REQUEST_STATES or row.expires_at <= now:
        return Continuation("unavailable", code="not_available")
    view = await load_request(session, account_id=account_id, request_id=row.id)
    if view is None:
        return Continuation("unavailable", code="not_available")
    if row.conversation_id is not None:
        conversation = await session.scalar(
            select(Conversation.id)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(
                Conversation.account_id == account_id,
                Conversation.id == row.conversation_id,
                Contact.account_id == account_id,
                Contact.id == row.contact_id,
            )
        )
        if conversation is None:
            return Continuation("unavailable", code="not_available")
    for source in view.sources:
        if isinstance(
            await load_source(session, account_id=account_id, source=source), SourceUnavailable
        ):
            return Continuation("unavailable", code="source_unavailable")
    if row.action_id is not None:
        action_version = select(Action.version).where(
            Action.account_id == account_id, Action.id == row.action_id
        )
        if row.conversation_id is not None:
            action_version = action_version.where(Action.conversation_id == row.conversation_id)
        version = await session.scalar(action_version)
        if version != row.action_version:
            return Continuation("unavailable", code="stale")
    return Continuation(
        "selected",
        request_id=row.id,
        version=row.version,
        action_id=row.action_id,
        action_version=row.action_version,
    )


async def _action(
    session: AsyncSession, *, account_id: int, action_id: int, version: int | None, now: datetime
) -> Continuation:
    action = await session.scalar(
        select(Action)
        .join(Conversation, Conversation.id == Action.conversation_id)
        .where(
            Action.account_id == account_id,
            Action.id == action_id,
            Conversation.account_id == account_id,
        )
        .execution_options(populate_existing=True)
    )
    if action is None:
        return Continuation("unavailable", code="not_available")
    if version is None or action.version != version:
        return Continuation("unavailable", code="stale")
    rows = list(
        await session.scalars(
            select(OwnerRequest).where(
                OwnerRequest.account_id == account_id, OwnerRequest.action_id == action.id
            )
        )
    )
    if rows:
        return await _request(session, account_id=account_id, rows=rows, now=now)
    # Pre-C1 cards can be explicitly selected without inventing a request
    # or importing the card body into a new recipient's task.
    return Continuation("selected", action_id=action.id, action_version=version)


async def _explicit(
    session: AsyncSession, *, account_id: int, turn_id: int, message_id: int, now: datetime
) -> Continuation:
    # Successful presentation mappings beat a mutable action pointer. An
    # in-place edit's latest mapping wins; replaced messages stay versioned.
    mapping = await session.scalar(
        select(AuditLogEntry)
        .where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.event == "card_presented",
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.details["control_message_id"].as_integer() == message_id,
        )
        .order_by(AuditLogEntry.id.desc())
        .limit(1)
    )
    if mapping is not None:
        version = mapping.details.get("version")
        if type(version) is not int or version < 1 or mapping.subject_id is None:
            return Continuation("unavailable", code="not_available")
        return await _action(
            session, account_id=account_id, action_id=mapping.subject_id, version=version, now=now
        )
    actions = list(
        await session.scalars(
            select(Action).where(
                Action.account_id == account_id, Action.card_message_id == message_id
            )
        )
    )
    if actions:
        if len(actions) != 1:
            return Continuation("unavailable", code="not_available")
        action = actions[0]
        return await _action(
            session,
            account_id=account_id,
            action_id=action.id,
            version=action.version if action.card_state is not None else None,
            now=now,
        )
    questions = list(
        await session.scalars(
            select(PendingInput).where(
                PendingInput.account_id == account_id,
                PendingInput.kind == "owner_request",
                PendingInput.prompt_message_id == message_id,
                PendingInput.expires_at > now,
            )
        )
    )
    if questions:
        refs = [question.ref_id for question in questions]
        rows = list(
            await session.scalars(
                select(OwnerRequest).where(
                    OwnerRequest.account_id == account_id,
                    OwnerRequest.id.in_(refs),
                    OwnerRequest.question_input_id.in_([question.id for question in questions]),
                )
            )
        )
        return await _request(session, account_id=account_id, rows=rows, now=now)
    turns = list(
        await session.scalars(
            select(ControlTurn).where(
                ControlTurn.account_id == account_id,
                ControlTurn.telegram_message_id == message_id,
                ControlTurn.id < turn_id,
            )
        )
    )
    if len(turns) != 1 or turns[0].text is None:
        return Continuation("unavailable", code="not_available")
    target = turns[0]
    rows = list(
        await session.scalars(
            select(OwnerRequest)
            .where(
                OwnerRequest.account_id == account_id,
                or_(
                    OwnerRequest.trigger_turn_id == target.id,
                    OwnerRequest.last_turn_id == target.id,
                    OwnerRequest.id.in_(_ids((target.refs or {}).get("owner_request_ids"))),
                ),
            )
            .order_by(OwnerRequest.id)
        )
    )
    # A topic-switch turn initially inherits the previous focus. The request
    # it subsequently opens is its reply target, even if that task has ended.
    triggered = [row for row in rows if row.trigger_turn_id == target.id]
    if triggered:
        rows = triggered
    if not rows:
        action_ids = _ids((target.refs or {}).get("continuation_action_ids"))
        versions = _ids((target.refs or {}).get("continuation_action_versions"))
        if len(action_ids) == 1 and len(versions) == 1:
            return await _action(
                session,
                account_id=account_id,
                action_id=action_ids[0],
                version=versions[0],
                now=now,
            )
    return await _request(session, account_id=account_id, rows=rows, now=now)


async def resolve(
    session: AsyncSession,
    *,
    account_id: int,
    turn_id: int,
    reply_to_message_id: int | None,
    now: datetime,
) -> Continuation:
    turn = await session.scalar(
        select(ControlTurn)
        .where(
            ControlTurn.account_id == account_id,
            ControlTurn.id == turn_id,
            ControlTurn.role == "owner",
        )
        .execution_options(populate_existing=True)
    )
    if turn is None or (
        turn.third_party and not (turn.owner_text_verified and turn.quoted_text is not None)
    ):
        return Continuation("unavailable", code="not_available")
    if turn.text is None:
        return Continuation("unavailable", code="source_unavailable")
    if (turn.refs or {}).get("explicit_target_unavailable") is True:
        return Continuation("unavailable", code="not_available", basis="explicit_reply")
    if reply_to_message_id is not None:
        selected = await _explicit(
            session, account_id=account_id, turn_id=turn_id, message_id=reply_to_message_id, now=now
        )
        return replace(selected, basis="explicit_reply")
    # Only a question still linked to a collecting request is active. A
    # suspended/terminal orphan must never hijack the next plain name.
    questions = list(
        await session.scalars(
            select(OwnerRequest)
            .join(PendingInput, PendingInput.id == OwnerRequest.question_input_id)
            .where(
                OwnerRequest.account_id == account_id,
                PendingInput.account_id == account_id,
                PendingInput.kind == "owner_request",
                PendingInput.ref_id == OwnerRequest.id,
                PendingInput.expires_at > now,
                OwnerRequest.expires_at > now,
                OwnerRequest.state == "collecting",
            )
            .order_by(OwnerRequest.id)
        )
    )
    if questions:
        focused = [row for row in questions if row.focused]
        selected = await _request(
            session, account_id=account_id, rows=focused or questions, now=now
        )
        return replace(selected, basis="pending_question")
    rows = list(
        await session.scalars(
            select(OwnerRequest).where(
                OwnerRequest.account_id == account_id,
                OwnerRequest.focused.is_(True),
                OwnerRequest.state.not_in(TERMINAL_REQUEST_STATES),
                OwnerRequest.expires_at > now,
            )
        )
    )
    if not rows:
        return Continuation("none")
    selected = await _request(session, account_id=account_id, rows=rows, now=now)
    return replace(selected, basis="focused_request")
