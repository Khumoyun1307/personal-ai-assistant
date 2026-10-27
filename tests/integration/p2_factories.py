"""Small synthetic, account-scoped seed helpers shared by P2 tests.

Every helper flushes, never commits. Conversation references are checked
against the supplied account before a domain row is created.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.actions.models import Action
from assistant.automation.models import Automation
from assistant.conversations.models import Contact, Conversation
from assistant.core.errors import NotInAccountError
from assistant.inbox.models import ConversationTriage, Notification, Task

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_ids = count(8_100_000_000)


async def _account(session: AsyncSession, account_id: int) -> None:
    if await session.scalar(select(Account.id).where(Account.id == account_id)) is None:
        raise NotInAccountError("account not found")


async def _conversation(
    session: AsyncSession, account_id: int, conversation_id: int | None
) -> None:
    await _account(session, account_id)
    if (
        conversation_id is not None
        and await session.scalar(
            select(Conversation.id).where(
                Conversation.account_id == account_id, Conversation.id == conversation_id
            )
        )
        is None
    ):
        raise NotInAccountError("conversation not found")


async def seed_account(
    session: AsyncSession, *, telegram_user_id: int | None = None, **fields: Any
) -> Account:
    user_id = telegram_user_id if telegram_user_id is not None else next(_ids)
    row = Account(
        **{
            "telegram_user_id": user_id,
            "control_chat_id": user_id,
            "status": "active",
            "timezone": "UTC",
            "created_at": NOW,
            **fields,
        }
    )
    session.add(row)
    await session.flush()
    return row


async def seed_conversation(
    session: AsyncSession,
    *,
    account_id: int,
    business_chat_id: int | None = None,
    telegram_user_id: int | None = None,
    contact_id: int | None = None,
    **fields: Any,
) -> Conversation:
    await _account(session, account_id)
    chat_id = business_chat_id if business_chat_id is not None else next(_ids)
    if contact_id is None:
        contact = Contact(
            account_id=account_id,
            telegram_user_id=telegram_user_id if telegram_user_id is not None else chat_id,
            display_name="Synthetic contact",
            is_bot=False,
            first_seen_at=NOW,
            updated_at=NOW,
        )
        session.add(contact)
        await session.flush()
        contact_id = contact.id
    elif (
        await session.scalar(
            select(Contact.id).where(Contact.id == contact_id, Contact.account_id == account_id)
        )
        is None
    ):
        raise NotInAccountError("contact not found")
    row = Conversation(
        **{
            "account_id": account_id,
            "contact_id": contact_id,
            "business_chat_id": chat_id,
            "mode": "observe",
            "last_contact_msg_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )
    session.add(row)
    await session.flush()
    return row


async def seed_action(
    session: AsyncSession, *, account_id: int, conversation_id: int, **fields: Any
) -> Action:
    await _conversation(session, account_id, conversation_id)
    row = Action(
        **{
            "account_id": account_id,
            "conversation_id": conversation_id,
            "kind": "send_message",
            "origin": "agent_proposal",
            "state": "proposed",
            "text": "Synthetic reply",
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )
    session.add(row)
    await session.flush()
    return row


async def seed_task(
    session: AsyncSession, *, account_id: int, conversation_id: int | None = None, **fields: Any
) -> Task:
    await _conversation(session, account_id, conversation_id)
    row = Task(
        **{
            "account_id": account_id,
            "conversation_id": conversation_id,
            "kind": "open_question",
            "title": "Synthetic task",
            "created_by": "agent",
            "source_message_ids": [],
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )
    session.add(row)
    await session.flush()
    return row


async def seed_triage(
    session: AsyncSession, *, account_id: int, conversation_id: int, **fields: Any
) -> ConversationTriage:
    await _conversation(session, account_id, conversation_id)
    row = ConversationTriage(
        **{
            "account_id": account_id,
            "conversation_id": conversation_id,
            "covers_through_message_id": 1,
            "needs_reply": False,
            "contains_question": False,
            "requests_decision": False,
            "urgency": "normal",
            "escalation_category": "none",
            "reasons": ["Synthetic reason"],
            "prompt_version": "synthetic.v1",
            "model": "synthetic",
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )
    session.add(row)
    await session.flush()
    return row


async def seed_automation(session: AsyncSession, *, account_id: int, **fields: Any) -> Automation:
    await _account(session, account_id)
    row = Automation(
        **{
            "account_id": account_id,
            "kind": "monitor",
            "spec": {"schema_version": 1},
            "refs": {},
            "timezone": "UTC",
            "tz_policy": "follow_owner",
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )
    session.add(row)
    await session.flush()
    return row


async def seed_notification(
    session: AsyncSession, *, account_id: int, conversation_id: int | None = None, **fields: Any
) -> Notification:
    await _conversation(session, account_id, conversation_id)
    row = Notification(
        **{
            "account_id": account_id,
            "conversation_id": conversation_id,
            "kind": "triage_needs_reply",
            "priority": "normal",
            "dedupe_key": f"synthetic:{next(_ids)}",
            "refs": {},
            "created_at": NOW,
            "updated_at": NOW,
            **fields,
        }
    )
    session.add(row)
    await session.flush()
    return row
