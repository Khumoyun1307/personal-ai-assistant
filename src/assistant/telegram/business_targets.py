"""Where a business send goes, resolved at call time (ARCHITECTURE §6.8; S04.4).

The connection id is mutable (TF §12 LV-05): a reconnect issues a new one, so
the gateway never trusts an id captured earlier. Every business call asks
here, in a short read-only session, for the account's **current** connection
and the conversation's chat. Anything missing, inactive, disabled, another
account's or a bot chat resolves to `None`, and the gateway then refuses
without calling Telegram (AGENTS §6 invariant 7: fail closed).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import BusinessConnection
from assistant.conversations.models import Contact, Conversation, Message


@dataclass(frozen=True, slots=True)
class BusinessTarget:
    """The current connection and the chat of one managed conversation."""

    connection_id: str
    chat_id: int


class BusinessTargetResolver:
    """Account-scoped reads for the gateway's business methods."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, account_id: int, conversation_id: int) -> BusinessTarget | None:
        """The account's active, enabled connection and this conversation's
        chat — a business conversation of the same account with a human
        contact. None for anything else."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(BusinessConnection.connection_id, Conversation.business_chat_id)
                    .join(Contact, Contact.id == Conversation.contact_id)
                    .join(
                        BusinessConnection,
                        BusinessConnection.account_id == Conversation.account_id,
                    )
                    .where(
                        Conversation.id == conversation_id,
                        Conversation.account_id == account_id,
                        Conversation.kind == "business",
                        Contact.account_id == account_id,
                        Contact.is_bot.is_(False),
                        BusinessConnection.status == "active",
                        BusinessConnection.is_enabled.is_(True),
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return BusinessTarget(connection_id=str(row[0]), chat_id=int(row[1]))

    async def assistant_message_ids(
        self, account_id: int, conversation_id: int, refs: Sequence[int]
    ) -> list[int] | None:
        """The Telegram message ids behind `refs` (`messages.id` values), in
        order — or None unless every ref is an `assistant_sent` row of this
        conversation, not deleted, whose Telegram id is known (RG-12: only
        the assistant's own sends may ever be edited or deleted)."""
        wanted = list(dict.fromkeys(refs))
        if not wanted:
            return None
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Message.id, Message.telegram_message_id).where(
                        Message.id.in_(wanted),
                        Message.account_id == account_id,
                        Message.conversation_id == conversation_id,
                        Message.direction == "assistant_sent",
                        Message.deleted_at.is_(None),
                        Message.telegram_message_id.is_not(None),
                    )
                )
            ).all()
        found = {
            int(message_id): int(telegram_id)
            for message_id, telegram_id in rows
            if telegram_id is not None
        }
        if set(found) != set(wanted):
            return None
        return [found[ref] for ref in wanted]
