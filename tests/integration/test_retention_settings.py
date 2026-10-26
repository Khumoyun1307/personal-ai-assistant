"""S03.5 retention settings and sweeps (PRD R-07, FR-PRV-03; ARCHITECTURE §10.4).

The required S03.5 test (IMPLEMENTATION_PLAN §5), findable by name:

- a retention change takes effect at the next sweep —
  `test_a_retention_change_takes_effect_at_the_next_sweep`.

Plus: overrides are per account; values outside the allowed ranges are
refused and stored garbage reads as the shortest period; the inbound metadata
row goes after 30 days; the /settings retention screen asks before
shortening; `update_preferences` with a retention value is always a card;
reply language, tone and style are settings.
Synthetic text only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts import preferences
from assistant.accounts.models import Account, AuditLogEntry
from assistant.accounts.service import record_control_turn
from assistant.agent.models import ToolConfirmation
from assistant.inbox.models import InboundUpdate
from assistant.llm.port import ToolResultItem
from assistant.privacy import retention_policy
from assistant.privacy.retention import sweep_all
from assistant.telegram.control import panels
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import MEMORY_FLAGS, applier, args, ask, one_id, runtime
from tests.integration.business_env import (
    NOW,
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    connection_update,
    deliver,
    environment,
    private_message,
)
from tests.integration.test_conversation_modes import card, keyboard_labels, keyboard_ops

pytestmark = pytest.mark.integration

OTHER_OWNER = 7000000002


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url, owner_ids=frozenset({OWNER, OTHER_OWNER})) as built:
        yield built


async def account(env: Env, telegram_user_id: int = OWNER) -> Account:
    async with env.factory() as session:
        return (
            await session.execute(
                select(Account).where(Account.telegram_user_id == telegram_user_id)
            )
        ).scalar_one()


async def set_retention(env: Env, telegram_user_id: int = OWNER, **days: int) -> None:
    async with env.factory() as session, session.begin():
        row = (
            await session.execute(
                select(Account).where(Account.telegram_user_id == telegram_user_id)
            )
        ).scalar_one()
        await preferences.update(session, row, retention=days, actor="owner", clock=env.clock)


async def texts_of(env: Env, telegram_user_id: int = OWNER) -> list[str | None]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT m.text FROM app.messages m JOIN app.accounts a ON a.id = m.account_id "
                "WHERE a.telegram_user_id = :u ORDER BY m.id"
            ),
            {"u": telegram_user_id},
        )
        return [r[0] for r in rows.all()]


async def received_at_now(env: Env) -> None:
    """Inbound rows carry the database's receipt time; pin it to the FakeClock."""
    async with env.factory() as session, session.begin():
        await session.execute(text("UPDATE app.inbound_updates SET received_at = :t"), {"t": NOW})


async def tap(env: Env, kind: str, op: str) -> None:
    row = await card(env, kind)
    assert row is not None, f"no open {kind} card"
    await deliver(env, callback(f"{kind}:{row.token}:{op}", message_id=row.prompt_message_id))


async def test_a_retention_change_takes_effect_at_the_next_sweep(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text="SYNTH-KEEP one"))
    owner = await account(env)
    async with env.factory() as session, session.begin():
        await record_control_turn(
            session, account_id=owner.id, role="owner", text="SYNTH turn", created_at=NOW
        )
    env.clock.set(NOW + timedelta(days=40))

    # Default (90 days): a 40-day-old message stays.
    await sweep_all(env.factory, env.clock)
    assert await texts_of(env) == ["SYNTH-KEEP one"]

    await set_retention(env, messages=30, control_chat=30)
    # The change itself deletes nothing...
    assert await texts_of(env) == ["SYNTH-KEEP one"]
    # ...the next sweep applies it.
    report = await sweep_all(env.factory, env.clock)
    assert report.message_contents == 1
    assert report.control_turn_texts == 1
    assert await texts_of(env) == [None]
    async with env.factory() as session:
        assert (
            await session.execute(text("SELECT text FROM app.control_turns"))
        ).scalar_one() is None


async def test_a_shorter_raw_update_retention_clears_payloads_sooner(env: Env) -> None:
    await connect(env)
    update = business_message(message_id=1, message_text="SYNTH payload")
    await deliver(env, update)
    await received_at_now(env)
    env.clock.set(NOW + timedelta(days=4))
    await sweep_all(env.factory, env.clock)
    async with env.factory() as session:
        row = (
            await session.execute(
                select(InboundUpdate).where(InboundUpdate.update_id == update["update_id"])
            )
        ).scalar_one()
    assert row.payload is not None  # 7 days by default
    await set_retention(env, raw_updates=3)
    await sweep_all(env.factory, env.clock)
    async with env.factory() as session:
        cleared = (
            await session.execute(
                text("SELECT payload IS NULL FROM app.inbound_updates WHERE update_id = :u"),
                {"u": update["update_id"]},
            )
        ).scalar_one()
    assert cleared


async def test_one_accounts_override_does_not_touch_another_account(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text="SYNTH mine"))
    await deliver(env, private_message(OTHER_OWNER, "/start"))
    await deliver(env, private_message(OTHER_OWNER, "yes"))
    await deliver(env, connection_update(user_id=OTHER_OWNER, connection_id="synthetic-conn-b"))
    await deliver(
        env,
        business_message(
            "synthetic-conn-b", from_id=7000000077, chat_id=7000000077, message_text="SYNTH theirs"
        ),
    )
    await set_retention(env, messages=14)
    env.clock.set(NOW + timedelta(days=20))
    await sweep_all(env.factory, env.clock)
    assert await texts_of(env) == [None]
    assert await texts_of(env, OTHER_OWNER) == ["SYNTH theirs"]


async def test_retention_values_outside_the_allowed_ranges_are_refused(env: Env) -> None:
    await connect(env)
    for days in ({"messages": 13}, {"messages": 91}, {"raw_updates": 8}, {"unknown": 30}):
        with pytest.raises(preferences.PreferenceError):
            await set_retention(env, **days)
    owner = await account(env)
    assert retention_policy.days(owner.settings, "messages") == 90
    # Garbage stored by hand never widens or breaks the sweep: see
    # test_an_invalid_stored_retention_reads_as_the_shortest_period.


async def test_inbound_metadata_rows_go_after_thirty_days(env: Env) -> None:
    await connect(env)
    old = business_message(message_id=1, message_text="SYNTH old")
    await deliver(env, old)
    async with env.factory() as session, session.begin():
        session.add(
            InboundUpdate(
                bot_id=env.bot_id,
                update_id=9_999_001,
                kind="message",
                payload={"update_id": 9_999_001},
                status="pending",
                received_at=NOW,
            )
        )
    await received_at_now(env)
    env.clock.set(NOW + timedelta(days=31))
    report = await sweep_all(env.factory, env.clock)
    assert report.inbound_rows >= 1
    async with env.factory() as session:
        remaining = (await session.execute(select(InboundUpdate.update_id))).scalars().all()
    assert old["update_id"] not in remaining
    assert 9_999_001 in remaining  # a pending row is never dropped
    # The message it became is kept as a tombstone of its own (purge is 90 days).
    async with env.factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM app.messages"))).scalar_one() == 1


# ------------------------------------------------------------ /settings
async def test_the_settings_panel_shows_reply_and_retention_settings(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    shown = env.sender.sends[-1]
    assert "Replies:" in shown["text"]
    assert "Messages kept: 90 days" in shown["text"]
    assert {"replies", "retention", "delall"} <= set(keyboard_ops(shown["reply_markup"]))

    await tap(env, panels.SETTINGS_CARD, "replies")
    replies = env.sender.edits[-1]
    assert "t_formal" in keyboard_ops(replies["reply_markup"])
    await tap(env, panels.REPLY_CARD, "t_formal")
    await tap(env, panels.REPLY_CARD, "l_ru")
    owner = await account(env)
    assert preferences.current(owner)[preferences.FIELD_REPLY_TONE] == "formal"
    assert preferences.current(owner)[preferences.FIELD_REPLY_LANGUAGE] == "ru"
    assert "Formal ✓" in keyboard_labels(env.sender.edits[-1]["reply_markup"])
    async with env.factory() as session:
        audited = (
            (
                await session.execute(
                    select(AuditLogEntry.details).where(
                        AuditLogEntry.event == "preferences_changed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audited) == 2


async def test_shortening_retention_on_the_panel_asks_first(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    await tap(env, panels.SETTINGS_CARD, "retention")
    screen = env.sender.edits[-1]
    assert "m_30" in keyboard_ops(screen["reply_markup"])
    await tap(env, panels.RETENTION_CARD, "m_30")
    asked = env.sender.edits[-1]
    assert "older than 30 days" in asked["text"]
    assert retention_policy.days((await account(env)).settings, "messages") == 90  # not yet
    await tap(env, panels.RETENTION_CONFIRM_CARD, "cancel")
    assert retention_policy.days((await account(env)).settings, "messages") == 90
    await tap(env, panels.RETENTION_CARD, "m_30")
    await tap(env, panels.RETENTION_CONFIRM_CARD, "m_30")
    assert retention_policy.days((await account(env)).settings, "messages") == 30
    # Back to a longer period applies at once (nothing more is deleted).
    await tap(env, panels.RETENTION_CARD, "m_90")
    assert retention_policy.days((await account(env)).settings, "messages") == 90
    # A forged value outside the choices does nothing.
    await tap(env, panels.RETENTION_CARD, "m_5")
    assert retention_policy.days((await account(env)).settings, "messages") == 90


# ------------------------------------------------------------ update_preferences
def results(request: Any) -> list[dict[str, Any]]:
    return [json.loads(item.output) for item in request.items if isinstance(item, ToolResultItem)]


def prefs_args(**values: Any) -> str:
    base: dict[str, Any] = {
        "preferred_language": None,
        "timezone": None,
        "default_mode": None,
        "reply_language": None,
        "reply_tone": None,
        "reply_style": None,
        "message_retention_days": None,
        "control_chat_retention_days": None,
        "raw_update_retention_days": None,
    }
    base.update(values)
    return args(**base)


async def test_update_preferences_with_a_retention_value_always_needs_a_card(env: Env) -> None:
    await connect(env)
    turn = await ask(env, "Keep messages for 30 days and reply formally")
    fake = FakeLLM(
        tool_response(
            ("c1", "update_preferences", prefs_args(message_retention_days=30)),
            ("c2", "update_preferences", prefs_args(reply_tone="formal", reply_style="detailed")),
        ),
        text_response("Done; confirm the retention change."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=MEMORY_FLAGS).run(turn)
    held, applied = results(fake.requests[1])
    assert held["status"] == "awaiting_confirmation"
    assert applied["status"] == "ok"
    owner = await account(env)
    assert retention_policy.days(owner.settings, "messages") == 90
    assert preferences.current(owner)[preferences.FIELD_REPLY_TONE] == "formal"
    assert preferences.current(owner)[preferences.FIELD_REPLY_STYLE] == "detailed"
    async with env.factory() as session:
        row = (await session.execute(select(ToolConfirmation))).scalar_one()
    assert row.tool_name == "update_preferences"
    assert row.args is not None
    assert row.args["message_retention_days"] == 30
    assert await one_id(env, "SELECT count(*) FROM app.messages") == 0  # nothing touched


# ------------------------------------------------------------ review (S03.5)
async def test_a_retention_card_says_whether_the_change_deletes_anything(env: Env) -> None:
    """General review item 12: only a shorter period deletes; a longer one or
    the same one says so instead of "cannot be restored"."""
    await connect(env)
    await set_retention(env, messages=30)
    turn = await ask(env, "Change how long things are kept")
    fake = FakeLLM(
        tool_response(
            ("c1", "update_preferences", prefs_args(message_retention_days=60)),
            ("c2", "update_preferences", prefs_args(message_retention_days=14)),
            ("c3", "update_preferences", prefs_args(message_retention_days=30)),
            ("c4", "update_preferences", prefs_args(control_chat_retention_days=14)),
        ),
        text_response("Please confirm."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=MEMORY_FLAGS).run(turn)
    longer, shorter, same, chat = channel.sends[:4]
    assert "cannot be restored" not in longer
    assert "30 → 60 days" in longer
    assert "nothing is deleted" in longer.lower()
    assert "already deleted does not come back" in longer
    assert "cannot be restored" in shorter
    assert "older than 14 days" in shorter
    assert "cannot be restored" not in same
    assert "already" in same.lower()
    assert "this chat older than" not in chat.lower()  # grammar (review nit)
    assert "cannot be restored" in chat


async def test_a_retention_change_has_no_undo(env: Env) -> None:
    """Safety review item 7 / general item 11: an Undo would change a
    retention period without the card (a revert to a shorter period deletes
    at the next sweep; a revert to a longer one brings nothing back). The
    echo of a confirmed retention change carries no Undo and says how to
    change it."""
    await connect(env)
    await set_retention(env, messages=30)
    turn = await ask(env, "Keep messages for 60 days and reply formally")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "update_preferences",
                prefs_args(message_retention_days=60, reply_tone="formal"),
            ),
        ),
        text_response("Please confirm."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=MEMORY_FLAGS).run(turn)
    async with env.factory() as session:
        held = (await session.execute(select(ToolConfirmation))).scalar_one()
        token = (
            await session.execute(
                text("SELECT token FROM app.pending_inputs WHERE kind = 'tool_confirm'")
            )
        ).scalar_one()
    await deliver(env, callback(f"tool_confirm:{token}:confirm"))
    applied = FakeChannel()
    outcome = await applier(env, applied, flags=MEMORY_FLAGS).apply(held.id)
    assert outcome.status == "applied"
    assert retention_policy.days((await account(env)).settings, "messages") == 60
    echo = next(i for i, sent in enumerate(applied.sends) if "Settings changed" in sent)
    assert applied.buttons[echo] == ()
    assert "/settings" in applied.sends[echo]
    assert "already deleted does not come back" in applied.sends[echo]
    async with env.factory() as session:
        undo_cards = await session.execute(
            text("SELECT count(*) FROM app.pending_inputs WHERE kind = 'undo'")
        )
        assert undo_cards.scalar_one() == 0


async def test_an_old_undo_of_a_retention_change_changes_nothing(env: Env) -> None:
    """Defensive: an audit row with a retention change is not reversible."""
    from assistant.memory import undo

    await connect(env)
    await set_retention(env, messages=30)
    async with env.factory() as session:
        change_id = (
            await session.execute(
                select(AuditLogEntry.id).where(AuditLogEntry.event == "preferences_changed")
            )
        ).scalar_one()
    owner = await account(env)
    async with env.factory() as session, session.begin():
        outcome = await undo.undo(
            session, account_id=owner.id, change_id=change_id, clock=env.clock
        )
    assert outcome.status is undo.UndoStatus.NOT_FOUND
    assert retention_policy.days((await account(env)).settings, "messages") == 30


async def test_the_panel_says_a_longer_period_brings_nothing_back(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    await tap(env, panels.SETTINGS_CARD, "retention")
    assert "already deleted does not come back" in env.sender.edits[-1]["text"]


def test_an_invalid_stored_retention_reads_as_the_shortest_period() -> None:
    """Safety review item 7: fail safe. Unset is the default; a value that is
    set but invalid (hand-written, left over) is the shortest period."""
    assert retention_policy.days({}, "messages") == 90
    assert retention_policy.days({"retention_days": {}}, "messages") == 90
    assert retention_policy.days({"retention_days": {"messages": None}}, "messages") == 90
    for garbage in ("x", 5, 9999, [1], True):
        assert retention_policy.days({"retention_days": {"messages": garbage}}, "messages") == 14
    assert retention_policy.days({"retention_days": "junk"}, "raw_updates") == 1
