"""Search-key normalization v2 (ASCII canonical form), query terms and tagging
(ARCHITECTURE §11.2, M03 D44).

Synthetic samples only. The point of v2 is convergence: 1995 Latin, the new
Latin alphabet, apostrophe-less Latin and Cyrillic must all produce one key,
because the same function runs at write time and at query time.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assistant.memory.search import (
    NORMALIZER_VERSION,
    build_tsquery,
    content_terms,
    detect_script,
    guess_lang,
    normalize,
    query_terms,
    spelling_variants,
    stem_variants,
    term_alternatives,
)


def test_version_three_is_the_idempotent_ascii_canonical_form() -> None:
    """v2 was the ASCII form; v3 composes again after the stray-dot drop and
    folds to a fixed point (S03.1 review 3 item 4), forcing a reindex."""
    assert NORMALIZER_VERSION == 3


# ------------------------------------------------------------ apostrophes
@pytest.mark.parametrize(
    "variant",
    [
        "oʻzbekiston",
        "o'zbekiston",
        "o‘zbekiston",
        "o’zbekiston",
        "o`zbekiston",
        "o´zbekiston",
        "oʼzbekiston",
        "oʹzbekiston",
        "oʽzbekiston",
        "o′zbekiston",
        "O'ZBEKISTON",
        "ozbekiston",
    ],
)
def test_every_apostrophe_variant_and_none_converge(variant: str) -> None:
    assert normalize(variant) == "ozbekiston"


@pytest.mark.parametrize("variant", ["gʻalaba", "g'alaba", "g’alaba", "g`alaba", "galaba"])
def test_g_with_any_apostrophe_or_none_converges(variant: str) -> None:
    assert normalize(variant) == "galaba"


def test_the_tutuq_belgisi_is_removed() -> None:
    assert normalize("ma'no") == normalize("maʼno") == normalize("маъно") == "mano"


# ------------------------------------------------------------ new-alphabet letters
@pytest.mark.parametrize("variant", ["özbek", "ōzbek", "ŏzbek", "ózbek", "Özbek"])
def test_o_letters_of_other_orthographies_fold_to_o(variant: str) -> None:
    assert normalize(variant) == "ozbek"


@pytest.mark.parametrize("variant", ["ğalaba", "ǵalaba"])
def test_g_letters_fold_to_g(variant: str) -> None:
    assert normalize(variant) == "galaba"


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [("şahar", "shahar"), ("çoy", "choy"), ("deñiz", "dengiz")],
)
def test_turkic_keyboard_letters_fold_to_digraphs(typed: str, canonical: str) -> None:
    assert normalize(typed) == canonical


def test_decomposed_input_folds_like_composed() -> None:
    assert normalize("özbek") == "ozbek"


# ------------------------------------------------------------ Cyrillic
@pytest.mark.parametrize(
    ("cyrillic", "latin"),
    [
        ("Ўзбекистон", "ozbekiston"),
        ("ғалаба", "galaba"),
        ("қишлоқ", "qishloq"),
        ("ҳа", "ha"),
        ("хабар", "xabar"),
        ("шаҳар", "shahar"),
        ("чой", "choy"),
        ("ёз", "yoz"),
        ("юлдуз", "yulduz"),
        ("янги", "yangi"),
        ("цирк", "tsirk"),
        ("ер", "yer"),  # е word-initially
        ("келди", "keldi"),  # е after a consonant
        ("поезд", "poyezd"),  # е after a vowel
        ("съезд", "syezd"),  # е after ъ
    ],
)
def test_uzbek_cyrillic_transliterates_to_the_latin_key(cyrillic: str, latin: str) -> None:
    assert normalize(cyrillic) == latin


@pytest.mark.parametrize(
    ("russian", "latin"),
    [
        ("Привет, как дела?", "privet, kak dela?"),
        ("щука", "shchuka"),
        ("мы", "mi"),
        ("это", "eto"),
        ("мой", "moy"),
    ],
)
def test_russian_letters_transliterate_consistently(russian: str, latin: str) -> None:
    assert normalize(russian) == latin


def test_cyrillic_and_every_latin_spelling_share_one_key() -> None:
    keys = {
        normalize(spelling)
        for spelling in ("Ўзбекистон", "Oʻzbekiston", "O'zbekiston", "Ozbekiston", "Özbekiston")
    }
    assert keys == {"ozbekiston"}


# ------------------------------------------------------------ general
def test_whitespace_and_case_are_folded() -> None:
    assert normalize("  Salom\n\tDunyo  ") == "salom dunyo"


def test_nfkc_folds_compatibility_forms() -> None:
    assert normalize("ﬁsh") == "fish"


_ALPHABET = st.sampled_from(
    list("abcdefghijklmnopqrstuvwxyzOGŞÇÑöğşçñōŏóǵ0123456789 \t\n.,?!-")
    + list("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳЎҚҒҲЕ")
    + list("ʻʼ‘’'`´ʹʽ′")
)


#: The idempotence alphabet adds what composes or decomposes: combining
#: marks, the dotted capital I and precomposed letters with accents.
_COMBINING_ALPHABET = st.sampled_from(
    list("abcdefghijklmnopqrstuvwxyzOGŞÇÑöğşçñōŏóǵ0123456789 .")
    + list("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳЎҚҒҲЕ")
    + list("ʻʼ‘’'`´ʹʽ′")
    + list("İıÍíÖŌŎÓĞǴżėǰ")
    + ["\u0300", "\u0301", "\u0302", "\u0303", "\u0304", "\u0306", "\u0307", "\u0308"]
    + ["\u030c", "\u0327", "\u0328", "\u0338", "\u034f", "\u0653", "\u3099"]
)


@given(st.text(alphabet=_COMBINING_ALPHABET, max_size=40))
def test_normalize_is_idempotent(text: str) -> None:
    once = normalize(text)
    assert normalize(once) == once


@given(st.text(alphabet=_ALPHABET, max_size=40))
def test_the_key_is_ascii_for_latin_and_cyrillic_input(text: str) -> None:
    assert normalize(text).isascii()


# ------------------------------------------------------------ query terms
def test_query_terms_are_normalized_words_without_duplicates() -> None:
    assert query_terms("Qachon? Ўзбекистон, ozbekiston! 2026") == ["qachon", "ozbekiston", "2026"]


def test_query_terms_let_no_tsquery_syntax_through() -> None:
    terms = query_terms("salom & | ! ( ) :* <-> 'x' \\ ;DROP")
    assert terms == ["salom", "x", "drop"]
    assert all(term.isalnum() and term.isascii() for term in terms)


def test_query_terms_of_punctuation_only_is_empty() -> None:
    assert query_terms("?! — …") == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("the address Kamola sent", ["address", "kamola", "sent"]),
        ("when does Daniel land", ["daniel", "land"]),
        ("сколько стоит замена колодок", ["stoit", "zamena", "kolodok"]),
        ("предоплата за мёд Эргаш", ["predoplata", "myod", "ergash"]),
        ("Nilufar opa bilan uchrashuv qachon", ["nilufar", "opa", "uchrashuv"]),
    ],
)
def test_function_words_are_not_search_terms(query: str, expected: list[str]) -> None:
    """S03.6: as prefixes, "the", "za", "bilan" match nearly every message, so an
    any-word search ranked chatter (an English message full of "the") first."""
    assert content_terms(query_terms(query)) == expected


def test_a_query_of_function_words_only_is_searched_as_typed() -> None:
    assert content_terms(query_terms("what is this")) == ["what", "is", "this"]


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("yuragim", {"yuragim", "yurag", "yurak", "yuraq"}),
        ("kitoblarni", {"kitoblarni", "kitob"}),
        ("uydan", {"uydan"}),  # the stem "uy" is too short: untouched
        ("yurak", {"yurak", "yurag"}),
        ("qishloq", {"qishloq", "qishlog"}),
        ("ga", {"ga"}),
        # S03.6: verb forms and final vowels (kelishgan / kelishdik, kolodka / kolodki)
        ("kelishgan", {"kelishgan", "kelish"}),
        ("kelishdik", {"kelishdik", "kelish", "kelishdig"}),
        ("kolodka", {"kolodka", "kolodk", "kolodg"}),
        ("narxi", {"narxi", "narx"}),
        ("uyi", {"uyi"}),  # a final vowel is stripped only from longer words
    ],
)
def test_light_uzbek_stem_variants(term: str, expected: set[str]) -> None:
    assert set(stem_variants(term)) == expected
    assert stem_variants(term)[0] == term


def _groups(tsquery: str | None, joiner: str) -> list[set[str]]:
    assert tsquery is not None
    return [
        {alt.strip().removesuffix(":*") for alt in group.strip("() ").split("|")}
        for group in tsquery.split(joiner)
    ]


def test_build_tsquery_and_or_modes() -> None:
    assert build_tsquery(["salom", "ertaga"], "and") == (
        "(salom:* | salum:*) & (ertaga:* | yertaga:* | erta:* | yerta:*)"
    )
    assert build_tsquery(["salom", "ertaga"], "or") == (
        "(salom:* | salum:*) | (ertaga:* | yertaga:* | erta:* | yerta:*)"
    )
    [yurak] = _groups(build_tsquery(["yurak"]), "&")
    assert {"yurak", "yurag"} <= yurak


_TSQUERY_SHAPE = re.compile(
    r"^\([a-z0-9]+:\*( \| [a-z0-9]+:\*)*\)( [&|] \([a-z0-9]+:\*( \| [a-z0-9]+:\*)*\))*$"
)


def test_build_tsquery_drops_anything_but_plain_terms() -> None:
    """The injection guard, strictly: exact output, nothing but the allowed shape."""
    assert build_tsquery(["ok", "a&b", "x:*", "!", ""]) == "(ok:* | oq:* | uk:*)"
    assert build_tsquery([]) is None
    assert build_tsquery(["&"]) is None
    assert build_tsquery(["a'b", "c)d", "e|f", "g:*h"]) is None
    hostile = query_terms("x'); DROP TABLE t; -- & | ! :* <-> (a) 'b'")
    built = build_tsquery(hostile, "or")
    assert built is not None
    assert _TSQUERY_SHAPE.fullmatch(built)


def test_a_long_query_stays_bounded() -> None:
    """Review item 6: at most 200 lexemes per tsquery; no 1-letter alternatives."""
    query = " ".join(f"soz{i}kitob" for i in range(40))[:200]
    terms = query_terms(query)
    built = build_tsquery(terms, "or")
    assert built is not None
    lexemes = [alt.strip().removesuffix(":*") for alt in re.split(r"[()&|]", built) if alt.strip()]
    assert len(lexemes) <= 200
    # Only a query term itself may be a single letter, never an alternative.
    assert all(len(lexeme) >= 2 or lexeme in terms for lexeme in lexemes)
    wide = build_tsquery([f"kuh{i}" for i in range(120)], "or") or ""
    assert wide.count(":*") <= 200
    long_term = build_tsquery(["qishloqlarimizdagi"]) or ""
    assert long_term.count(":*") <= 24


def test_one_letter_alternatives_are_dropped_but_a_one_letter_term_is_kept() -> None:
    assert build_tsquery(["u"]) == "(u:*)"  # not "o:*"
    assert "i:*" not in (build_tsquery(["y"]) or "")


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("zhdu", "жду"),  # zh / j
        ("ego", "его"),  # initial e / ye
        ("vy", "вы"),  # y / i
        ("khorosho", "хорошо"),  # kh / x
        ("kup", "кўп"),  # Uzbek on a Russian keyboard: у for ў
        ("raxmat", "rahmat"),  # x / h
        ("kalesan", "qalesan"),  # k / q
    ],
)
def test_typed_spellings_reach_the_stored_key_both_ways(typed: str, stored: str) -> None:
    """Query-side alternatives (D54): each spelling's terms include the other's key."""
    assert normalize(stored) in term_alternatives(normalize(typed))
    assert normalize(typed) in term_alternatives(normalize(stored))


def test_spelling_alternatives_are_bounded() -> None:
    assert len(term_alternatives("qishloqlarimizdagi")) <= 24
    assert spelling_variants("salom")[0] == "salom"


@pytest.mark.parametrize(
    "variant",
    ["oꞌzbekiston", "oˈzbekiston", "oˊzbekiston", "oˋzbekiston", "oʹzbekiston", "o^zbekiston"],
)
def test_apostrophe_lookalike_modifier_letters_are_dropped(variant: str) -> None:
    assert normalize(variant) == "ozbekiston"


def test_dotted_capital_i_casefolds_without_a_stray_combining_dot() -> None:
    assert normalize("İstanbul") == "istanbul"
    assert normalize("İ").isascii()


@pytest.mark.parametrize("text", ["İ\u0301", "ў\u0308", "ö\u0301", "o\u0308\u0301", "İ\u0308"])
def test_composing_after_the_folds_is_idempotent(text: str) -> None:
    """Review 3 item 4: a mark left after the stray-dot drop or a fold composes
    on the next pass; normalize must already return that fixed point."""
    once = normalize(text)
    assert normalize(once) == once


@pytest.mark.parametrize(("decomposed", "composed"), [("ż", "ż"), ("ė", "ė")])
def test_a_combining_dot_composes_like_the_precomposed_letter(
    decomposed: str, composed: str
) -> None:
    """Review item 8: NFKC first; only the dot casefolding leaves after `i` is dropped."""
    assert normalize(decomposed) == normalize(composed)


# ------------------------------------------------------------ tagging
@pytest.mark.parametrize(
    ("text", "script"),
    [
        ("salom dunyo", "latin"),
        ("салом дунё", "cyrillic"),
        ("салом https://t.me/x", "cyrillic"),  # a URL does not make it mixed
        ("salom дунё qalesan", "mixed"),
        ("12345 !!!", None),
    ],
)
def test_script_detection(text: str, script: str | None) -> None:
    assert detect_script(text) == script


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Ўзбекистон ҳақида", "uz"),
        ("Это щедрый ответ", "ru"),
        ("салом", None),  # shared letters only: no guess
        ("Oʻzbekiston", "uz"),  # the apostrophe letters
        ("Salom, ertaga bugun emas", "uz"),
        ("What time is the meeting tomorrow?", "en"),
        ("privet, kak dela? spasibo", "ru"),
        ("Toshkent", None),  # a name: no signal
        ("12345", None),
    ],
)
def test_language_guess(text: str, lang: str | None) -> None:
    assert guess_lang(text) == lang
