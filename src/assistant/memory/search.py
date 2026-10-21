"""Search-key normalization and cheap script/language tagging (ARCHITECTURE §11.2).

Version 1 (M02) is deliberately small: NFKC, case folding, one apostrophe
family, collapsed whitespace. Cyrillic→Latin transliteration, the new-alphabet
letters and stem variants arrive in S03.1, which bumps `NORMALIZER_VERSION`
and runs `reindex-search` over rows written with an older version. Writing
*some* key now means every M02 message is already findable once search
exists, and nothing has to be re-ingested — only re-keyed.

The same function runs at write time (`messages.search_key`) and at query
time; that symmetry is the whole design, so never add a write-only step here.
"""

from __future__ import annotations

import re
import unicodedata

NORMALIZER_VERSION = 1

#: Every apostrophe-like character Uzbek Latin text is typed with (TF §8), to
#: the one the canonical form uses. The 1995 alphabet's oʻ/gʻ keep their
#: apostrophe in v1; S03.1 folds them into ö/ğ.
_APOSTROPHES = str.maketrans(dict.fromkeys("ʻʼ‘’`´ʹʽ", "'"))  # noqa: RUF001
_WHITESPACE = re.compile(r"\s+")
#: Links are not language: a Cyrillic message with a URL in it is Cyrillic.
_URL = re.compile(r"\S+://\S+|\bwww\.\S+")

_UZBEK_CYRILLIC_LETTERS = set("ўқғҳЎҚҒҲ")
_RUSSIAN_ONLY_LETTERS = set("щыэЩЫЭ")


def normalize(text: str) -> str:
    """The canonical search form of `text`. Pure, deterministic, versioned."""
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_APOSTROPHES)
    return _WHITESPACE.sub(" ", folded).strip()


def detect_script(text: str) -> str | None:
    """`latin`, `cyrillic`, `mixed`, or None when there are no letters."""
    cyrillic = latin = 0
    for char in _URL.sub(" ", text):
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if name.startswith("CYRILLIC"):
            cyrillic += 1
        elif name.startswith("LATIN"):
            latin += 1
    if not cyrillic and not latin:
        return None
    if cyrillic and latin:
        # A few Latin letters in a Cyrillic message (a URL, a brand) are
        # noise, not a mixed-script message.
        minority = min(cyrillic, latin) / (cyrillic + latin)
        if minority < 0.2:
            return "cyrillic" if cyrillic > latin else "latin"
        return "mixed"
    return "cyrillic" if cyrillic else "latin"


def guess_lang(text: str) -> str | None:
    """A cheap guess for Cyrillic text only (§11.2): Uzbek-only letters ⇒ `uz`,
    Russian-only letters ⇒ `ru`. Latin text is left untagged in v1 — Uzbek
    Latin and English need a vocabulary heuristic that belongs with S03.1."""
    letters = set(text)
    if letters & _UZBEK_CYRILLIC_LETTERS:
        return "uz"
    if letters & _RUSSIAN_ONLY_LETTERS:
        return "ru"
    return None
