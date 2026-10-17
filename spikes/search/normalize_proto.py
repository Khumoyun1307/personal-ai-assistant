"""Prototype of `memory.search.normalize()` (ARCHITECTURE §11.2).

Throwaway. The shipped version lives in `src/assistant/memory/` from M03 with
its own tests and a `normalizer_version`.

Goal: 1995 Uzbek Latin, new Uzbek Latin and Uzbek Cyrillic must converge on one
apostrophe-free canonical form, so a query typed in any script finds text
stored in any other. Russian passes through the same transliteration — not
linguistically right, but consistent on both sides, which is all a search key
needs. The original text is always kept for display and for the model.
"""

from __future__ import annotations

import re
import unicodedata

NORMALIZER_VERSION = 0  # prototype; the shipped one starts at 1

#: Every apostrophe-like character Telegram clients and keyboards produce.
APOSTROPHES = "ʻʼ‘’'`´′"

#: Uzbek Cyrillic -> Latin. `е` is handled separately (context-dependent).
CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "ğ", "д": "d",
    "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k",
    "қ": "q", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ў": "ö", "ф": "f",
    "х": "x", "ҳ": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Russian letters that are not in the Uzbek alphabet.
    "ё": "yo",
}

VOWELS = set("аеёиоуўыэюяaeiouöü")

_WORD_BOUNDARY = re.compile(r"\W", re.UNICODE)


def _transliterate(text: str) -> str:
    out: list[str] = []
    for index, char in enumerate(text):
        if char == "е":
            previous = text[index - 1] if index else ""
            # `е` is `ye` word-initially and after a vowel or a sign, else `e`.
            initial = index == 0 or _WORD_BOUNDARY.match(previous) is not None
            out.append("ye" if initial or previous in VOWELS or previous in "ъь" else "e")
        else:
            out.append(CYRILLIC_TO_LATIN.get(char, char))
    return "".join(out)


def _fold_latin(text: str) -> str:
    """Fold the new-alphabet letters and every apostrophe variant."""
    for apostrophe in APOSTROPHES:
        text = text.replace(f"o{apostrophe}", "ö").replace(f"g{apostrophe}", "ğ")
    # Any apostrophe left is the hamza/tutuq belgisi: it carries no search value.
    text = re.sub(f"[{re.escape(APOSTROPHES)}]", "", text)
    # Turkish-style spellings people type on phone keyboards.
    return text.replace("ş", "sh").replace("ç", "ch").replace("ñ", "ng")


def normalize(text: str) -> str:
    """Canonical, apostrophe-free search key."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _transliterate(text)
    text = _fold_latin(text)
    return re.sub(r"\s+", " ", text).strip()
