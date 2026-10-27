"""Onboarding asks the owner's language, then the time zone (M03 fix round C4).

FR-ONB-01, J1 step 3: /start collects the preferred language and the time
zone. Owner decision 2026-10-27: "Language, then time zone". The claims:

- /start shows a language card first; the consent question follows a tap, in
  the chosen language, and a typed answer counts only once that question was
  delivered: consent is never given to a question the owner did not see
  (SEC-02, §6 invariant 9);
- the tap saves the language through `accounts/preferences.py` (validated,
  audited); stale, double, expired and foreign taps change nothing
  (§6 invariant 12); a repeated /start leaves one live picker;
- Telegram's `language_code` of the control message only orders the
  buttons; it is never stored, logged only as a mapped enum, and a business
  message's `language_code` is never used (§6 invariant 5);
- after the consent answer a time-zone card, once, only while the time zone
  is the default one nobody chose;
- /settings has a Language button and shows the real language, never "auto";
- forwarded text never picks a language or answers the consent question
  (§6 invariant 3b).

No model is called anywhere here. Synthetic text only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts import preferences, service
from assistant.accounts.models import Account, AuditLogEntry, Consent, PendingInput
from assistant.config import Settings
from assistant.db.uow import transaction
from assistant.observability.logging import configure_logging
from assistant.telegram.control import onboarding_texts as texts
from tests.fakes.sender import network_error, telegram_error
from tests.integration.business_env import (
    CONTACT,
    LANGUAGE_CARD,
    OWNER,
    TIMEZONE_CARD,
    Env,
    business_message,
    callback,
    connect,
    connection_update,
    deliver,
    environment,
    onboard,
    own_card,
    press_own,
    private_message,
)

pytestmark = pytest.mark.integration

OTHER_OWNER = 7000000002
EXPIRED = "This button is no longer active."
#: The M01 line, kept verbatim above the picker for an account set up before C4.
ALREADY_SET_UP = "You are already set up. /help shows what I can do."
VALID_DB_URL = "postgresql+psycopg://user:pw@db:5432/assistant"


# ------------------------------------------------------------ helpers
async def owner_account(env: Env, owner_id: int = OWNER) -> Account:
    async with env.factory() as session:
        return (
            await session.execute(select(Account).where(Account.telegram_user_id == owner_id))
        ).scalar_one()


async def questions(env: Env) -> list[PendingInput]:
    """Open text questions (a plain message answers them), not cards."""
    async with env.factory() as session:
        return list(
            (await session.execute(select(PendingInput).where(PendingInput.token.is_(None))))
            .scalars()
            .all()
        )


async def preference_changes(env: Env) -> list[tuple[str, dict[str, Any]]]:
    async with env.factory() as session:
        rows = await session.execute(
            select(AuditLogEntry.actor, AuditLogEntry.details)
            .where(AuditLogEntry.event == preferences.EVENT_PREFERENCES_CHANGED)
            .order_by(AuditLogEntry.id)
        )
        return [(actor, details) for actor, details in rows.all()]


async def granted(env: Env, owner_id: int = OWNER) -> bool:
    account = await owner_account(env, owner_id)
    async with env.factory() as session:
        state = await service.active_consent(session, account.id, kind=service.CONSENT_AI_CONTROL)
    return state.granted


def ops(markup: Any) -> list[str]:
    return [b.callback_data.split(":")[2] for row in markup.inline_keyboard for b in row]


def kinds(markup: Any) -> set[str]:
    return {b.callback_data.split(":")[0] for row in markup.inline_keyboard for b in row}


def labels(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def last_message_id(env: Env) -> int:
    """The id `FakeTelegramSender` gave the latest delivered send (a failed
    send is recorded but gets none)."""
    return env.sender._next_message_id


async def legacy_owner(env: Env) -> None:
    """An account set up before C4: consent given, no language chosen."""
    await deliver(env, private_message(OWNER, "/start"))
    async with transaction(env.factory) as uow:
        account = (
            await uow.session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()
        await service.grant_consent(
            uow.session,
            account,
            kind=service.CONSENT_AI_CONTROL,
            policy_version=service.POLICY_VERSION,
            processors=list(service.AI_PROCESSORS),
            clock=env.clock,
        )
        account.status = "active"
        await service.close_cards(uow.session, account_id=account.id, kind=LANGUAGE_CARD)


def forwarded(owner_id: int, text: str) -> dict[str, Any]:
    update = private_message(owner_id, text)
    update["message"]["forward_origin"] = {
        "type": "user",
        "date": 1792354000,
        "sender_user": {"id": CONTACT, "is_bot": False, "first_name": "Synthetic"},
    }
    return update


@pytest.fixture
async def two_owners(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url, owner_ids=frozenset({OWNER, OTHER_OWNER})) as built:
        yield built


# ------------------------------------------------------------ the language card
async def test_start_asks_for_the_language_before_the_consent_question(env: Env) -> None:
    await deliver(env, private_message(OWNER, "/start"))

    [picker] = env.sender.sends
    assert picker["text"] == texts.picker_text(texts.language_order(None))
    assert ops(picker["reply_markup"]) == ["uz_latn", "uz_cyrl", "ru", "en"]
    assert kinds(picker["reply_markup"]) == {LANGUAGE_CARD}
    assert "OpenAI" not in picker["text"]  # consent is asked after the language
    card = await own_card(env, LANGUAGE_CARD)
    assert card is not None
    assert card.prompt_message_id == last_message_id(env)
    # Nothing is armed that a plain message could answer.
    assert await questions(env) == []
    account = await owner_account(env)
    assert (account.status, account.locale_pref, account.timezone) == ("onboarding", None, "UTC")


async def test_a_yes_before_the_consent_question_was_shown_grants_nothing(env: Env) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    await deliver(env, private_message(OWNER, "yes"))

    assert not await granted(env)
    assert (await owner_account(env)).status == "onboarding"
    assert await own_card(env, LANGUAGE_CARD) is not None  # still waiting for a tap


@pytest.mark.parametrize(
    ("op", "tag", "word"),
    [
        ("uz_latn", "uz-Latn", "ha"),
        ("uz_cyrl", "uz-Cyrl", "ҳа"),
        ("ru", "ru", "да"),
        ("en", "en", "yes"),
    ],
)
async def test_choosing_a_language_saves_it_and_asks_consent_in_that_language(
    env: Env, op: str, tag: Any, word: str
) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    picker = await own_card(env, LANGUAGE_CARD)
    assert picker is not None

    await press_own(env, LANGUAGE_CARD, op)

    assert (await owner_account(env)).locale_pref == tag
    assert await preference_changes(env) == [
        ("owner", {"changes": {"preferred_language": [None, tag]}})
    ]
    edit = env.sender.edits[-1]
    assert (edit["message_id"], edit["text"], edit["reply_markup"]) == (
        picker.prompt_message_id,
        texts.LANGUAGE_SAVED[tag],
        None,
    )
    assert env.sender.answered_callbacks, "a press is always answered"
    assert env.sender.texts[-1] == texts.consent_prompt(tag, ["OpenAI"])
    [question] = await questions(env)
    assert (question.kind, question.prompt_message_id) == (
        "onboarding_consent",
        last_message_id(env),
    )
    assert await own_card(env, LANGUAGE_CARD) is None  # used up

    await deliver(env, private_message(OWNER, word))

    assert await granted(env)
    assert (await owner_account(env)).status == "active"
    assert texts.CONSENT_GRANTED[tag] in env.sender.texts
    assert all(call["business_connection_id"] is None for call in env.sender.sends)
    assert all(call["business_connection_id"] is None for call in env.sender.edits)


@pytest.mark.parametrize(
    "failure",
    [network_error(), telegram_error("Bad Request: chat not found", status_code=400)],
    ids=["no_answer", "refused"],
)
async def test_the_consent_question_is_not_armed_when_it_was_not_delivered(
    env: Env, failure: Exception
) -> None:
    """SEC-02: a "yes" can only answer a question the owner was shown."""
    await deliver(env, private_message(OWNER, "/start"))
    env.sender.fail_with = failure
    await press_own(env, LANGUAGE_CARD, "en")
    env.sender.fail_with = None

    assert (await owner_account(env)).locale_pref == "en"  # the tap itself is committed
    assert await questions(env) == []
    await deliver(env, private_message(OWNER, "yes"))
    assert not await granted(env)

    # /start asks again, in the chosen language, and only then can it be answered.
    await deliver(env, private_message(OWNER, "/start"))
    assert env.sender.texts[-1] == texts.consent_prompt("en", ["OpenAI"])
    [question] = await questions(env)
    assert question.prompt_message_id == last_message_id(env)
    await deliver(env, private_message(OWNER, "yes"))
    assert await granted(env)


async def test_start_does_not_arm_the_question_when_it_was_not_delivered(env: Env) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    await press_own(env, LANGUAGE_CARD, "ru")
    await deliver(env, private_message(OWNER, "/help"))  # a command cancels the question
    env.sender.fail_with = network_error()
    await deliver(env, private_message(OWNER, "/start"))
    env.sender.fail_with = None

    assert await questions(env) == []
    await deliver(env, private_message(OWNER, "да"))
    assert not await granted(env)


# ------------------------------------------------------------ the hint
@pytest.mark.parametrize(
    ("hint", "first"),
    [
        ("ru", "ru"),
        ("uz-Cyrl", "uz_cyrl"),
        ("uz", "uz_latn"),
        ("en-GB", "en"),
        (None, "uz_latn"),
        ("xx-garbage!!", "uz_latn"),
    ],
)
async def test_the_hint_orders_the_buttons_and_is_never_stored(
    env: Env, hint: str | None, first: str
) -> None:
    await deliver(env, private_message(OWNER, "/start", language_code=hint))

    picker = env.sender.sends[-1]
    assert ops(picker["reply_markup"])[0] == first
    assert sorted(ops(picker["reply_markup"])) == ["en", "ru", "uz_cyrl", "uz_latn"]
    account = await owner_account(env)
    assert account.locale_pref is None  # never saved from the hint
    assert account.settings == {}
    assert await preference_changes(env) == []


async def test_the_hint_is_logged_only_as_a_supported_locale(
    env: Env, clean_env: None, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    configure_logging(Settings(_env_file=None, database_url=VALID_DB_URL, log_format="json"))  # type: ignore[call-arg]
    caplog.set_level(logging.DEBUG)
    await deliver(env, private_message(OWNER, "/start", language_code="ru-Qx9zk7"))

    output = capsys.readouterr().out
    assert "Qx9zk7" not in output
    for record in caplog.records:
        assert "Qx9zk7" not in f"{record.getMessage()} {record.__dict__}"
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    [requested] = [e for e in events if e.get("event") == "onboarding_language_requested"]
    assert requested["hint_locale"] == "ru"


async def test_a_business_messages_language_code_is_never_used(env: Env) -> None:
    """§6 invariant 5: the contact's (or the owner's business) `language_code`
    never reaches the control chat; only the control message's would."""
    await legacy_owner(env)
    connection = connection_update()
    connection["business_connection"]["user"]["language_code"] = "ru"
    await deliver(env, connection)
    from_contact = business_message(message_id=1)
    from_contact["business_message"]["from_user"]["language_code"] = "ru"
    from_owner = business_message(message_id=2, from_id=OWNER)
    from_owner["business_message"]["from_user"]["language_code"] = "ru"
    await deliver(env, from_contact, from_owner)

    await deliver(env, private_message(OWNER, "/start"))

    assert ops(env.sender.sends[-1]["reply_markup"])[0] == "uz_latn"
    assert (await owner_account(env)).locale_pref is None


# ------------------------------------------------------------ taps that change nothing
async def test_a_repeated_start_leaves_one_live_picker(env: Env) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    first = await own_card(env, LANGUAGE_CARD)
    await deliver(env, private_message(OWNER, "/start"))
    second = await own_card(env, LANGUAGE_CARD)  # one, or this raises
    assert first is not None
    assert second is not None
    assert first.token != second.token

    await deliver(env, callback(f"{LANGUAGE_CARD}:{first.token}:ru"))

    assert env.sender.answered_callbacks[-1]["text"] == EXPIRED
    assert (await owner_account(env)).locale_pref is None
    assert await questions(env) == []


async def test_a_double_tap_saves_once_and_asks_once(env: Env) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    update = await press_own(env, LANGUAGE_CARD, "en")
    token = update["callback_query"]["data"].split(":")[1]

    await deliver(env, callback(f"{LANGUAGE_CARD}:{token}:ru"))

    assert env.sender.answered_callbacks[-1]["text"] == EXPIRED
    assert (await owner_account(env)).locale_pref == "en"
    assert env.sender.texts.count(texts.consent_prompt("en", ["OpenAI"])) == 1
    assert len(await preference_changes(env)) == 1


async def test_an_expired_picker_changes_nothing(env: Env) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    env.clock.advance(hours=25)

    await press_own(env, LANGUAGE_CARD, "ru")

    assert env.sender.answered_callbacks[-1]["text"] == EXPIRED
    assert (await owner_account(env)).locale_pref is None
    assert await questions(env) == []


async def test_another_owners_picker_cannot_be_pressed(two_owners: Env) -> None:
    env = two_owners
    await deliver(env, private_message(OWNER, "/start"))
    await deliver(env, private_message(OTHER_OWNER, "/start"))
    theirs = await own_card(env, LANGUAGE_CARD, OTHER_OWNER)
    assert theirs is not None

    await deliver(env, callback(f"{LANGUAGE_CARD}:{theirs.token}:ru", from_id=OWNER))

    assert env.sender.answered_callbacks[-1]["text"] == EXPIRED
    assert (await owner_account(env, OTHER_OWNER)).locale_pref is None
    assert (await owner_account(env)).locale_pref is None
    assert await own_card(env, LANGUAGE_CARD, OTHER_OWNER) is not None  # still theirs


async def test_forwarded_text_never_picks_a_language_or_answers_consent(env: Env) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    await deliver(env, forwarded(OWNER, "/settings language ru"))
    assert (await owner_account(env)).locale_pref is None

    await press_own(env, LANGUAGE_CARD, "en")
    await deliver(env, forwarded(OWNER, "yes"))
    assert not await granted(env)
    assert len(await questions(env)) == 1  # still open for the owner's own answer

    await deliver(env, private_message(OWNER, "yes"))
    assert await granted(env)


# ------------------------------------------------------------ accounts set up before C4
async def test_an_onboarded_owner_without_a_language_gets_the_picker_on_start(env: Env) -> None:
    await legacy_owner(env)

    await deliver(env, private_message(OWNER, "/start"))
    shown = env.sender.texts[-1]
    assert shown.startswith(ALREADY_SET_UP)
    assert texts.picker_text(texts.language_order(None)) in shown
    sends_before = len(env.sender.sends)
    await press_own(env, LANGUAGE_CARD, "ru")

    assert (await owner_account(env)).locale_pref == "ru"
    assert env.sender.edits[-1]["text"] == texts.LANGUAGE_SAVED["ru"]
    assert len(env.sender.sends) == sends_before  # no consent question again
    assert await questions(env) == []
    assert await own_card(env, TIMEZONE_CARD) is None
    async with env.factory() as session:
        assert len((await session.execute(select(Consent))).scalars().all()) == 1


async def test_start_with_a_language_and_consent_says_so_in_that_language(env: Env) -> None:
    await onboard(env, language="uz_cyrl")
    await deliver(env, private_message(OWNER, "/start"))
    assert env.sender.texts[-1] == texts.ALREADY_SET_UP["uz-Cyrl"]


# ------------------------------------------------------------ the time zone
@pytest.mark.parametrize(
    ("answer", "outcome"), [("да", texts.CONSENT_GRANTED), ("нет", texts.CONSENT_DECLINED)]
)
async def test_the_time_zone_card_follows_the_consent_answer(
    env: Env, answer: str, outcome: dict[Any, str]
) -> None:
    await deliver(env, private_message(OWNER, "/start"))
    await press_own(env, LANGUAGE_CARD, "ru")
    await deliver(env, private_message(OWNER, answer))

    assert env.sender.texts[-2] == outcome["ru"]
    card = env.sender.sends[-1]
    assert card["text"] == texts.TIMEZONE_QUESTION["ru"]
    assert ops(card["reply_markup"]) == ["tashkent", "moscow", "utc"]
    assert kinds(card["reply_markup"]) == {TIMEZONE_CARD}
    shown = await own_card(env, TIMEZONE_CARD)
    assert shown is not None
    assert shown.prompt_message_id == last_message_id(env)

    await press_own(env, TIMEZONE_CARD, "tashkent")

    assert (await owner_account(env)).timezone == "Asia/Tashkent"
    assert (await preference_changes(env))[-1] == (
        "owner",
        {"changes": {"timezone": ["UTC", "Asia/Tashkent"]}},
    )
    assert env.sender.edits[-1]["text"] == texts.TIMEZONE_SAVED["ru"].format(zone="Asia/Tashkent")
    assert env.sender.edits[-1]["reply_markup"] is None


async def test_the_time_zone_is_asked_once(env: Env) -> None:
    await onboard(env)  # the card was shown and answered (UTC)
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    await deliver(env, private_message(OWNER, "/start"))
    await deliver(env, private_message(OWNER, "yes"))

    assert await granted(env)
    assert env.sender.texts[-1] == texts.CONSENT_GRANTED["en"]
    assert await own_card(env, TIMEZONE_CARD) is None


async def test_a_time_zone_the_owner_chose_is_not_asked_again(env: Env) -> None:
    """Even when it is UTC again: chosen, not the untouched default."""
    await deliver(env, private_message(OWNER, "/start"))
    await deliver(env, private_message(OWNER, "/settings timezone Europe/Moscow"))
    await deliver(env, private_message(OWNER, "/settings timezone UTC"))
    await press_own(env, LANGUAGE_CARD, "en")
    await deliver(env, private_message(OWNER, "yes"))

    assert await granted(env)
    assert await own_card(env, TIMEZONE_CARD) is None
    assert (await owner_account(env)).timezone == "UTC"


async def test_the_default_time_zone_set_by_the_owner_counts_as_chosen(env: Env) -> None:
    """`/settings timezone UTC` on an account still at UTC changes nothing,
    so no `preferences_changed` row records it; it is a choice all the same
    (C4 review). Recorded once, content-free."""
    await deliver(env, private_message(OWNER, "/start"))
    await deliver(env, private_message(OWNER, "/settings timezone UTC"))
    await deliver(env, private_message(OWNER, "/settings timezone UTC"))
    await press_own(env, LANGUAGE_CARD, "en")
    await deliver(env, private_message(OWNER, "yes"))

    assert await granted(env)
    assert env.sender.texts[-1] == texts.CONSENT_GRANTED["en"]
    assert await own_card(env, TIMEZONE_CARD) is None
    assert await preference_changes(env) == [
        ("owner", {"changes": {"preferred_language": [None, "en"]}})
    ]
    async with env.factory() as session:
        rows = await session.execute(
            select(AuditLogEntry.actor, AuditLogEntry.details).where(
                AuditLogEntry.event == preferences.EVENT_TIMEZONE_CONFIRMED
            )
        )
        assert rows.all() == [("owner", {})]


# ------------------------------------------------------------ /settings
async def test_settings_has_a_language_button_that_saves_the_choice(env: Env) -> None:
    """FR-CTL-10: deterministic, no model involved."""
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    shown = env.sender.sends[-1]
    assert "Language: English" in shown["text"]
    assert "lang" in ops(shown["reply_markup"])

    await press_own(env, "settings", "lang")
    screen = env.sender.edits[-1]
    assert ops(screen["reply_markup"]) == ["uz_latn", "uz_cyrl", "ru", "en", "back"]
    assert kinds(screen["reply_markup"]) == {"set_lang"}
    assert "English ✓" in labels(screen["reply_markup"])

    await press_own(env, "set_lang", "ru")

    assert (await owner_account(env)).locale_pref == "ru"
    assert (await preference_changes(env))[-1] == (
        "owner",
        {"changes": {"preferred_language": ["en", "ru"]}},
    )
    settings = env.sender.edits[-1]
    assert "Language: Русский" in settings["text"]
    assert "lang" in ops(settings["reply_markup"])  # the settings card again
    assert await questions(env) == []  # no consent question from /settings


async def test_the_language_screen_goes_back_to_settings(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    await press_own(env, "settings", "lang")
    await press_own(env, "set_lang", "back")

    assert "<b>Settings</b>" in env.sender.edits[-1]["text"]
    assert (await owner_account(env)).locale_pref == "en"


async def test_settings_says_not_set_never_auto(env: Env) -> None:
    await legacy_owner(env)
    await deliver(env, private_message(OWNER, "/settings"))
    shown = env.sender.texts[-1]
    assert "Language: not set" in shown
    assert "auto" not in shown
