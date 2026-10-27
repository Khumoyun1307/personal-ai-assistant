"""Deletion of managed data leaves no content anywhere (S02.4, ARCHITECTURE §10.1).

The scan is schema-driven: every `text`/`jsonb` column in the `app` and
`procrastinate` schemas is searched for the canary, not just the columns we
remembered to list. And the registry itself is checked against the live
schema, so a new content column cannot slip in unclassified.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from assistant.accounts.service import record_control_turn
from assistant.agent.models import AgentRun, ToolConfirmation
from assistant.privacy.content_columns import CONTENT_COLUMNS, NON_CONTENT_COLUMNS
from assistant.privacy.deletion import delete_managed_data, purge_conversation
from tests.integration.business_env import (
    CONTACT as CONTACT_CHAT,
)
from tests.integration.business_env import (
    OWNER,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    environment,
    onboard,
)

pytestmark = pytest.mark.integration

CANARY = "CANARY-SYNTH-4d1a"


async def test_managed_deletion_clears_action_texts_agent_tasks_and_triage_and_keeps_automations(
    env: Env,
) -> None:
    from assistant.actions.models import Action
    from assistant.automation.models import Automation
    from assistant.inbox.models import ConversationTriage, Notification, Task
    from tests.integration.test_action_lifecycle_bulk import seed_actions

    account_id, conversation_id, ids = await seed_actions(env)
    other_account_id, _, _ = await seed_actions(env, user_id=7000080002)
    async with env.factory() as session, session.begin():
        for created_by in ("owner", "agent"):
            session.add(
                Task(
                    account_id=account_id,
                    conversation_id=conversation_id,
                    kind="open_question",
                    title="synthetic task",
                    created_by=created_by,
                )
            )
        session.add(
            ConversationTriage(
                account_id=account_id,
                conversation_id=conversation_id,
                covers_through_message_id=1,
                needs_reply=True,
                contains_question=True,
                requests_decision=False,
                urgency="normal",
                escalation_category="none",
                reasons=["synthetic reason"],
                prompt_version="synthetic.v1",
                model="synthetic-model",
            )
        )
        session.add(
            Automation(
                account_id=account_id,
                kind="monitor",
                spec={"schema_version": 1},
                timezone="UTC",
                tz_policy="fixed",
            )
        )
        session.add(
            Notification(
                account_id=account_id,
                kind="system",
                priority="normal",
                dedupe_key="synthetic-managed",
            )
        )
    async with env.factory() as session, session.begin():
        await delete_managed_data(session, account_id=account_id, clock=env.clock)
    async with env.factory() as session:
        actions = list(await session.scalars(select(Action).where(Action.account_id == account_id)))
        assert all(action.text is None for action in actions)
        assert all(
            action.state == "cancelled"
            for action in actions
            if action.id
            in {ids["proposed"], ids["awaiting_approval"], ids["approved"], ids["scheduled"]}
        )
        assert list(
            await session.scalars(select(Task.created_by).where(Task.account_id == account_id))
        ) == ["owner"]
        assert (
            list(
                await session.scalars(
                    select(ConversationTriage).where(ConversationTriage.account_id == account_id)
                )
            )
            == []
        )
        assert (
            list(
                await session.scalars(
                    select(Notification).where(Notification.account_id == account_id)
                )
            )
            == []
        )
        assert (
            len(
                list(
                    await session.scalars(
                        select(Automation).where(Automation.account_id == account_id)
                    )
                )
            )
            == 1
        )
        assert all(
            action.text is not None
            for action in await session.scalars(
                select(Action).where(Action.account_id == other_account_id)
            )
        )


async def _text_columns(env: Env) -> list[tuple[str, str, str]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT table_schema, table_name, column_name FROM information_schema.columns "
                "WHERE table_schema IN ('app', 'procrastinate') "
                "AND data_type IN ('text', 'jsonb', 'character varying', 'json', 'tsvector') "
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
                    # ILIKE: search keys hold the canary case-folded.
                    f'WHERE "{column}"::text ILIKE :needle LIMIT 1'
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
    assert "app.messages.search_key" in before
    assert "app.messages.search_tsv" in before
    assert "app.contacts.display_name" in before
    assert "app.inbound_updates.payload" in before

    account_id = await _account_id(env, OWNER)
    async with env.factory() as session, session.begin():
        report = await delete_managed_data(session, account_id=account_id, clock=env.clock)

    assert report.messages == 2
    assert report.contacts_cleared == 1
    assert report.payloads_cleared >= 3  # message, edit, deletion, connection
    assert await _columns_containing(env, CANARY) == []


async def test_deletion_clears_quoted_text_and_held_tool_arguments(env: Env) -> None:
    """S03.1 content columns: `control_turns.quoted_text` of a turn built on
    managed content, and `tool_confirmations.args`, go with the rest."""
    await connect(env)
    await deliver(env, business_message(message_text=f"{CANARY} chat"))
    account_id = await _account_id(env, OWNER)
    async with env.factory() as session, session.begin():
        await record_control_turn(
            session,
            account_id=account_id,
            role="owner",
            text=f"{CANARY} asked",
            quoted_text=f"{CANARY} quoted",
            contains_managed=True,
        )
        session.add(
            ToolConfirmation(
                account_id=account_id,
                tool_call_id="call-1",
                tool_name="synthetic_configure",
                args={"note": f"{CANARY} held"},
            )
        )
    before = await _columns_containing(env, CANARY)
    assert {"app.control_turns.quoted_text", "app.tool_confirmations.args"} <= set(before)

    async with env.factory() as session, session.begin():
        report = await delete_managed_data(session, account_id=account_id, clock=env.clock)

    assert await _columns_containing(env, CANARY) == []
    async with env.factory() as session:
        status = (await session.execute(select(ToolConfirmation.status))).scalar_one()
        details = (
            await session.execute(
                text("SELECT details FROM app.audit_log WHERE event = 'managed_data_deleted'")
            )
        ).scalar_one()
    assert status == "expired"
    # S03.5 fix round 2, item 3: the held call is counted, not "0 held change(s)".
    assert report.held_calls_cleared == 1
    assert "1 held change(s)" in report.receipt()
    assert details["held_calls_cleared"] == 1


async def test_purging_a_conversation_redacts_the_turns_that_referred_to_it(env: Env) -> None:
    """`control_turns.refs` has a consumer: a purged chat takes the answers
    that quoted it, and the held calls of those runs, with it (§10.4)."""
    await connect(env)
    await deliver(env, business_message(message_text=f"{CANARY} chat"))
    account_id = await _account_id(env, OWNER)
    async with env.factory() as session:
        conversation_id, business_chat_id, contact_id = (
            await session.execute(
                text("SELECT id, business_chat_id, contact_id FROM app.conversations")
            )
        ).one()
    async with env.factory() as session, session.begin():
        run = AgentRun(
            account_id=account_id, run_type="owner_request", model="gpt-6-luna", status="succeeded"
        )
        session.add(run)
        await session.flush()
        referring = await record_control_turn(
            session,
            account_id=account_id,
            role="assistant",
            text=f"{CANARY} answer",
            refs={"conversation_ids": [conversation_id], "contact_ids": [contact_id]},
        )
        referring.run_id = run.id
        by_contact = await record_control_turn(
            session,
            account_id=account_id,
            role="assistant",
            text=f"{CANARY} by contact",
            refs={"conversation_ids": [], "contact_ids": [contact_id]},
        )
        unrelated = await record_control_turn(
            session,
            account_id=account_id,
            role="assistant",
            text="Synthetic unrelated",
            refs={"conversation_ids": [conversation_id + 1000], "contact_ids": []},
        )
        session.add(
            ToolConfirmation(
                account_id=account_id,
                run_id=run.id,
                tool_call_id="c1",
                tool_name="synthetic_configure",
                args={"note": f"{CANARY} held"},
            )
        )
    async with env.factory() as session, session.begin():
        report = await purge_conversation(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            business_chat_id=business_chat_id,
            clock=env.clock,
        )
    assert report.control_turns_cleared == 2
    async with env.factory() as session:
        texts = dict((await session.execute(text("SELECT id, text FROM app.control_turns"))).all())
    assert texts[referring.id] is None
    assert texts[by_contact.id] is None
    assert texts[unrelated.id] == "Synthetic unrelated"
    assert await _columns_containing(env, CANARY) == []


async def _add_summaries(env: Env, account_id: int, conversation_id: int, marker: str) -> None:
    async with env.factory() as session, session.begin():
        for kind, start in (("rolling", None), ("period", env.clock.now())):
            await session.execute(
                text(
                    "INSERT INTO app.conversation_summaries "
                    "(account_id, conversation_id, kind, period_start, summary) "
                    "VALUES (:a, :c, :k, :s, CAST(:j AS jsonb))"
                ),
                {
                    "a": account_id,
                    "c": conversation_id,
                    "k": kind,
                    "s": start,
                    "j": json.dumps({"overview": f"{marker} summary"}),
                },
            )


async def test_conversation_summaries_go_with_purge_and_managed_data_deletion(env: Env) -> None:
    """S03.2's content column `conversation_summaries.summary` is covered by
    both cascades: a conversation purge takes that chat's summaries only;
    managed-data deletion (offered on decline/revoke) takes them all."""
    await connect(env)
    await deliver(env, business_message(message_text=f"{CANARY} one"))
    await deliver(
        env,
        business_message(from_id=7000000006, chat_id=7000000006, message_text="Synthetic two"),
    )
    account_id = await _account_id(env, OWNER)
    async with env.factory() as session:
        rows = dict(
            (
                await session.execute(text("SELECT business_chat_id, id FROM app.conversations"))
            ).all()
        )
    first, second = rows[CONTACT_CHAT], rows[7000000006]
    await _add_summaries(env, account_id, first, CANARY)
    await _add_summaries(env, account_id, second, "OTHER-SYNTH")
    assert "app.conversation_summaries.summary" in await _columns_containing(env, CANARY)

    async with env.factory() as session, session.begin():
        report = await purge_conversation(
            session,
            account_id=account_id,
            conversation_id=first,
            business_chat_id=CONTACT_CHAT,
            clock=env.clock,
        )
    assert report.summaries == 2
    assert await _columns_containing(env, CANARY) == []
    assert "app.conversation_summaries.summary" in await _columns_containing(env, "OTHER-SYNTH")

    async with env.factory() as session, session.begin():
        report = await delete_managed_data(session, account_id=account_id, clock=env.clock)
    assert report.summaries == 2
    assert await _columns_containing(env, "OTHER-SYNTH") == []


async def test_memory_items_go_with_purge_and_managed_data_deletion(env: Env) -> None:
    """S03.3's content column `memory_items.content`: purging a chat takes
    its notes and its contact's; managed-data deletion takes every note about
    a contact or chat; the owner's own items stay (M03 D71)."""
    from assistant.memory import items

    await connect(env)
    await deliver(env, business_message(message_text="Synthetic one"))
    await deliver(
        env,
        business_message(from_id=7000000006, chat_id=7000000006, message_text="Synthetic two"),
    )
    account_id = await _account_id(env, OWNER)
    async with env.factory() as session:
        rows = {
            int(chat): (int(conv), int(contact))
            for chat, conv, contact in (
                await session.execute(
                    text("SELECT business_chat_id, id, contact_id FROM app.conversations")
                )
            ).all()
        }
    (first, first_contact), (second, second_contact) = rows[CONTACT_CHAT], rows[7000000006]
    async with env.factory() as session, session.begin():
        for content, target in (
            (f"{CANARY} contact note", {"contact_id": first_contact}),
            (f"{CANARY} chat note", {"conversation_id": first}),
            ("OTHER-SYNTH contact note", {"contact_id": second_contact}),
            ("OTHER-SYNTH chat note", {"conversation_id": second}),
            ("OWNER-SYNTH own fact", {}),
        ):
            await items.save(
                session,
                account_id=account_id,
                content=content,
                kind="fact",
                actor="owner",
                now=env.clock.now(),
                **target,  # type: ignore[arg-type]
            )
    assert "app.memory_items.content" in await _columns_containing(env, CANARY)

    async with env.factory() as session, session.begin():
        report = await purge_conversation(
            session,
            account_id=account_id,
            conversation_id=first,
            business_chat_id=CONTACT_CHAT,
            clock=env.clock,
        )
    assert report.memory_items == 2
    assert await _columns_containing(env, CANARY) == []
    assert "app.memory_items.content" in await _columns_containing(env, "OTHER-SYNTH")

    async with env.factory() as session, session.begin():
        report = await delete_managed_data(session, account_id=account_id, clock=env.clock)
    assert report.memory_items == 2
    assert await _columns_containing(env, "OTHER-SYNTH") == []
    assert "app.memory_items.content" in await _columns_containing(env, "OWNER-SYNTH")


async def test_deletion_leaves_other_accounts_intact(migrated_url: str) -> None:
    from tests.integration.business_env import connection_update

    other = 7000000002
    other_chat = 7000000077
    async with environment(migrated_url, owner_ids=frozenset({OWNER, other})) as env:
        await connect(env)
        await deliver(env, business_message(message_text=f"{CANARY} mine"))
        await onboard(env, other)
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
