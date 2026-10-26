"""S03.3 general review (2026-10-26): retention robustness, honest outcomes
of changes that were already so, preference races, Undo edge cases, and the
weak-test gaps the review named. Synthetic text only; FakeLLM only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from assistant.accounts import preferences, service
from assistant.accounts.models import Account, PendingInput
from assistant.agent.models import ToolConfirmation
from assistant.agent.tools.memory import RememberArgs, remember_tool
from assistant.agent.tools.modes import UpdatePreferencesArgs, update_preferences_tool
from assistant.agent.tools.registry import ToolContext, ToolServices
from assistant.conversations.models import Conversation
from assistant.memory import items, summaries
from assistant.memory.models import MemoryItem
from assistant.privacy import retention
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import (
    MEMORY_FLAGS,
    READ_FLAGS,
    S035_PREFS_UNCHANGED,
    applier,
    ask,
    one_id,
    runtime,
)
from tests.integration.business_env import (
    CONTACT,
    NOW,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    connection_update,
    deliver,
    environment,
    grant_managed,
    private_message,
)

pytestmark = pytest.mark.integration

FLAGS = READ_FLAGS | MEMORY_FLAGS
SENT = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
FORWARD = {
    "forward_origin": {"type": "hidden_user", "sender_user_name": "Synthetic", "date": T_BASE}
}


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


def remember_args(content: str, **extra: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "content": content,
        "kind": "fact",
        "contact_id": None,
        "conversation_id": None,
        "applies_to_replies": None,
        "replaces_memory_id": None,
    }
    values.update(extra)
    return values


async def account_id(env: Env) -> int:
    return await one_id(env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER)


async def conversation_id(env: Env, chat_id: int = CONTACT) -> int:
    return await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=chat_id
    )


async def ctx(env: Env, channel: FakeChannel) -> ToolContext:
    return ToolContext(
        account_id=await account_id(env),
        run_id=0,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory, clock=env.clock, monthly_budget_usd=None, channel=channel
        ),
    )


async def tap_undo(env: Env, channel: FakeChannel, index: int) -> None:
    [button] = channel.buttons[index]
    await deliver(env, callback(button.callback_data))


async def seed_old_messages(env: Env, *, managed: bool = True) -> int:
    await connect(env)
    if managed:
        await grant_managed(env)
    await deliver(
        env,
        business_message(message_id=1, message_text="SYNTH old", date=int(SENT.timestamp())),
    )
    return await conversation_id(env)


class FailingScheduler:
    async def defer(self, task: str, **kwargs: Any) -> int | None:
        raise RuntimeError("synthetic scheduler outage")


# ------------------------------------------------------------ 1. rollup isolation
async def test_a_failing_rollup_never_stops_the_purge(env: Env) -> None:
    conversation = await seed_old_messages(env)
    env.clock.set(NOW + timedelta(days=91))
    report = await retention.sweep_all(
        env.factory, env.clock, scheduler=FailingScheduler(), rollups_enabled=True
    )
    assert report.message_contents == 1
    async with env.factory() as session:
        texts = (
            (
                await session.execute(
                    text("SELECT text FROM app.messages WHERE conversation_id = :c"),
                    {"c": conversation},
                )
            )
            .scalars()
            .all()
        )
    assert texts == [None]


# ------------------------------------------------------------ 8. rollup filters and order
async def test_the_rollup_skips_ignore_and_bot_chats(env: Env) -> None:
    conversation = await seed_old_messages(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Conversation).where(Conversation.id == conversation).values(mode="ignore")
        )
    env.clock.set(NOW + timedelta(days=81))
    async with env.factory() as session:
        assert await summaries.rollup_candidates(session, NOW) == []
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Conversation).where(Conversation.id == conversation).values(mode="observe")
        )
        await session.execute(
            text(
                "UPDATE app.contacts SET is_bot = true WHERE id = "
                "(SELECT contact_id FROM app.conversations WHERE id = :c)"
            ),
            {"c": conversation},
        )
    async with env.factory() as session:
        assert await summaries.rollup_candidates(session, NOW) == []


async def test_the_rollup_does_not_queue_a_chat_set_to_ignore_meanwhile(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """abe3e22: a chat set to Ignore between the candidate read and the flag
    update is neither flagged nor queued, nor counted."""
    conversation = await seed_old_messages(env)
    real = summaries.rollup_candidates

    async def then_ignored(session: Any, *a: Any, **k: Any) -> Any:
        found = await real(session, *a, **k)
        async with env.factory() as other, other.begin():
            await other.execute(
                update(Conversation).where(Conversation.id == conversation).values(mode="ignore")
            )
        return found

    monkeypatch.setattr(retention, "rollup_candidates", then_ignored)
    env.clock.set(NOW + timedelta(days=81))
    assert env.scheduler is not None
    queued = await retention.schedule_rollups(env.factory, env.clock, env.scheduler)
    assert queued == 0
    async with env.factory() as session:
        jobs = (
            await session.execute(
                text(
                    "SELECT count(*) FROM procrastinate.procrastinate_jobs "
                    "WHERE task_name = 'memory.refresh_summary'"
                )
            )
        ).scalar_one()
    assert jobs == 0


async def test_the_rollup_flag_counts_from_the_oldest_unsummarized_content(env: Env) -> None:
    """The flag moves back to the expiring content (least()), so the refresh
    is due at once instead of waiting out the debounce."""
    conversation = await seed_old_messages(env)
    later = NOW + timedelta(days=80)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation)
            .values(pending_ai_since=later)
        )
    env.clock.set(NOW + timedelta(days=81))
    assert env.scheduler is not None
    await retention.schedule_rollups(env.factory, env.clock, env.scheduler)
    async with env.factory() as session:
        flag = (
            await session.execute(
                select(Conversation.pending_ai_since).where(Conversation.id == conversation)
            )
        ).scalar_one()
    assert flag is not None
    assert flag < later


async def test_the_rollup_takes_the_oldest_expiring_chats_first(env: Env) -> None:
    await connect(env)
    await grant_managed(env)
    await deliver(
        env,
        business_message(
            message_id=1,
            message_text="SYNTH newer",
            date=int((SENT + timedelta(days=2)).timestamp()),
        ),
    )
    await deliver(
        env,
        business_message(
            message_id=2,
            from_id=OTHER_CONTACT,
            chat_id=OTHER_CONTACT,
            message_text="SYNTH older",
            date=int(SENT.timestamp()),
        ),
    )
    newer, older = await conversation_id(env), await conversation_id(env, OTHER_CONTACT)
    assert newer < older  # the older content sits in the higher id
    async with env.factory() as session, session.begin():
        await session.execute(text("UPDATE app.messages SET updated_at = sent_at"))
    async with env.factory() as session:
        found = await summaries.rollup_candidates(session, NOW + timedelta(days=30), limit=1)
    assert [c for c, _ in found] == [older]


# ------------------------------------------------------------ 3. already so
async def test_a_confirmed_change_that_is_already_so_is_reported_honestly(env: Env) -> None:
    """A held "ignore this chat", confirmed after the owner already ignored
    it from the panel: nothing to change, and the owner is told so — not
    "failed", not a bare "Done"."""
    await seed_old_messages(env)
    conversation = await conversation_id(env)
    turn = await ask(env, "Ignore it", **FORWARD)
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "set_conversation_mode",
                json.dumps({"conversation_id": conversation, "contact_id": None, "mode": "ignore"}),
            )
        ),
        text_response("Asked you."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Conversation).where(Conversation.id == conversation).values(mode="ignore")
        )
        row = (await session.execute(select(ToolConfirmation))).scalar_one()
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm"))
        ).scalar_one()
    await deliver(env, callback(f"tool_confirm:{card.token}:confirm"))
    channel = FakeChannel()
    outcome = await applier(env, channel, flags=FLAGS).apply(row.id)
    assert outcome.status == "applied"
    assert "already" in channel.sends[-1]
    assert "could not" not in channel.sends[-1]


# ------------------------------------------------------------ 4. newest notes kept
async def test_contact_facing_keeps_the_newest_notes(env: Env) -> None:
    conversation = await seed_old_messages(env)
    contact = await one_id(
        env, "SELECT contact_id FROM app.conversations WHERE id = :c", c=conversation
    )
    async with env.factory() as session, session.begin():
        for n in range(31):
            await items.save(
                session,
                account_id=await account_id(env),
                content=f"SYNTH note {n:02d}",
                kind="fact",
                contact_id=contact,
                actor="owner",
                now=NOW + timedelta(minutes=n),
            )
    async with env.factory() as session:
        loaded = await items.contact_facing(
            session, await account_id(env), conversation, include_owner_preferences=False
        )
    contents = [i.content for i in loaded]
    assert len(contents) == 30
    assert contents[0] == "SYNTH note 01"  # the oldest one dropped
    assert contents[-1] == "SYNTH note 30"  # newest kept, chronological order


# ------------------------------------------------------------ 6. preference races
async def test_a_concurrent_settings_change_is_not_lost(env: Env) -> None:
    await connect(env)
    mine = await account_id(env)
    async with env.factory() as session_a, session_a.begin():
        account_a = await session_a.get(Account, mine)
        assert account_a is not None
        async with env.factory() as session_b, session_b.begin():
            account_b = await session_b.get(Account, mine)
            assert account_b is not None
            await service.set_managed_mode(session_b, account_b, service.MANAGED_MODE_STOPPED)
        await preferences.update(
            session_a, account_a, new_default_mode="ignore", actor="owner", clock=env.clock
        )
    async with env.factory() as session:
        settings = (await session.execute(select(Account.settings))).scalar_one()
    assert settings.get(service.MANAGED_MODE_KEY) == service.MANAGED_MODE_STOPPED
    assert settings.get(preferences.DEFAULT_MODE_KEY) == "ignore"


async def test_an_undo_waits_for_a_concurrent_settings_change(env: Env) -> None:
    """Undo's check-then-revert holds the account lock: a change committed
    meanwhile is seen and never overwritten."""
    await connect(env)
    channel = FakeChannel()
    await update_preferences_tool(
        await ctx(env, channel),
        UpdatePreferencesArgs(
            preferred_language=None,
            timezone="Asia/Tashkent",
            default_mode=None,
            **S035_PREFS_UNCHANGED,
        ),
    )
    mine = await account_id(env)
    async with env.factory() as holder, holder.begin():
        account = await holder.get(Account, mine, with_for_update={"key_share": True})
        assert account is not None
        undo = asyncio.create_task(tap_undo(env, channel, 0))
        done, _ = await asyncio.wait({undo}, timeout=1.0)
        assert not done  # waiting on the lock
        account.timezone = "Europe/Paris"
    await undo
    async with env.factory() as session:
        assert (await session.execute(select(Account.timezone))).scalar_one() == "Europe/Paris"
    assert any("changed again" in t for t in env.sender.texts)


# ------------------------------------------------------------ 9. partial revert
async def test_a_partial_preference_undo_says_what_it_restored(env: Env) -> None:
    await connect(env)
    channel = FakeChannel()
    await update_preferences_tool(
        await ctx(env, channel),
        UpdatePreferencesArgs(
            preferred_language="ru",
            timezone="Asia/Tashkent",
            default_mode=None,
            **S035_PREFS_UNCHANGED,
        ),
    )
    await update_preferences_tool(
        await ctx(env, channel),
        UpdatePreferencesArgs(
            preferred_language="en", timezone=None, default_mode=None, **S035_PREFS_UNCHANGED
        ),
    )
    await tap_undo(env, channel, 0)
    async with env.factory() as session:
        account = (await session.execute(select(Account))).scalar_one()
    assert (account.timezone, account.locale_pref) == ("UTC", "en")
    last = env.sender.texts[-1]
    assert "Partly undone" in last
    assert "timezone" in last
    assert "language" in last


# ------------------------------------------------------------ Undo of corrections
async def test_a_correction_replaces_the_note_and_its_undo_restores_it(env: Env) -> None:
    await connect(env)
    channel = FakeChannel()
    context = await ctx(env, channel)
    first = await remember_tool(context, RememberArgs.model_validate(remember_args("SYNTH v1")))
    second = await remember_tool(
        context,
        RememberArgs.model_validate(
            remember_args("SYNTH v2", replaces_memory_id=first.data["memory_id"])
        ),
    )
    assert second.data["replaced_memory_id"] == first.data["memory_id"]

    async def statuses() -> dict[str, str]:
        async with env.factory() as session:
            rows = (await session.execute(select(MemoryItem))).scalars().all()
        return {row.content: row.status for row in rows}

    assert await statuses() == {"SYNTH v1": "archived", "SYNTH v2": "active"}
    await tap_undo(env, channel, 1)
    assert await statuses() == {"SYNTH v1": "active", "SYNTH v2": "archived"}


async def test_undo_after_the_sweep_says_the_note_is_gone(env: Env) -> None:
    await connect(env)
    channel = FakeChannel()
    context = await ctx(env, channel)
    saved = await remember_tool(context, RememberArgs.model_validate(remember_args("SYNTH x")))
    async with env.factory() as session, session.begin():
        await items.forget(
            session,
            account_id=await account_id(env),
            memory_id=saved.data["memory_id"],
            actor="owner",
            now=env.clock.now(),
        )
    env.clock.advance(hours=25)
    async with env.factory() as session, session.begin():
        assert await items.sweep_forgotten(session, env.clock.now()) == 1
    env.clock.set(NOW)  # the card itself is still live
    await tap_undo(env, channel, 0)
    assert any("no longer exists" in t for t in env.sender.texts)


# ------------------------------------------------------------ 7. timezones, nits
@pytest.mark.parametrize("name", ["localtime", "Factory", "posixrules", "UTC "])
def test_only_iana_timezones_are_accepted(name: str) -> None:
    if name.strip() == "UTC":
        assert preferences.check_timezone(name) == "UTC"
        return
    with pytest.raises(preferences.PreferenceError):
        preferences.check_timezone(name)


@pytest.mark.parametrize(
    ("given", "stored"), [("EN", "en"), ("Uz-latn", "uz-Latn"), ("ru-RU", "ru-RU"), ("uz", "uz")]
)
def test_language_codes_are_normalised(given: str, stored: str) -> None:
    assert preferences.check_language(given) == stored


async def test_settings_timezone_without_a_value_shows_the_panel(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings timezone"))
    assert env.sender.sends[-1]["reply_markup"] is not None
    assert "New chats start in" in env.sender.sends[-1]["text"]


# ------------------------------------------------------------ weak test: lists
async def test_a_list_cannot_open_another_accounts_real_chat(migrated_url: str) -> None:
    other = 7000000002
    other_chat = 7000000077
    async with environment(migrated_url, owner_ids=frozenset({OWNER, other})) as env:
        await connect(env)
        await deliver(env, business_message(message_id=1, message_text="SYNTH mine"))
        await deliver(env, private_message(other, "/start"))
        await deliver(env, private_message(other, "yes"))
        await deliver(env, connection_update(user_id=other, connection_id="synthetic-conn-b"))
        await deliver(
            env,
            business_message(
                "synthetic-conn-b", from_id=other_chat, chat_id=other_chat, message_text="SYNTH b"
            ),
        )
        theirs = await conversation_id(env, other_chat)
        await deliver(env, private_message(OWNER, "/settings"))
        async with env.factory() as session:
            card = (
                (
                    await session.execute(
                        select(PendingInput)
                        .where(PendingInput.kind == "settings")
                        .order_by(PendingInput.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert card is not None
        await deliver(env, callback(f"settings:{card.token}:lobserve"))
        async with env.factory() as session:
            listing = (
                (
                    await session.execute(
                        select(PendingInput)
                        .where(PendingInput.kind == "chat_list")
                        .order_by(PendingInput.id.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert listing is not None
        sends_before = len(env.sender.sends)
        await deliver(env, callback(f"chat_list:{listing.token}:o_{theirs}"))
        assert len(env.sender.sends) == sends_before
        assert env.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
