"""Cross-account isolation (§6 invariant 12, AGENTS §9).

Named regression suite: never weaken or delete these. One account must not be
able to read another's rows, and the API must make forgetting the filter
impossible rather than merely discouraged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.db.repository import AccountScopedRepository, AccountScopeError

pytestmark = pytest.mark.integration


class ControlTurnRepository(AccountScopedRepository[ControlTurn]):
    model = ControlTurn


class AuditRepository(AccountScopedRepository[AuditLogEntry]):
    model = AuditLogEntry


async def make_account(session: AsyncSession, telegram_user_id: int) -> Account:
    account = Account(telegram_user_id=telegram_user_id, status="active", timezone="UTC")
    session.add(account)
    await session.flush()
    return account


async def test_one_account_cannot_read_another_s_rows(db_session: AsyncSession) -> None:
    owner = await make_account(db_session, 1001)
    stranger = await make_account(db_session, 1002)

    db_session.add(ControlTurn(account_id=owner.id, role="owner", text="mine"))
    db_session.add(ControlTurn(account_id=stranger.id, role="owner", text="theirs"))
    await db_session.flush()

    owner_repo = ControlTurnRepository(db_session, owner.id)
    rows = await owner_repo.list_all()

    assert len(rows) == 1
    assert rows[0].account_id == owner.id
    assert await owner_repo.count() == 1


async def test_fetching_a_foreign_row_by_id_returns_none(db_session: AsyncSession) -> None:
    """Not an error — indistinguishable from "does not exist", by design."""
    owner = await make_account(db_session, 1003)
    stranger = await make_account(db_session, 1004)

    foreign = ControlTurn(account_id=stranger.id, role="owner", text="theirs")
    db_session.add(foreign)
    await db_session.flush()

    assert await ControlTurnRepository(db_session, owner.id).get(foreign.id) is None
    # ...and the row really does exist, for its own account.
    assert await ControlTurnRepository(db_session, stranger.id).get(foreign.id) is not None


async def test_add_forces_the_repository_s_account(db_session: AsyncSession) -> None:
    """A caller cannot smuggle in a row belonging to someone else."""
    owner = await make_account(db_session, 1005)
    stranger = await make_account(db_session, 1006)

    repo = ControlTurnRepository(db_session, owner.id)
    smuggled = ControlTurn(account_id=stranger.id, role="owner", text="wrong owner")
    repo.add(smuggled)
    await db_session.flush()

    assert smuggled.account_id == owner.id


@pytest.mark.parametrize("bad_account_id", [0, -1])
async def test_repository_refuses_a_non_account(
    db_session: AsyncSession, bad_account_id: int
) -> None:
    """`0` must never be read as "all accounts"."""
    with pytest.raises(AccountScopeError):
        ControlTurnRepository(db_session, bad_account_id)


async def test_isolation_holds_for_every_scoped_table(db_session: AsyncSession) -> None:
    owner = await make_account(db_session, 1007)
    stranger = await make_account(db_session, 1008)

    db_session.add(AuditLogEntry(account_id=stranger.id, actor="system", event="theirs"))
    await db_session.flush()

    assert await AuditRepository(db_session, owner.id).count() == 0
    assert await AuditRepository(db_session, stranger.id).count() == 1


async def test_deleting_an_account_cascades_to_its_rows(db_session: AsyncSession) -> None:
    """Deletion must not leave orphaned content behind (AGENTS §9)."""
    account = await make_account(db_session, 1009)
    db_session.add(ControlTurn(account_id=account.id, role="owner", text="content"))
    await db_session.flush()

    await db_session.delete(account)
    await db_session.flush()

    repo = ControlTurnRepository(db_session, account.id)
    assert await repo.count() == 0


async def test_account_telegram_user_id_is_unique(db_session: AsyncSession) -> None:
    """Two accounts for one Telegram user would split a tenant in half."""
    await make_account(db_session, 1010)
    db_session.add(Account(telegram_user_id=1010, status="active", timezone="UTC"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ------------------------------------------------ managed conversations (S02.2)
class ContactRepository(AccountScopedRepository[Contact]):
    model = Contact


class ConversationRepository(AccountScopedRepository[Conversation]):
    model = Conversation


class MessageRepository(AccountScopedRepository[Message]):
    model = Message


class CoverageGapRepository(AccountScopedRepository[CoverageGap]):
    model = CoverageGap


async def _seed_chat(session: AsyncSession, account: Account, chat_id: int, text: str) -> int:
    contact = Contact(account_id=account.id, telegram_user_id=chat_id, display_name=text)
    session.add(contact)
    await session.flush()
    conversation = Conversation(
        account_id=account.id, business_chat_id=chat_id, contact_id=contact.id
    )
    session.add(conversation)
    await session.flush()
    session.add(
        Message(
            account_id=account.id,
            conversation_id=conversation.id,
            telegram_message_id=1,
            direction="contact",
            sent_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
            content_type="text",
            text=text,
        )
    )
    session.add(
        CoverageGap(
            account_id=account.id,
            conversation_id=conversation.id,
            kind="dead_letter",
            certainty="definite",
            at_risk_from=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
            at_risk_to=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        )
    )
    await session.flush()
    return conversation.id


@pytest.mark.parametrize(
    "repository",
    [ContactRepository, ConversationRepository, MessageRepository, CoverageGapRepository],
)
async def test_isolation_holds_for_every_managed_conversation_table(
    db_session: AsyncSession, repository: type[AccountScopedRepository[Any]]
) -> None:
    """RG-21 extended to S02.2's tables: one owner never sees another's chats."""
    owner = await make_account(db_session, 2001)
    stranger = await make_account(db_session, 2002)
    await _seed_chat(db_session, owner, 3001, "mine")
    await _seed_chat(db_session, stranger, 3002, "theirs")

    rows = await repository(db_session, owner.id).list_all()
    assert len(rows) == 1
    assert rows[0].account_id == owner.id


async def test_the_same_chat_under_two_owners_is_two_conversations(
    db_session: AsyncSession,
) -> None:
    """Business chat ids live in each owner's numbering (AGENTS §7): the same
    counterpart id under two accounts is two unrelated conversations."""
    owner = await make_account(db_session, 2003)
    other = await make_account(db_session, 2004)
    first = await _seed_chat(db_session, owner, 3003, "a")
    second = await _seed_chat(db_session, other, 3003, "b")

    assert first != second
    assert await ConversationRepository(db_session, owner.id).get(second) is None
    assert await MessageRepository(db_session, owner.id).count() == 1
