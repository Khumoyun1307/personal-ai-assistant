"""Synthetic chats for the actions-core tests (S04.1).

`seed_chat` builds one account that can be replied for: active, a connected
business bot with the reply right, managed-AI consent naming the draft
route's processor, and one conversation whose contact wrote an hour ago.
Every knob that a policy rule reads can be turned off. Texts are synthetic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import (
    Account,
    AuditLogEntry,
    BusinessConnection,
    Consent,
    ControlTurn,
)
from assistant.actions.config import ActionsConfig
from assistant.actions.models import Action
from assistant.actions.proposals import ProposalRequest
from assistant.actions.states import ActionKind, ActionOrigin
from assistant.agent.models import AgentRun
from assistant.conversations.models import Contact, Conversation, Message
from tests.integration.business_env import Env

CONFIG = ActionsConfig(
    approval_max_age=timedelta(hours=6),
    draft_debounce=timedelta(seconds=20),
    mark_uncertain_after=timedelta(minutes=2),
    connection_stale_alert_after=timedelta(hours=1),
    sending_enabled=True,
    process_run_id="synthetic-run",
    assist_draft_trigger="off",
    draft_route_model="gpt-6-luna",
)
DRAFT_TEXT = "Synthetic draft reply"

_ids = count(7_400_000_000)


@dataclass(frozen=True, slots=True)
class Chat:
    account_id: int
    conversation_id: int
    contact_message_id: int


async def seed_chat(
    session: AsyncSession,
    now: datetime,
    *,
    reply_right: bool = True,
    consent: bool = True,
    connection: bool = True,
    contact_wrote: timedelta | None = timedelta(hours=1),
    **conversation_fields: Any,
) -> Chat:
    user_id = next(_ids)
    account = Account(telegram_user_id=user_id, control_chat_id=user_id, status="active")
    session.add(account)
    await session.flush()
    if connection:
        session.add(
            BusinessConnection(
                account_id=account.id,
                connection_id=f"synthetic-conn-{user_id}",
                is_enabled=True,
                rights=["can_reply"] if reply_right else [],
                capability_tier="can_reply" if reply_right else "observe_only",
                user_chat_id=user_id,
                status="active",
                connected_at=now - timedelta(days=1),
            )
        )
    if consent:
        session.add(
            Consent(
                account_id=account.id,
                kind="ai_managed",
                policy_version="synthetic-1",
                processors=["OpenAI"],
            )
        )
    contact = Contact(account_id=account.id, telegram_user_id=next(_ids))
    session.add(contact)
    await session.flush()
    anchor = None if contact_wrote is None else now - contact_wrote
    conversation = Conversation(
        account_id=account.id,
        contact_id=contact.id,
        business_chat_id=contact.telegram_user_id,
        mode="assist",
        last_contact_msg_at=anchor,
        **conversation_fields,
    )
    session.add(conversation)
    await session.flush()
    message_id = 0
    if anchor is not None:
        message_id = await add_message(
            session, account.id, conversation.id, direction="contact", sent_at=anchor
        )
    return Chat(account.id, conversation.id, message_id)


async def add_message(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    direction: str,
    sent_at: datetime,
    deleted: bool = False,
    **fields: Any,
) -> int:
    row = Message(
        account_id=account_id,
        conversation_id=conversation_id,
        telegram_message_id=next(_ids),
        direction=direction,
        sent_at=sent_at,
        content_type="text",
        text=None if deleted else "Synthetic message",
        deleted_at=sent_at if deleted else None,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row.id


async def seed_run(session: AsyncSession, account_id: int) -> int:
    run = AgentRun(account_id=account_id, run_type="owner_request", model=CONFIG.draft_route_model)
    session.add(run)
    await session.flush()
    return run.id


async def seed_turn(session: AsyncSession, account_id: int) -> int:
    turn = ControlTurn(account_id=account_id, role="owner")
    session.add(turn)
    await session.flush()
    return turn.id


def request(chat: Chat, **fields: Any) -> ProposalRequest:
    values: dict[str, Any] = {
        "account_id": chat.account_id,
        "conversation_id": chat.conversation_id,
        "origin": ActionOrigin.AGENT_PROPOSAL,
        "kind": ActionKind.SEND_MESSAGE,
        "text": DRAFT_TEXT,
        "base_message_id": chat.contact_message_id or None,
        "shown_through_message_id": None,
        "run_id": None,
        "tool_call_id": None,
        "trigger_turn_id": None,
        "draft_run_id": None,
        **fields,
    }
    return ProposalRequest(**values)


async def action(env: Env, action_id: int) -> Action:
    async with env.factory() as session:
        row = await session.get(Action, action_id)
        assert row is not None
        return row


async def audit(env: Env, action_id: int) -> list[AuditLogEntry]:
    async with env.factory() as session:
        rows = await session.scalars(
            select(AuditLogEntry)
            .where(AuditLogEntry.subject_type == "action", AuditLogEntry.subject_id == action_id)
            .order_by(AuditLogEntry.id)
        )
        return list(rows)


async def wait_for_a_lock_wait(env: Env, *, deadline: float = 20.0) -> None:
    """Poll until another session of this database waits on a lock. No
    assertion depends on elapsed time; the deadline only bounds a failure."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while True:
        async with env.factory() as session:
            waiting = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND pid <> pg_backend_pid()"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        if loop.time() > end:
            raise AssertionError("the second session never waited for the lock")
        await asyncio.sleep(0.02)
