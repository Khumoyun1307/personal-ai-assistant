"""The fixed mode texts of M03 fix round C3 (`agent/owner_texts.py`).

Every text exists in every supported locale with the same placeholders, so
no owner sees a missing or half-filled sentence; the receipts say exactly
what a mode change did and stay short. Synthetic names only.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from typing import get_args

import pytest

from assistant.agent import owner_texts
from assistant.agent.locales import SupportedLocale

LOCALES: tuple[SupportedLocale, ...] = get_args(SupportedLocale)

#: name → per-locale texts; the per-mode ones are flattened as name[mode].
PLAIN = {
    "IGNORE_NOT_SAVED": owner_texts.IGNORE_NOT_SAVED,
    "IGNORE_LEFT_OUT": owner_texts.IGNORE_LEFT_OUT,
    "IGNORE_KEPT": owner_texts.IGNORE_KEPT,
    "IGNORE_DELETE_HINT": owner_texts.IGNORE_DELETE_HINT,
    "OBSERVE_SAVED": owner_texts.OBSERVE_SAVED,
    "OBSERVE_GAP": owner_texts.OBSERVE_GAP,
    "OBSERVE_AI_OFF": owner_texts.OBSERVE_AI_OFF,
    "MODE_UNCHANGED": owner_texts.MODE_UNCHANGED,
    "SAVED_COUNT": owner_texts.SAVED_COUNT,
    "NOT_SAVED_COUNT": owner_texts.NOT_SAVED_COUNT,
}
BY_MODE = {"MODE_LINE": owner_texts.MODE_LINE, "MODE_ON": owner_texts.MODE_ON}

#: A receipt is a header and at most four short bullets; it sits on top of a
#: panel that also holds a summary and notes (Telegram: 4096 characters).
RECEIPT_MAX_CHARS = 520


def _flattened() -> dict[str, Mapping[str, str]]:
    texts: dict[str, Mapping[str, str]] = dict(PLAIN)
    for name, per_locale in BY_MODE.items():
        assert set(per_locale) == set(LOCALES), name
        for mode in ("observe", "ignore"):
            texts[f"{name}[{mode}]"] = {locale: per_locale[locale][mode] for locale in LOCALES}
    return texts


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def test_every_mode_text_has_every_locale_and_the_same_placeholders() -> None:
    for name, per_locale in _flattened().items():
        assert set(per_locale) == set(LOCALES), name
        expected = _placeholders(per_locale["en"])
        for locale in LOCALES:
            template = per_locale[locale]
            assert template.strip(), (name, locale)
            assert _placeholders(template) == expected, (name, locale)
            filled = template.format(**dict.fromkeys(expected, "X"))
            assert "{" not in filled, (name, locale)
            assert "}" not in filled, (name, locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_ignore_receipt_is_short_and_says_what_stays(locale: SupportedLocale) -> None:
    receipt = owner_texts.mode_receipt(
        locale, mode="ignore", previous="observe", name="Kamola", days=30, ai_on=True
    )
    lines = receipt.split("\n")
    assert lines[0] == owner_texts.MODE_ON[locale]["ignore"].format(name="Kamola")
    assert lines[1:] == [
        "• " + owner_texts.IGNORE_NOT_SAVED[locale],
        "• " + owner_texts.IGNORE_LEFT_OUT[locale],
        "• " + owner_texts.IGNORE_KEPT[locale].format(days=30),
        "• " + owner_texts.IGNORE_DELETE_HINT[locale],
    ]
    assert "30" in lines[3]
    assert len(receipt) <= RECEIPT_MAX_CHARS


@pytest.mark.parametrize("locale", LOCALES)
def test_with_a_deletion_the_ignore_receipt_does_not_say_the_data_stays(
    locale: SupportedLocale,
) -> None:
    receipt = owner_texts.mode_receipt(
        locale,
        mode="ignore",
        previous="observe",
        name="Kamola",
        days=90,
        ai_on=True,
        deleting=True,
    )
    assert owner_texts.IGNORE_KEPT[locale].format(days=90) not in receipt
    assert owner_texts.IGNORE_DELETE_HINT[locale] not in receipt
    assert owner_texts.IGNORE_NOT_SAVED[locale] in receipt


@pytest.mark.parametrize("locale", LOCALES)
def test_the_observe_receipt_names_the_lost_messages_and_ai_only_when_true(
    locale: SupportedLocale,
) -> None:
    after_ignore = owner_texts.mode_receipt(
        locale, mode="observe", previous="ignore", name="Kamola", days=90, ai_on=False
    )
    assert after_ignore.split("\n")[1:] == [
        "• " + owner_texts.OBSERVE_SAVED[locale],
        "• " + owner_texts.OBSERVE_GAP[locale],
        "• " + owner_texts.OBSERVE_AI_OFF[locale],
    ]
    assert len(after_ignore) <= RECEIPT_MAX_CHARS
    plain = owner_texts.mode_receipt(
        locale, mode="observe", previous="observe", name="Kamola", days=90, ai_on=True
    )
    assert plain.split("\n")[1:] == ["• " + owner_texts.OBSERVE_SAVED[locale]]


@pytest.mark.parametrize("locale", LOCALES)
def test_a_receipt_escapes_the_name_and_falls_back_to_a_fixed_word(
    locale: SupportedLocale,
) -> None:
    escaped = owner_texts.mode_receipt(
        locale, mode="ignore", previous="observe", name="<b>Ka&mola</b>", days=90, ai_on=True
    )
    assert "&lt;b&gt;Ka&amp;mola&lt;/b&gt;" in escaped
    assert "<b>Ka" not in escaped
    nameless = owner_texts.mode_unchanged(locale, mode="ignore", name=None)
    assert owner_texts.CONTACT_FALLBACK[locale] in nameless
    assert "Ignore" in nameless  # the button's name, as on the button


def test_the_mode_line_falls_back_for_a_mode_without_a_text() -> None:
    assert owner_texts.mode_line("ru", "assist") == "Mode: assist"
    assert owner_texts.mode_line("ru", "ignore") == owner_texts.MODE_LINE["ru"]["ignore"]
