"""Read models behind the conversation tools (S03.1): account-scoped lists,
pages with a cursor, coverage facts, the contact directory. Synthetic data."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from assistant.conversations.read import (
    contact_directory,
    conversation_page,
    list_conversations,
)
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connect,
    connection_update,
    deleted_messages,
    deliver,
    environment,
    private_message,
)

pytestmark = pytest.mark.integration

_ids = iter(range(20_000, 30_000))


async def say(
    env: Env,
    body: str,
    *,
    contact: int = CONTACT,
    minutes: int = 0,
    first_name: str = "Synthetic",
    owner: bool = False,
) -> int:
    message_id = next(_ids)
    await deliver(
        env,
        business_message(
            from_id=OWNER if owner else contact,
            chat_id=contact,
            message_id=message_id,
            message_text=body,
            date=T_BASE + minutes * 60,
            first_name=first_name,
        ),
    )
    return message_id


async def account_id(env: Env, owner: int = OWNER) -> int:
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT id FROM app.accounts WHERE telegram_user_id = :o"), {"o": owner}
                )
            ).scalar_one()
        )


async def conversation_id(env: Env, chat: int) -> int:
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT id FROM app.conversations WHERE business_chat_id = :c"),
                    {"c": chat},
                )
            ).scalar_one()
        )


async def test_conversations_are_listed_by_latest_activity(env: Env) -> None:
    await connect(env)
    await say(env, "first", contact=CONTACT, minutes=0, first_name="Aziza")
    await say(env, "second", contact=OTHER_CONTACT, minutes=5, first_name="Bobur")
    async with env.factory() as session:
        rows = await list_conversations(session, await account_id(env))
    assert [row.display_name for row in rows] == ["Bobur", "Aziza"]
    assert all(row.retained_messages == 1 for row in rows)


async def test_awaiting_reply_lists_only_chats_where_the_contact_spoke_last(env: Env) -> None:
    await connect(env)
    await say(env, "question", contact=CONTACT, minutes=0)
    await say(env, "answered", contact=CONTACT, minutes=1, owner=True)
    await say(env, "waiting", contact=OTHER_CONTACT, minutes=2)
    async with env.factory() as session:
        rows = await list_conversations(session, await account_id(env), awaiting_owner_reply=True)
    assert [row.conversation_id for row in rows] == [await conversation_id(env, OTHER_CONTACT)]


async def test_a_page_is_oldest_first_with_a_cursor_to_older_messages(env: Env) -> None:
    await connect(env)
    for minute in range(5):
        await say(env, f"line {minute}", minutes=minute)
    conv = await conversation_id(env, CONTACT)
    async with env.factory() as session:
        first = await conversation_page(session, await account_id(env), conv, limit=3)
        assert [m.text for m in first.messages] == ["line 2", "line 3", "line 4"]
        assert first.next_cursor is not None
        older = await conversation_page(
            session, await account_id(env), conv, before=first.next_cursor, limit=3
        )
    assert [m.text for m in older.messages] == ["line 0", "line 1"]
    assert older.next_cursor is None


async def test_paging_follows_dates_even_when_ids_are_out_of_order(env: Env) -> None:
    """J: a message stored late (a re-queued dead letter: new id, old date)
    is neither skipped nor repeated — the cursor is (sent_at, id)."""
    await connect(env)
    for minute in (10, 20, 30, 40):
        await say(env, f"line {minute}", minutes=minute)
    await say(env, "line 5 (stored last)", minutes=5)
    conv = await conversation_id(env, CONTACT)
    seen: list[str] = []
    cursor = None
    async with env.factory() as session:
        for _ in range(5):
            page = await conversation_page(
                session, await account_id(env), conv, before=cursor, limit=2
            )
            seen = [m.text or "" for m in page.messages] + seen
            cursor = page.next_cursor
            if cursor is None:
                break
    assert seen == ["line 5 (stored last)", "line 10", "line 20", "line 30", "line 40"]


async def test_a_page_reports_deleted_messages_in_its_coverage(env: Env) -> None:
    await connect(env)
    await say(env, "kept")
    gone = await say(env, "gone", minutes=1)
    await deliver(env, deleted_messages(message_ids=[gone]))
    async with env.factory() as session:
        page = await conversation_page(
            session, await account_id(env), await conversation_id(env, CONTACT)
        )
    assert [m.text for m in page.messages] == ["kept"]
    assert page.coverage.deleted_messages == 1
    assert page.coverage.coverage_started_at is not None


async def test_another_accounts_conversation_is_not_found(migrated_url: str) -> None:
    """RG-21: a foreign id behaves exactly like an unknown one."""
    other = 7000000002
    async with environment(migrated_url, owner_ids=frozenset({OWNER, other})) as env:
        await connect(env)
        await deliver(env, private_message(other, "/start"))
        await deliver(env, private_message(other, "yes"))
        await deliver(env, connection_update(user_id=other, connection_id="synthetic-conn-b"))
        await deliver(
            env,
            business_message(
                "synthetic-conn-b", from_id=7000000077, chat_id=7000000077, message_text="theirs"
            ),
        )
        theirs = await conversation_id(env, 7000000077)
        mine = await account_id(env)
        async with env.factory() as session:
            with pytest.raises(LookupError):
                await conversation_page(session, mine, theirs)
            with pytest.raises(LookupError):
                await conversation_page(session, mine, 987654321)
            assert await list_conversations(session, mine) == []
            assert await contact_directory(session, mine) == []


async def test_the_directory_lists_named_contacts_with_their_conversation(env: Env) -> None:
    await connect(env)
    await say(env, "hi", first_name="Dilnoza")
    async with env.factory() as session:
        [entry] = await contact_directory(session, await account_id(env))
    assert entry.display_name == "Dilnoza"
    assert entry.conversation_id == await conversation_id(env, CONTACT)
