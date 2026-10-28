"""S05.2P: the M05 settings panels and the contact priority row (FR-CTL-10,
FR-TRI-04; PRD Q-07).

Required tests, findable by name: `test_settings_has_an_alerts_and_digest_button`,
`test_alerts_screen_shows_the_defaults`, `test_choosing_quiet_hours_saves_and_ticks_it`,
`test_turning_quiet_hours_off_saves_off`, `test_urgent_bypass_toggles`,
`test_choosing_a_digest_time_saves_it`, `test_turning_the_digest_off_saves_off`,
`test_choosing_a_cap_saves_it`, `test_back_returns_to_settings`,
`test_an_unknown_op_answers_no_longer_active`,
`test_a_stale_or_foreign_alerts_card_answers_no_longer_active`,
`test_the_panel_shows_the_contacts_priority_ticked`,
`test_choosing_vip_on_the_panel_sets_the_contacts_priority_and_audits_it`,
`test_a_bot_chat_panel_has_no_priority_row`,
`test_each_change_writes_one_content_free_audit_row`.

Buttons only, no model: every press goes through a card (`pending_inputs`) and
the gateway; the stored values are the ones `accounts/preferences.py` validates.
Synthetic text only.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts import preferences
from assistant.accounts.models import Account, AuditLogEntry
from assistant.conversations import priority, visibility
from assistant.conversations.models import Contact
from assistant.conversations.visibility import ConversationPanel
from assistant.db.uow import transaction
from assistant.telegram.control import panels
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    onboard,
    private_message,
)
from tests.integration.p2_factories import seed_account, seed_conversation
from tests.integration.test_conversation_modes import (
    card,
    conversation_row,
    keyboard_labels,
    keyboard_ops,
    open_panel,
)

pytestmark = pytest.mark.integration

OTHER_OWNER = 7000000002
BOT_CHAT = 7000000007
EXPIRED = "This button is no longer active."
#: Every button of the alerts screen, in order (ops stay short: callback data
#: is at most 64 bytes).
ALERTS_OPS = [
    "q_off",
    "q_2207",
    "q_2308",
    "q_0008",
    "b_on",
    "b_off",
    "d_off",
    "d_0800",
    "d_1200",
    "d_1800",
    "d_2100",
    "c_3",
    "c_6",
    "c_10",
    "c_20",
    "back",
]
KAMOLA = "Kamola"


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


async def current(env: Env, telegram_user_id: int = OWNER) -> dict[str, Any]:
    return preferences.current(await account(env, telegram_user_id))


async def tap(env: Env, kind: str, op: str) -> None:
    row = await card(env, kind)
    assert row is not None, f"no open {kind} card"
    await deliver(env, callback(f"{kind}:{row.token}:{op}", message_id=row.prompt_message_id))


def ticked(markup: Any) -> list[str]:
    """The ops whose button carries the current-value tick."""
    return [
        button.callback_data.rsplit(":", 1)[1]
        for row in markup.inline_keyboard
        for button in row
        if button.text.endswith(" ✓")
    ]


async def open_alerts(env: Env) -> None:
    """An onboarded, connected owner on /settings → Alerts & digest…"""
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    await tap(env, panels.SETTINGS_CARD, "alerts")


async def kamola_panel(env: Env) -> int:
    """The owner's chat with Kamola, its panel open; her contact id."""
    await connect(env)
    await deliver(
        env, business_message(message_id=11, message_text="SYNTH hello", first_name=KAMOLA)
    )
    await open_panel(env)
    async with env.factory() as session:
        contact_id = await session.scalar(
            select(Contact.id).where(Contact.telegram_user_id == CONTACT)
        )
    assert contact_id is not None
    return contact_id


async def contact_priority(env: Env, contact_id: int) -> str:
    async with env.factory() as session:
        value = await session.scalar(select(Contact.priority).where(Contact.id == contact_id))
    assert value is not None
    return value


async def audit_details(env: Env, event: str) -> list[dict[str, Any]]:
    async with env.factory() as session:
        rows = await session.execute(
            select(AuditLogEntry.details)
            .where(AuditLogEntry.event == event)
            .order_by(AuditLogEntry.id)
        )
        return [dict(details or {}) for details in rows.scalars()]


async def changed_fields(env: Env) -> list[list[str]]:
    """The fields each `preferences_changed` audit row names, oldest first."""
    rows = await audit_details(env, preferences.EVENT_PREFERENCES_CHANGED)
    return [list(details["changes"]) for details in rows]


def last_answer(env: Env) -> Any:
    return env.sender.answered_callbacks[-1]["text"]


# ------------------------------------------------------------ /settings → Alerts & digest
async def test_settings_has_an_alerts_and_digest_button(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    shown = env.sender.sends[-1]
    assert "Alerts & digest…" in keyboard_labels(shown["reply_markup"])
    assert "alerts" in keyboard_ops(shown["reply_markup"])
    # The screens that were there stay (nothing else moved).
    assert {"replies", "retention", "delall", "lang"} <= set(keyboard_ops(shown["reply_markup"]))

    await tap(env, panels.SETTINGS_CARD, "alerts")
    screen = env.sender.edits[-1]
    assert screen["text"].startswith("<b>Alerts &amp; digest</b>")
    assert (await card(env, panels.ALERTS_CARD)) is not None
    assert last_answer(env) == "Settings"


async def test_alerts_screen_shows_the_defaults(env: Env) -> None:
    """PRD Q-07: quiet 23:00-08:00, bypass off, digest 21:00, cap 6 an hour."""
    await open_alerts(env)
    screen = env.sender.edits[-1]
    text = screen["text"]
    assert "Quiet hours: 23:00-08:00" in text
    assert 'urgent alerts wait too, unless "Urgent bypass" is on' in text
    assert "Urgent bypass: off" in text
    assert "Daily digest: 21:00" in text
    assert "Most alerts per hour: 6" in text
    assert "Times are in your timezone (UTC)." in text

    markup = screen["reply_markup"]
    assert keyboard_ops(markup) == ALERTS_OPS
    assert ticked(markup) == ["q_2308", "b_off", "d_2100", "c_6"]
    assert keyboard_labels(markup) == [
        "Off",
        "22–07",  # noqa: RUF001 (the buttons' en dash)
        "23–08 ✓",  # noqa: RUF001
        "00–08",  # noqa: RUF001
        "Bypass on",
        "Bypass off ✓",
        "Off",
        "08:00",
        "12:00",
        "18:00",
        "21:00 ✓",
        "3/h",
        "6/h ✓",
        "10/h",
        "20/h",
        "◀ Back",
    ]
    # Telegram's hard limit on callback data, and short ops.
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64
            assert len(button.callback_data.rsplit(":", 1)[1]) <= 12
    # Looking changes nothing: only the onboarding language was ever saved.
    assert await changed_fields(env) == [["preferred_language"]]
    assert {k: (await current(env))[k] for k in preferences.M05_DEFAULTS} == (
        preferences.M05_DEFAULTS
    )


async def test_the_alerts_screen_names_the_owners_timezone(env: Env) -> None:
    await connect(env)
    await preferences_set(env, timezone="Asia/Tashkent")
    await deliver(env, private_message(OWNER, "/settings"))
    await tap(env, panels.SETTINGS_CARD, "alerts")
    assert "Times are in your timezone (Asia/Tashkent)." in env.sender.edits[-1]["text"]


# ------------------------------------------------------------ quiet hours
@pytest.mark.parametrize(
    ("op", "stored"),
    [("q_2207", "22:00-07:00"), ("q_2308", "23:00-08:00"), ("q_0008", "00:00-08:00")],
)
async def test_choosing_quiet_hours_saves_and_ticks_it(env: Env, op: str, stored: str) -> None:
    await open_alerts(env)
    await preferences_set(env, quiet_hours="off")  # so every choice is a change
    await tap(env, panels.ALERTS_CARD, op)
    assert (await current(env))["quiet_hours"] == stored
    screen = env.sender.edits[-1]
    assert f"Quiet hours: {stored}" in screen["text"]
    assert ticked(screen["reply_markup"])[0] == op
    assert last_answer(env) == "Saved"


async def test_turning_quiet_hours_off_saves_off(env: Env) -> None:
    await open_alerts(env)
    await tap(env, panels.ALERTS_CARD, "q_off")
    assert (await current(env))["quiet_hours"] == "off"
    screen = env.sender.edits[-1]
    assert "Quiet hours: off" in screen["text"]
    assert ticked(screen["reply_markup"]) == ["q_off", "b_off", "d_2100", "c_6"]
    # The digest's "Off" is a different button: only the quiet row is ticked.
    assert (await current(env))["digest_time"] == "21:00"


# ------------------------------------------------------------ urgent bypass
async def test_urgent_bypass_toggles(env: Env) -> None:
    await open_alerts(env)
    await tap(env, panels.ALERTS_CARD, "b_on")
    assert (await current(env))["urgent_bypass"] is True
    on = env.sender.edits[-1]
    assert "Urgent bypass: on" in on["text"]
    assert ticked(on["reply_markup"]) == ["q_2308", "b_on", "d_2100", "c_6"]

    await tap(env, panels.ALERTS_CARD, "b_off")
    assert (await current(env))["urgent_bypass"] is False
    off = env.sender.edits[-1]
    assert "Urgent bypass: off" in off["text"]
    assert ticked(off["reply_markup"]) == ["q_2308", "b_off", "d_2100", "c_6"]


# ------------------------------------------------------------ digest
@pytest.mark.parametrize(
    ("op", "stored"),
    [("d_0800", "08:00"), ("d_1200", "12:00"), ("d_1800", "18:00"), ("d_2100", "21:00")],
)
async def test_choosing_a_digest_time_saves_it(env: Env, op: str, stored: str) -> None:
    await open_alerts(env)
    await preferences_set(env, digest_time="off")  # so every choice is a change
    await tap(env, panels.ALERTS_CARD, op)
    assert (await current(env))["digest_time"] == stored
    screen = env.sender.edits[-1]
    assert f"Daily digest: {stored}" in screen["text"]
    assert ticked(screen["reply_markup"])[2] == op
    assert last_answer(env) == "Saved"


async def test_turning_the_digest_off_saves_off(env: Env) -> None:
    await open_alerts(env)
    await tap(env, panels.ALERTS_CARD, "d_off")
    assert (await current(env))["digest_time"] == "off"
    screen = env.sender.edits[-1]
    assert "Daily digest: off" in screen["text"]
    assert ticked(screen["reply_markup"]) == ["q_2308", "b_off", "d_off", "c_6"]
    assert (await current(env))["quiet_hours"] == "23:00-08:00"


# ------------------------------------------------------------ cap
@pytest.mark.parametrize(("op", "stored"), [("c_3", 3), ("c_10", 10), ("c_20", 20), ("c_6", 6)])
async def test_choosing_a_cap_saves_it(env: Env, op: str, stored: int) -> None:
    await open_alerts(env)
    await preferences_set(env, alert_cap_per_hour=15)  # so every choice is a change
    await tap(env, panels.ALERTS_CARD, op)
    assert (await current(env))["alert_cap_per_hour"] == stored
    screen = env.sender.edits[-1]
    assert f"Most alerts per hour: {stored}" in screen["text"]
    assert ticked(screen["reply_markup"])[3] == op
    assert last_answer(env) == "Saved"


async def test_a_value_set_another_way_ticks_nothing_in_its_row(env: Env) -> None:
    """The agent's `update_preferences` may store a value the buttons do not
    offer (a cap of 15, quiet hours 01:00-09:00): the text shows it, no
    button in the row is ticked."""
    await open_alerts(env)
    await preferences_set(env, alert_cap_per_hour=15, quiet_hours="01:00-09:00")
    await tap(env, panels.ALERTS_CARD, "b_on")
    screen = env.sender.edits[-1]
    assert "Quiet hours: 01:00-09:00" in screen["text"]
    assert "Most alerts per hour: 15" in screen["text"]
    assert ticked(screen["reply_markup"]) == ["b_on", "d_2100"]


# ------------------------------------------------------------ navigation and refusals
async def test_back_returns_to_settings(env: Env) -> None:
    await open_alerts(env)
    await tap(env, panels.ALERTS_CARD, "back")
    screen = env.sender.edits[-1]
    assert screen["text"].startswith("<b>Settings</b>")
    assert "alerts" in keyboard_ops(screen["reply_markup"])
    assert (await card(env, panels.SETTINGS_CARD)) is not None
    assert last_answer(env) == "Settings"
    # Going back changes nothing.
    assert await changed_fields(env) == [["preferred_language"]]


@pytest.mark.parametrize("op", ["q_9999", "c_999", "d_2500", "b_maybe", "p_vip", "x"])
async def test_an_unknown_op_answers_no_longer_active(env: Env, op: str) -> None:
    await open_alerts(env)
    before = await current(env)
    edits = len(env.sender.edits)
    await tap(env, panels.ALERTS_CARD, op)
    assert last_answer(env) == EXPIRED
    assert len(env.sender.edits) == edits  # no screen, nothing saved
    assert await current(env) == before


async def test_a_stale_or_foreign_alerts_card_answers_no_longer_active(env: Env) -> None:
    await open_alerts(env)
    await onboard(env, OTHER_OWNER)
    row = await card(env, panels.ALERTS_CARD)
    data = f"{panels.ALERTS_CARD}:{row.token}:b_on"
    mine = await current(env)
    theirs = await current(env, OTHER_OWNER)

    # Another account presses this account's token.
    await deliver(env, callback(data, from_id=OTHER_OWNER, chat_id=OTHER_OWNER, message_id=77))
    assert last_answer(env) == EXPIRED
    assert await current(env) == mine
    assert await current(env, OTHER_OWNER) == theirs

    # The owner's own press works once; the same button again is stale.
    await deliver(env, callback(data, message_id=row.prompt_message_id))
    assert last_answer(env) == "Saved"
    assert (await current(env))["urgent_bypass"] is True
    await deliver(env, callback(data, message_id=row.prompt_message_id))
    assert last_answer(env) == EXPIRED
    assert (await current(env))["urgent_bypass"] is True

    # A different kind's token does not open this card either.
    await deliver(env, private_message(OWNER, "/settings"))
    settings = await card(env, panels.SETTINGS_CARD)
    await deliver(
        env,
        callback(f"{panels.ALERTS_CARD}:{settings.token}:b_off", message_id=1),
    )
    assert last_answer(env) == EXPIRED
    assert (await current(env))["urgent_bypass"] is True


# ------------------------------------------------------------ the conversation panel
async def test_the_panel_shows_the_contacts_priority_ticked(env: Env) -> None:
    await kamola_panel(env)
    shown = env.sender.sends[-1]
    assert keyboard_ops(shown["reply_markup"]) == [
        "observe",
        "ignore",
        "delete",
        "p_vip",
        "p_normal",
        "p_low",
    ]
    assert keyboard_labels(shown["reply_markup"]) == [
        "Observe ✓",
        "Ignore",
        "Delete stored data…",
        "VIP",
        "Normal ✓",
        "Low",
    ]
    for row in shown["reply_markup"].inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


async def test_choosing_vip_on_the_panel_sets_the_contacts_priority_and_audits_it(
    env: Env,
) -> None:
    contact_id = await kamola_panel(env)
    await tap(env, panels.PANEL_CARD, "p_vip")
    assert await contact_priority(env, contact_id) == "vip"
    assert last_answer(env) == "Saved"
    shown = env.sender.edits[-1]
    assert ticked(shown["reply_markup"]) == ["observe", "p_vip"]
    assert keyboard_labels(shown["reply_markup"])[3:] == ["VIP ✓", "Normal", "Low"]
    [details] = await audit_details(env, priority.EVENT_PRIORITY_CHANGED)
    assert details == {"contact_id": contact_id, "old": "normal", "new": "vip"}
    async with env.factory() as session:
        entry = (
            await session.execute(
                select(AuditLogEntry).where(AuditLogEntry.event == priority.EVENT_PRIORITY_CHANGED)
            )
        ).scalar_one()
    assert (entry.actor, entry.subject_type, entry.subject_id) == ("owner", "contact", contact_id)

    # Low, then Normal again, from the re-armed panel.
    await tap(env, panels.PANEL_CARD, "p_low")
    assert await contact_priority(env, contact_id) == "low"
    assert ticked(env.sender.edits[-1]["reply_markup"]) == ["observe", "p_low"]
    await tap(env, panels.PANEL_CARD, "p_normal")
    assert await contact_priority(env, contact_id) == "normal"
    assert ticked(env.sender.edits[-1]["reply_markup"]) == ["observe", "p_normal"]


async def test_the_panels_mode_buttons_still_work_beside_the_priority_row(env: Env) -> None:
    contact_id = await kamola_panel(env)
    await tap(env, panels.PANEL_CARD, "p_vip")
    await tap(env, panels.PANEL_CARD, "ignore")  # asks first, as before
    await tap(env, panels.IGNORE_CARD, "confirm")
    assert (await conversation_row(env)).mode == "ignore"
    # The priority belongs to the contact: it survives and the row stays.
    assert await contact_priority(env, contact_id) == "vip"
    shown = env.sender.edits[-1]
    assert ticked(shown["reply_markup"]) == ["ignore", "p_vip"]
    assert keyboard_ops(shown["reply_markup"])[-3:] == ["p_vip", "p_normal", "p_low"]


async def test_tapping_the_chosen_priority_again_changes_nothing(env: Env) -> None:
    contact_id = await kamola_panel(env)
    await tap(env, panels.PANEL_CARD, "p_normal")
    assert await contact_priority(env, contact_id) == "normal"
    assert await audit_details(env, priority.EVENT_PRIORITY_CHANGED) == []
    assert ticked(env.sender.edits[-1]["reply_markup"]) == ["observe", "p_normal"]


async def test_a_bot_chat_panel_has_no_priority_row(env: Env) -> None:
    await connect(env)
    await deliver(
        env,
        business_message(chat_id=BOT_CHAT, from_id=OWNER, message_text="SYNTH /start"),
        business_message(
            chat_id=BOT_CHAT,
            from_id=BOT_CHAT,
            sender_is_bot=True,
            message_text="SYNTH bot reply",
        ),
    )
    await open_panel(env, BOT_CHAT)
    shown = env.sender.sends[-1]
    assert "Mode:" in shown["text"]
    assert shown["reply_markup"] is None  # a bot chat has no buttons at all
    assert (await card(env, panels.PANEL_CARD)) is None


async def test_a_panel_pointing_at_a_foreign_contact_answers_no_longer_active(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`set_priority` raises `NotInAccountError` for a contact of another
    account; the press answers like a dead button and changes nothing."""
    await kamola_panel(env)
    async with transaction(env.factory) as uow:
        stranger = await seed_account(uow.session, telegram_user_id=7000000099)
        other = await seed_conversation(uow.session, account_id=stranger.id)
        foreign_contact = other.contact_id
    original = visibility.conversation_panel

    async def pointing_elsewhere(*args: Any, **kwargs: Any) -> ConversationPanel | None:
        panel = await original(*args, **kwargs)
        assert panel is not None
        return dataclasses.replace(panel, contact_id=foreign_contact)

    monkeypatch.setattr(visibility, "conversation_panel", pointing_elsewhere)
    await tap(env, panels.PANEL_CARD, "p_vip")
    assert last_answer(env) == EXPIRED
    assert await contact_priority(env, foreign_contact) == "normal"
    assert await audit_details(env, priority.EVENT_PRIORITY_CHANGED) == []


# ------------------------------------------------------------ audit
async def test_each_change_writes_one_content_free_audit_row(env: Env) -> None:
    contact_id = await kamola_panel(env)
    await tap(env, panels.PANEL_CARD, "p_vip")
    await tap(env, panels.PANEL_CARD, "p_vip")  # the same value again: no row
    await deliver(env, private_message(OWNER, "/settings"))
    await tap(env, panels.SETTINGS_CARD, "alerts")
    for op in ("q_off", "b_on", "d_0800", "c_10", "c_10"):  # the last repeats: no row
        await tap(env, panels.ALERTS_CARD, op)

    changes = [
        details["changes"]
        for details in await audit_details(env, preferences.EVENT_PREFERENCES_CHANGED)
    ]
    # The language chosen at onboarding, then one row per real change here.
    assert changes[1:] == [
        {"quiet_hours": ["23:00-08:00", "off"]},
        {"urgent_bypass": [False, True]},
        {"digest_time": ["21:00", "08:00"]},
        {"alert_cap_per_hour": [6, 10]},
    ]
    assert await audit_details(env, priority.EVENT_PRIORITY_CHANGED) == [
        {"contact_id": contact_id, "old": "normal", "new": "vip"}
    ]
    # Ids, enums and settings only: no name, no text, no username.
    async with env.factory() as session:
        rows = await session.execute(
            select(AuditLogEntry.actor, AuditLogEntry.details).where(
                AuditLogEntry.event.in_(
                    (preferences.EVENT_PREFERENCES_CHANGED, priority.EVENT_PRIORITY_CHANGED)
                )
            )
        )
        recorded = rows.all()
    assert {actor for actor, _ in recorded} == {"owner"}
    blob = json.dumps([details for _, details in recorded])
    assert KAMOLA not in blob
    assert "SYNTH" not in blob


async def preferences_set(env: Env, **values: Any) -> None:
    """Set preferences the way the agent's tool would (outside the buttons)."""
    async with transaction(env.factory) as uow:
        row = (
            await uow.session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()
        await preferences.update(uow.session, row, actor="owner", clock=env.clock, **values)
