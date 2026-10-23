"""Search-key normalization v2 (ASCII canonical form), query terms and tagging
(ARCHITECTURE §11.2, M03 D44).

Synthetic samples only. The point of v2 is convergence: 1995 Latin, the new
Latin alphabet, apostrophe-less Latin and Cyrillic must all produce one key,
because the same function runs at write time and at query time.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assistant.memory.search import (
    NORMALIZER_VERSION,
    build_tsquery,
    detect_script,
    guess_lang,
    normalize,
    query_terms,
    stem_variants,
)


def test_version_two_is_the_ascii_canonical_form() -> None:
    assert NORMALIZER_VERSION == 2


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


@given(st.text(alphabet=_ALPHABET, max_size=40))
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
    ("term", "expected"),
    [
        ("yuragim", {"yuragim", "yurag", "yurak", "yuraq"}),
        ("kitoblarni", {"kitoblarni", "kitob"}),
        ("uydan", {"uydan"}),  # the stem "uy" is too short: untouched
        ("yurak", {"yurak", "yurag"}),
        ("qishloq", {"qishloq", "qishlog"}),
        ("ga", {"ga"}),
    ],
)
def test_light_uzbek_stem_variants(term: str, expected: set[str]) -> None:
    assert set(stem_variants(term)) == expected
    assert stem_variants(term)[0] == term


def test_build_tsquery_and_or_modes() -> None:
    assert build_tsquery(["salom", "ertaga"], "and") == "(salom:*) & (ertaga:* | erta:*)"
    assert build_tsquery(["salom", "ertaga"], "or") == "(salom:*) | (ertaga:* | erta:*)"
    assert build_tsquery(["yurak"]) == "(yurak:* | yurag:*)"


def test_build_tsquery_drops_anything_but_plain_terms() -> None:
    assert build_tsquery(["ok", "a&b", "x:*", "!", ""]) == "(ok:*)"
    assert build_tsquery([]) is None
    assert build_tsquery(["&"]) is None


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
