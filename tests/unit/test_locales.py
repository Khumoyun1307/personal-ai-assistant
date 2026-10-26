"""The owner-facing locales for fixed product wording (S03.7)."""

from __future__ import annotations

import pytest

from assistant.agent.locales import supported_locale


@pytest.mark.parametrize(
    ("tag", "locale"),
    [
        ("uz-Latn", "uz-Latn"),
        ("uz", "uz-Latn"),
        ("uz-UZ", "uz-Latn"),
        ("UZ-latn", "uz-Latn"),
        ("uz-Cyrl", "uz-Cyrl"),
        ("ru", "ru"),
        ("ru-RU", "ru"),
        ("en", "en"),
        ("en-US", "en"),
    ],
)
def test_the_owners_language_tag_maps_onto_a_supported_locale(tag: str, locale: str) -> None:
    assert supported_locale(tag) == locale


@pytest.mark.parametrize("tag", [None, "", "  ", "uz-AF", "de", "tr-TR"])
def test_an_unknown_or_missing_tag_falls_back_to_english(tag: str | None) -> None:
    assert supported_locale(tag) == "en"
