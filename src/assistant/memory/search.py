"""Search-key normalization, query terms and cheap script/language tagging
(ARCHITECTURE §11.2).

**Version 2 — the ASCII canonical form** (owner decision 2026-10-22, M02 D41,
M03 D44). 1995 Uzbek Latin, the new Latin alphabet, apostrophe-less Latin and
Uzbek Cyrillic all converge on one key, because people type every one of them:

- NFKC and case folding;
- every apostrophe-like character (ʻ ʼ ‘ ’ ' ` ´ ʹ ʽ ′) is removed, so oʻ/o'/o →
  `o`, gʻ/g' → `g`, and the tutuq belgisi disappears;
- ö ō ŏ ó → `o`; ğ ǵ → `g`; ş → `sh`; ç → `ch`; ñ → `ng`;
- Cyrillic → Latin with the Uzbek table (ў→o, ғ→g, қ→q, ҳ→h, х→x, ш→sh, ч→ch,
  ё→yo, ю→yu, я→ya, ц→ts, е→ye word-initially or after a vowel/ъ/ь, else e;
  ъ and ь dropped) plus the Russian letters (щ→shch, ы→i, э→e, й→y), so
  Latin-typed Russian matches Cyrillic Russian too. Linguistically loose,
  but identical on both sides, which is all a search key needs;
- whitespace collapsed.

The original text is always kept for display and for the model. `pg_trgm`
absorbs what is left (typos, x/h).

The same `normalize` runs at write time (`messages.search_key`) and at query
time; that symmetry is the whole design, so never add a write-only step here.
A change to its output bumps `NORMALIZER_VERSION`, and `reindex-search`
re-keys every retained row (`memory/search_index.py`).

Query helpers are pure: `query_terms` lets only `[a-z0-9]+` tokens through, so
nothing a user (or the model) types can inject tsquery syntax.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Literal

NORMALIZER_VERSION = 3

#: Every apostrophe-like character Telegram clients and keyboards produce
#: (TF §8). Removed before NFKC as well as after: NFKC turns the acute accent
#: (´) into a space plus a combining mark, which would split the word.
_APOSTROPHE_CHARS = "ʻʼ‘’'`´ʹʽ′‛ꞌˈˊˋ"
_DROP_APOSTROPHES = str.maketrans(dict.fromkeys(_APOSTROPHE_CHARS, None))
#: Spacing modifier letters (U+02B0–U+02FF) are apostrophe and accent
#: lookalikes in Latin text; with modifier symbols (category Sk) and the
#: combining dot that casefolding `İ` leaves behind, they are dropped too.
_COMBINING_DOT_ABOVE = "̇"


def _drop_modifiers(text: str) -> str:
    return "".join(
        char
        for char in text
        if not (0x02B0 <= ord(char) <= 0x02FF or unicodedata.category(char) == "Sk")
    )


#: Casefolding `İ` gives `i` + U+0307; only that stray dot is dropped, after
#: NFKC, so a decomposed `z` + U+0307 still composes to `ż` first.
_STRAY_DOT = "i" + _COMBINING_DOT_ABOVE


#: Latin letters of other orthographies people type Uzbek with.
_LATIN_FOLDS = str.maketrans(
    {
        "ö": "o",
        "ō": "o",
        "ŏ": "o",
        "ó": "o",
        "ğ": "g",
        "ǵ": "g",
        "ş": "sh",
        "ç": "ch",
        "ñ": "ng",
    }
)

#: Uzbek Cyrillic → Latin, plus the Russian letters Uzbek does not use. `е` is
#: context-dependent and handled in `_transliterate`.
_CYRILLIC = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ғ": "g",
    "д": "d",
    "ё": "yo",
    "ж": "j",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "қ": "q",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ў": "o",
    "ф": "f",
    "х": "x",
    "ҳ": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "i",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
#: After these, Cyrillic `е` is read `ye`.
_YE_AFTER = set("аеёиоуўыэюяъь") | set("aeiou")

_WHITESPACE = re.compile(r"\s+")
_TERM = re.compile(r"[a-z0-9]+")
#: Links are not language: a Cyrillic message with a URL in it is Cyrillic.
_URL = re.compile(r"\S+://\S+|\bwww\.\S+")

_UZBEK_CYRILLIC_LETTERS = set("ўқғҳЎҚҒҲ")
_RUSSIAN_ONLY_LETTERS = set("щыэЩЫЭ")


def _transliterate(text: str) -> str:
    out: list[str] = []
    previous = ""
    for char in text:
        if char == "е":
            initial = not previous.isalpha()
            out.append("ye" if initial or previous in _YE_AFTER else "e")
        else:
            out.append(_CYRILLIC.get(char, char))
        previous = char
    return "".join(out)


#: More than enough: each pass that changes anything removes a non-ASCII
#: character or composes two into one.
_MAX_PASSES = 4


def _fold(text: str) -> str:
    folded = _drop_modifiers(text.translate(_DROP_APOSTROPHES))
    folded = unicodedata.normalize("NFKC", folded).casefold()
    folded = _drop_modifiers(folded.translate(_DROP_APOSTROPHES)).replace(_STRAY_DOT, "i")
    # Dropping the stray dot can leave a mark that now composes with the
    # `i` (`İ` + U+0301 → í): compose again before the letter folds see it.
    folded = unicodedata.normalize("NFC", folded)
    folded = _transliterate(folded).translate(_LATIN_FOLDS)
    return _WHITESPACE.sub(" ", folded).strip()


def normalize(text: str) -> str:
    """The canonical search form of `text`. Pure, deterministic, versioned.

    Idempotent: a fold can expose a new composition (`ў` + U+0308 → `o` +
    U+0308 → `ö` → `o`), so folding repeats until nothing changes (v3).
    """
    folded = _fold(text)
    for _ in range(_MAX_PASSES):
        again = _fold(folded)
        if again == folded:
            break
        folded = again
    return folded


# ------------------------------------------------------------------- queries
#: Short suffixes stripped for a stem variant, longest first (one per term). A
#: stem must keep at least `_MIN_STEM` letters, so short words are left alone.
#: Uzbek case and plural endings, the common verb endings (S03.6: kelishgan and
#: kelishdik share `kelish`) and a final vowel (Uzbek -i, Russian -a/-i/-y/-u:
#: kolodka and kolodki share `kolodk`).
_SUFFIXES = (
    "larni",
    "moqda",
    "yapti",
    "imiz",
    "ning",
    "lar",
    "dan",
    "ing",
    "gan",
    "dik",
    "ni",
    "ga",
    "da",
    "di",
    "im",
    "a",
    "i",
    "u",
    "e",
    "o",
    "y",
)
_MIN_STEM = 3
#: A one-letter ending (a final vowel: Uzbek -i, Russian -a/-i/-y/-u) is
#: stripped only from longer words.
_MIN_STEM_ONE_LETTER = 4
#: Voicing alternations (yurak → yuragim, qishloq → qishlogʻi) only for words
#: long enough that the variant is not a different common word.
_MIN_VOICING = 4

TsqueryMode = Literal["and", "or"]


def query_terms(query: str) -> list[str]:
    """The normalized words of a query, in order, without duplicates.

    Only `[a-z0-9]+` survives: the rest of the normalized query (punctuation,
    other scripts, emoji) is left to trigram similarity, and nothing can
    reach `to_tsquery` that it would read as an operator.
    """
    seen: dict[str, None] = {}
    for term in _TERM.findall(normalize(query)):
        seen.setdefault(term, None)
    return list(seen)


#: Function and question words of owner questions (normalized ASCII forms of
#: Uzbek, Russian and English). As prefixes they match almost every message
#: ("the", "na", "va"), so in an any-word search they rank chatter above the
#: message asked about, and in an all-words search they demand words a message
#: has no reason to contain ("where", "qachon"). Query side only (M03 S03.6).
QUERY_STOPWORDS = frozenset(
    {
        # English
        *[
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "by",
            "did",
            "do",
            "does",
            "for",
            "from",
            "had",
            "has",
            "have",
            "he",
            "her",
            "his",
            "how",
            "i",
            "in",
            "is",
            "it",
            "its",
            "me",
            "my",
            "of",
            "on",
            "or",
            "our",
            "she",
            "that",
            "the",
            "their",
            "them",
            "they",
            "this",
            "to",
            "was",
            "we",
            "were",
            "what",
            "when",
            "where",
            "which",
            "who",
            "whom",
            "why",
            "will",
            "with",
            "you",
            "your",
        ],
        # Russian (Cyrillic and typed in Latin, both normalized)
        *[
            "a",
            "by",
            "i",
            "v",
            "vo",
            "vi",
            "da",
            "dlya",
            "do",
            "ego",
            "eyo",
            "yego",
            "yeyo",
            "ili",
            "iz",
            "k",
            "kak",
            "kakoy",
            "kakaya",
            "kogda",
            "kto",
            "gde",
            "li",
            "mi",
            "mne",
            "na",
            "ne",
            "no",
            "o",
            "ob",
            "ot",
            "po",
            "pro",
            "s",
            "so",
            "u",
            "uje",
            "chto",
            "eto",
            "ya",
            "ti",
            "on",
            "ona",
            "oni",
            "vas",
            "nas",
            "nam",
            "vam",
            "za",
            "skolko",
        ],
        # Uzbek
        *[
            "u",
            "bu",
            "shu",
            "va",
            "bilan",
            "uchun",
            "ham",
            "edi",
            "ekan",
            "emas",
            "qayerda",
            "qachon",
            "nima",
            "nimaga",
            "qanday",
            "qaysi",
            "kim",
            "kimga",
            "qancha",
            "necha",
            "mi",
            "mu",
            "esa",
            "yoki",
            "lekin",
            "degan",
        ],
    }
)


def content_terms(terms: Sequence[str]) -> list[str]:
    """`terms` without `QUERY_STOPWORDS`, unless nothing else is left (a query
    of function words only is searched as typed)."""
    kept = [term for term in terms if term not in QUERY_STOPWORDS]
    return kept or list(terms)


def stem_variants(term: str) -> list[str]:
    """`term` plus light Uzbek variants: one stripped suffix, and k/q ↔ g
    voicing at the end of the word. All are used as prefixes."""
    variants: dict[str, None] = {term: None}
    stem = term
    for suffix in _SUFFIXES:
        minimum = _MIN_STEM_ONE_LETTER if len(suffix) == 1 else _MIN_STEM
        if term.endswith(suffix) and len(term) - len(suffix) >= minimum:
            stem = term[: -len(suffix)]
            variants.setdefault(stem, None)
            break
    for word in (term, stem):
        if len(word) >= _MIN_VOICING and word[-1] in "kq":
            variants.setdefault(word[:-1] + "g", None)
    if stem != term and len(stem) >= _MIN_VOICING and stem[-1] == "g":
        # Only a stripped stem (yurag-im): a bare word ending in g is usually
        # just a word ending in g.
        variants.setdefault(stem[:-1] + "k", None)
        variants.setdefault(stem[:-1] + "q", None)
    return list(variants)


#: Query-side spelling alternatives (M03 D54). The key keeps one spelling;
#: people type the others: Russian in Latin (zhdu, khorosho, vy, ego) against
#: the Uzbek-table key of Cyrillic (жду → jdu, хорошо → xorosho, вы → vi,
#: его → yego), and Uzbek on a Russian keyboard (куп for кўп → kup vs kop;
#: raxmat/rahmat; k for q). Each rule is applied to every occurrence, one
#: rule at a time, in both directions. The write side never changes, so the
#: two sides stay symmetric; these only widen what a query term may match.
_SPELLING_RULES: tuple[tuple[str, str], ...] = (
    ("zh", "j"),
    ("j", "zh"),
    ("kh", "x"),
    ("x", "kh"),
    ("x", "h"),
    ("h", "x"),
    ("y", "i"),
    ("i", "y"),
    ("q", "k"),
    ("k", "q"),
    ("u", "o"),
    ("o", "u"),
)
#: Cap on alternatives per term, so a long word cannot blow up the tsquery.
_MAX_ALTERNATIVES = 24


def spelling_variants(term: str) -> list[str]:
    """`term` and its typed-spelling alternatives (query side only)."""
    variants: dict[str, None] = {term: None}
    for source, target in _SPELLING_RULES:
        if source in term:
            variants.setdefault(term.replace(source, target), None)
    if term.startswith("ye"):
        variants.setdefault(term[1:], None)  # yego → ego
    elif term.startswith("e"):
        variants.setdefault("y" + term, None)  # ego → yego
    return list(variants)


def term_alternatives(term: str, *, limit: int = _MAX_ALTERNATIVES) -> list[str]:
    """Every prefix a query term may match: the term, its stems, then the
    spellings of each. Alternatives shorter than two letters are dropped (a
    one-letter prefix matches almost everything); the term itself is kept."""
    out: dict[str, None] = {term: None}
    for stem in stem_variants(term):
        for variant in spelling_variants(stem):
            if len(out) >= limit:
                return list(out)
            if len(variant) >= _MIN_ALTERNATIVE:
                out.setdefault(variant, None)
    return list(out)


#: Bounds on one tsquery: lexemes in total and terms searched.
MAX_TSQUERY_LEXEMES = 200
MAX_TSQUERY_TERMS = 50
_MIN_ALTERNATIVE = 2


def build_tsquery(terms: Sequence[str], mode: TsqueryMode = "and") -> str | None:
    """A `to_tsquery('simple', …)` string: each term's variants as prefixes,
    OR-ed together; terms joined by `&` (all words) or `|` (any word).

    Terms that are not plain `[a-z0-9]+` are dropped, so the result is safe
    to bind even if a caller skipped `query_terms`. At most
    `MAX_TSQUERY_TERMS` terms and `MAX_TSQUERY_LEXEMES` lexemes in total: a
    long query gets fewer spelling alternatives per term.
    """
    plain = [term for term in terms if _TERM.fullmatch(term)][:MAX_TSQUERY_TERMS]
    if not plain:
        return None
    per_term = max(1, min(_MAX_ALTERNATIVES, MAX_TSQUERY_LEXEMES // len(plain)))
    groups: list[str] = []
    for term in plain:
        alternatives = " | ".join(
            f"{variant}:*" for variant in term_alternatives(term, limit=per_term)
        )
        groups.append(f"({alternatives})")
    if not groups:
        return None
    return (" & " if mode == "and" else " | ").join(groups)


# --------------------------------------------------------------- tagging
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


#: Frequent, short function words: a vocabulary heuristic for Latin text.
#: Normalized (ASCII) forms, so they match after `normalize`.
_LATIN_MARKERS: dict[str, frozenset[str]] = {
    "uz": frozenset(
        {
            "va",
            "bu",
            "men",
            "sen",
            "siz",
            "biz",
            "ular",
            "bilan",
            "uchun",
            "emas",
            "ham",
            "yoq",
            "bor",
            "nima",
            "qanday",
            "qachon",
            "qayerda",
            "rahmat",
            "salom",
            "yaxshi",
            "kerak",
            "ertaga",
            "bugun",
            "keyin",
            "hozir",
            "mayli",
            "xop",
        }
    ),
    "en": frozenset(
        {
            "the",
            "and",
            "is",
            "are",
            "you",
            "to",
            "of",
            "in",
            "it",
            "that",
            "what",
            "for",
            "with",
            "will",
            "can",
            "please",
            "thanks",
            "hello",
            "tomorrow",
            "today",
        }
    ),
    "ru": frozenset(
        {
            "privet",
            "kak",
            "chto",
            "eto",
            "ne",
            "da",
            "net",
            "spasibo",
            "seychas",
            "zavtra",
            "segodnya",
            "pozhaluysta",
            "khorosho",
            "horosho",
        }
    ),
}


def guess_lang(text: str) -> str | None:
    """A cheap language guess (§11.2), or None when there is no signal.

    Cyrillic: Uzbek-only letters ⇒ `uz`, Russian-only letters ⇒ `ru`.
    Latin: Uzbek apostrophe letters (oʻ, gʻ) or the new-alphabet letters ⇒
    `uz`; otherwise the language whose function words occur most, if any.
    """
    letters = set(text)
    if letters & _UZBEK_CYRILLIC_LETTERS:
        return "uz"
    if letters & _RUSSIAN_ONLY_LETTERS:
        return "ru"
    if detect_script(text) != "latin":
        return None
    lowered = unicodedata.normalize("NFKC", text).casefold()
    if re.search(r"[og][ʻʼ‘’'`]", lowered) or set(lowered) & set("ŏğş"):
        return "uz"
    words = _TERM.findall(normalize(text))
    scores = {
        lang: sum(word in markers for word in words) for lang, markers in _LATIN_MARKERS.items()
    }
    best = max(scores, key=lambda lang: scores[lang])
    if scores[best] == 0 or list(scores.values()).count(scores[best]) > 1:
        return None  # no signal, or a tie
    return best
