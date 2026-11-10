"""The owner-facing locales for fixed product wording (S03.7).

Text the application writes itself — who asked or promised in a summary
item, the source of a cited message — comes from fixed per-locale templates,
never from the model, so it needs one of the locales those templates exist
for. `supported_locale` maps the owner's language tag
(`accounts.locale_pref`) onto them the way `context.summary_language` names
it: bare `uz` and `uz-UZ` mean Latin script, region tags of Russian and
English fall back to the language, anything else falls back to English.
"""

from __future__ import annotations

from typing import Literal

SupportedLocale = Literal["uz-Latn", "uz-Cyrl", "ru", "en"]

_EXACT: dict[str, SupportedLocale] = {
    "uz": "uz-Latn",
    "uz-uz": "uz-Latn",
    "uz-latn": "uz-Latn",
    "uz-latn-uz": "uz-Latn",
    "uz-cyrl": "uz-Cyrl",
    "uz-cyrl-uz": "uz-Cyrl",
}
#: Languages whose region subtag never changes the locale (`ru-RU`, `en-US`).
_BY_PRIMARY: dict[str, SupportedLocale] = {"ru": "ru", "en": "en"}
DEFAULT_LOCALE: SupportedLocale = "en"


def supported_locale(tag: str | None) -> SupportedLocale:
    """The fixed-wording locale for the owner's language tag."""
    if not tag:
        return DEFAULT_LOCALE
    key = tag.strip().lower()
    if key in _EXACT:
        return _EXACT[key]
    return _BY_PRIMARY.get(key.partition("-")[0], DEFAULT_LOCALE)
