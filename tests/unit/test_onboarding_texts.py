"""The onboarding texts and the language and time-zone screens (M03 fix round C4).

Pinned claims:

- every table has the four supported locales, with the same placeholders;
- the English texts are the M01 texts unchanged (the consent wording is
  owner-approved, M02 D58);
- every consent question names the processor, and the word it asks for
  counts as agreement (SEC-02);
- the stop word each "done" line names really pauses (§6 invariant 10);
- Telegram's `language_code` only orders the buttons; a missing or
  malformed one gives the default order;
- every button's callback data parses and fits Telegram's 64 bytes.
"""

from __future__ import annotations

import re
import string
from datetime import UTC, datetime

import pytest

from assistant.accounts import callbacks
from assistant.agent.locales import SupportedLocale
from assistant.telegram.control import onboarding_texts as texts
from assistant.telegram.control.handlers import AGREEMENT_WORDS, is_stop_word

LOCALES: set[SupportedLocale] = {"en", "uz-Latn", "uz-Cyrl", "ru"}

TABLES = {
    "LANGUAGE_QUESTION": texts.LANGUAGE_QUESTION,
    "LANGUAGE_SAVED": texts.LANGUAGE_SAVED,
    "CONSENT_PROMPT": texts.CONSENT_PROMPT,
    "CONSENT_WORD": texts.CONSENT_WORD,
    "CONSENT_GRANTED": texts.CONSENT_GRANTED,
    "CONSENT_DECLINED": texts.CONSENT_DECLINED,
    "ALREADY_SET_UP": texts.ALREADY_SET_UP,
    "TIMEZONE_QUESTION": texts.TIMEZONE_QUESTION,
    "TIMEZONE_SAVED": texts.TIMEZONE_SAVED,
}

#: The M01 texts, verbatim (`handlers.py` before C4).
ENGLISH_CONSENT_PROMPT = (
    "<b>Before we start</b>\n\n"
    "To answer you, I send the text of this chat to "
    "<b>OpenAI</b>. Nothing is sent anywhere else, and "
    "your messages are not used to train any model.\n\n"
    "See /privacy for what I store and how to delete it.\n\n"
    "Reply <b>yes</b> to agree, or anything else to decline. "
    "You can revoke this later at any time."
)
ENGLISH_GRANTED = "Done. Ask me anything, or see /help.\n\nSend <b>stop</b> to pause."
ENGLISH_DECLINED = (
    "No problem — nothing will be sent to an AI provider.\n"
    "/settings and /privacy still work. Run /start again to change your mind."
)
ENGLISH_ALREADY_SET_UP = "You are already set up. /help shows what I can do."


def placeholders(value: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(value) if field is not None}


@pytest.mark.parametrize("name", sorted(TABLES))
def test_every_table_has_the_four_locales_with_the_same_placeholders(name: str) -> None:
    table = TABLES[name]
    assert set(table) == LOCALES, name
    assert all(value.strip() for value in table.values()), name
    assert len({frozenset(placeholders(value)) for value in table.values()}) == 1, name


def test_every_locale_names_the_same_time_zones() -> None:
    assert set(texts.CITY_NAMES) == LOCALES
    zones = {zone for _, zone in texts.TIMEZONE_OPTIONS}
    assert all(set(names) == zones for names in texts.CITY_NAMES.values())


def test_the_english_texts_are_unchanged() -> None:
    assert texts.consent_prompt("en", ["OpenAI"]) == ENGLISH_CONSENT_PROMPT
    assert texts.CONSENT_GRANTED["en"] == ENGLISH_GRANTED
    assert texts.CONSENT_DECLINED["en"] == ENGLISH_DECLINED
    assert texts.ALREADY_SET_UP["en"] == ENGLISH_ALREADY_SET_UP


@pytest.mark.parametrize("locale", sorted(LOCALES))
def test_every_consent_question_names_the_processor(locale: SupportedLocale) -> None:
    """Consent that does not say who sees the data is not consent (SEC-02)."""
    assert "<b>OpenAI</b>" in texts.consent_prompt(locale, ["OpenAI"])


@pytest.mark.parametrize("locale", sorted(LOCALES))
def test_the_word_each_consent_question_asks_for_counts_as_agreement(
    locale: SupportedLocale,
) -> None:
    """A uz-Cyrl owner typing the "ҳа" the question asks for must not decline."""
    word = texts.CONSENT_WORD[locale]
    assert f"<b>{word}</b>" in texts.CONSENT_PROMPT[locale]
    assert word in AGREEMENT_WORDS
    assert word.upper().lower() in AGREEMENT_WORDS


@pytest.mark.parametrize("locale", sorted(LOCALES))
def test_the_stop_word_each_done_line_names_pauses(locale: SupportedLocale) -> None:
    named = re.findall(r"<b>([^<]+)</b>", texts.CONSENT_GRANTED[locale])
    assert named, locale
    assert all(is_stop_word(word) for word in named), locale


@pytest.mark.parametrize(
    ("hint", "order"),
    [
        ("ru", ("ru", "uz-Latn", "uz-Cyrl", "en")),
        ("ru-RU", ("ru", "uz-Latn", "uz-Cyrl", "en")),
        ("uz", ("uz-Latn", "uz-Cyrl", "ru", "en")),
        ("uz-Latn", ("uz-Latn", "uz-Cyrl", "ru", "en")),
        ("uz-Cyrl", ("uz-Cyrl", "uz-Latn", "ru", "en")),
        ("UZ_cyrl", ("uz-Cyrl", "uz-Latn", "ru", "en")),
        ("en", ("en", "uz-Latn", "uz-Cyrl", "ru")),
        ("en-US", ("en", "uz-Latn", "uz-Cyrl", "ru")),
        (None, ("uz-Latn", "uz-Cyrl", "ru", "en")),
        ("", ("uz-Latn", "uz-Cyrl", "ru", "en")),
        ("de", ("uz-Latn", "uz-Cyrl", "ru", "en")),
        ("xx-garbage!!", ("uz-Latn", "uz-Cyrl", "ru", "en")),
        ("ru" + "-a" * 40, ("uz-Latn", "uz-Cyrl", "ru", "en")),
        ("<b>ru</b>", ("uz-Latn", "uz-Cyrl", "ru", "en")),
    ],
)
def test_the_hint_only_orders_the_options(hint: str | None, order: tuple[str, ...]) -> None:
    assert texts.language_order(texts.hint_locale(hint)) == order
    assert sorted(order) == sorted(LOCALES)


def test_the_picker_lists_every_language_in_the_given_order() -> None:
    order = texts.language_order("ru")
    rows = texts.picker_rows(order)
    assert [op for row in rows for _, op in row] == ["ru", "uz_latn", "uz_cyrl", "en"]
    assert [label for row in rows for label, _ in row] == [
        "Русский",
        "Oʻzbekcha (lotin)",
        "Ўзбекча (кирилл)",
        "English",
    ]
    lines = texts.picker_text(order).splitlines()
    assert [line.removeprefix("🌐 ") for line in lines] == [
        texts.LANGUAGE_QUESTION[locale] for locale in order
    ]


@pytest.mark.parametrize(
    ("stored", "label"),
    [
        (None, "not set"),
        ("uz-Latn", "Oʻzbekcha (lotin)"),
        ("uz", "Oʻzbekcha (lotin)"),
        ("uz-Cyrl", "Ўзбекча (кирилл)"),
        ("ru-RU", "Русский"),
        ("en", "English"),
        ("kk", "kk"),
    ],
)
def test_the_language_label_never_says_auto(stored: str | None, label: str) -> None:
    assert texts.language_label(stored) == label


@pytest.mark.parametrize("kind", ["onb_lang", "set_lang", "onb_tz"])
def test_every_button_parses_and_fits_telegrams_limit(kind: str) -> None:
    token = callbacks.new_token()
    ops = [op for op, _, _ in texts.LANGUAGE_OPTIONS] + [op for op, _ in texts.TIMEZONE_OPTIONS]
    for op in [*ops, "back"]:
        data = callbacks.encode(kind, token, op)
        assert len(data.encode()) <= callbacks.MAX_CALLBACK_DATA_BYTES
        assert callbacks.parse(data).op == op


def test_the_time_zone_buttons_show_the_offset_in_the_owners_language() -> None:
    now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    rows = texts.timezone_rows("ru", now)
    assert rows == [[("Ташкент (UTC+5)", "tashkent"), ("Москва (UTC+3)", "moscow"), ("UTC", "utc")]]
    assert texts.timezone_rows("en", now)[0][0] == ("Tashkent (UTC+5)", "tashkent")
    assert "<code>/settings timezone Area/City</code>" in texts.TIMEZONE_QUESTION["en"]
