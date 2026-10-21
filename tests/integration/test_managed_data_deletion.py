"""Deletion of managed data leaves no content anywhere (S02.4, ARCHITECTURE §10.1).

The scan is schema-driven: every `text`/`jsonb` column in the `app` and
`procrastinate` schemas is searched for the canary, not just the columns we
remembered to list. And the registry itself is checked against the live
schema, so a new content column cannot slip in unclassified.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from assistant.privacy.content_columns import CONTENT_COLUMNS, NON_CONTENT_COLUMNS
from assistant.privacy.deletion import delete_managed_data
from tests.integration.business_env import (
    OWNER,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    environment,
    private_message,
)

pytestmark = pytest.mark.integration

CANARY = "CANARY-SYNTH-4d1a"


async def _text_columns(env: Env) -> list[tuple[str, str, str]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT table_schema, table_name, column_name FROM information_schema.columns "
                "WHERE table_schema IN ('app', 'procrastinate') "
                "AND data_type IN ('text', 'jsonb', 'character varying', 'json') "
                "ORDER BY 1, 2, 3"
            )
        )
        return [(str(s), str(t), str(c)) for s, t, c in rows.all()]


async def _columns_containing(env: Env, needle: str) -> list[str]:
    hits: list[str] = []
    for schema, table, column in await _text_columns(env):
        async with env.factory() as session:
            found = await session.execute(
                text(
                    f'SELECT 1 FROM "{schema}"."{table}" '  # noqa: S608 - catalogue identifiers
                    f'WHERE "{column}"::text LIKE :needle LIMIT 1'
                ),
                {"needle": f"%{needle}%"},
            )
            if found.first() is not None:
                hits.append(f"{schema}.{table}.{column}")
    return hits


async def _account_id(env: Env, telegram_user_id: int) -> int:
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT id FROM app.accounts WHERE telegram_user_id = :u"),
                    {"u": telegram_user_id},
                )
            ).scalar_one()
        )


async def test_every_text_or_jsonb_column_is_classified(env: Env) -> None:
    """Adding a text column means deciding whether it is content (§10.1)."""
    app_columns = {(t, c) for s, t, c in await _text_columns(env) if s == "app"}
    classified = CONTENT_COLUMNS | NON_CONTENT_COLUMNS
    unclassified = app_columns - classified
    stale = classified - app_columns
    assert unclassified == set(), f"unclassified text/jsonb columns: {sorted(unclassified)}"
    assert stale == set(), f"registry names columns that do not exist: {sorted(stale)}"
    assert set() == CONTENT_COLUMNS & NON_CONTENT_COLUMNS


async def test_after_deletion_no_managed_content_remains_in_any_content_column(env: Env) -> None:
    await connect(env)
    first = business_message(message_id=1, message_text=f"{CANARY} one", first_name=CANARY)
    await deliver(env, first)
    await deliver(
        env,
        business_message(
            message_id=1, message_text=f"{CANARY} edited", edit=True, first_name=CANARY
        ),
    )
    await deliver(
        env, business_message(message_id=2, message_text=f"{CANARY} two", first_name=CANARY)
    )
    await deliver(env, deleted_messages(message_ids=[2]))
    before = await _columns_containing(env, CANARY)
    assert "app.messages.text" in before
    assert "app.contacts.display_name" in before
    assert "app.inbound_updates.payload" in before

    account_id = await _account_id(env, OWNER)
    async with env.factory() as session, session.begin():
        report = await delete_managed_data(session, account_id=account_id, clock=env.clock)

    assert report.messages == 2
    assert report.contacts_cleared == 1
    assert report.payloads_cleared >= 3  # message, edit, deletion, connection
    assert await _columns_containing(env, CANARY) == []


async def test_deletion_leaves_other_accounts_intact(migrated_url: str) -> None:
    from tests.integration.business_env import connection_update

    other = 7000000002
    other_chat = 7000000077
    async with environment(migrated_url, owner_ids=frozenset({OWNER, other})) as env:
        await connect(env)
        await deliver(env, business_message(message_text=f"{CANARY} mine"))
        await deliver(env, private_message(other, "/start"))
        await deliver(env, private_message(other, "yes"))
        await deliver(env, connection_update(user_id=other, connection_id="synthetic-conn-b"))
        await deliver(
            env,
            business_message(
                "synthetic-conn-b",
                from_id=other_chat,
                chat_id=other_chat,
                message_text="OTHER-SYNTH-keep",
                first_name="OtherSynthetic",
            ),
        )
        mine = await _account_id(env, OWNER)
        async with env.factory() as session, session.begin():
            await delete_managed_data(session, account_id=mine, clock=env.clock)

        assert await _columns_containing(env, CANARY) == []
        kept = await _columns_containing(env, "OTHER-SYNTH-keep")
        assert "app.messages.text" in kept
        assert "app.contacts.display_name" in await _columns_containing(env, "OtherSynthetic")
