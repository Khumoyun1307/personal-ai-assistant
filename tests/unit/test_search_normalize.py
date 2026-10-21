"""Search-key normalization v1 and script tagging (ARCHITECTURE §11.2).

Synthetic samples only. v1 is case/apostrophe/whitespace folding; the
cross-script work is S03.1, which bumps `NORMALIZER_VERSION`.
"""

from __future__ import annotations

import pytest

from assistant.memory.search import NORMALIZER_VERSION, detect_script, guess_lang, normalize


def test_version_is_one_until_s03_1_finalises_the_normalizer() -> None:
    assert NORMALIZER_VERSION == 1


@pytest.mark.parametrize(
    "variant",
    ["oʻzbek", "o'zbek", "o‘zbek", "o’zbek", "o`zbek", "O'ZBEK"],
)
def test_every_apostrophe_variant_converges(variant: str) -> None:
    assert normalize(variant) == "o'zbek"


def test_whitespace_and_case_are_folded() -> None:
    assert normalize("  Salom\n\tDunyo  ") == "salom dunyo"


def test_nfkc_folds_compatibility_forms() -> None:
    assert normalize("ﬁsh") == "fish"


def test_normalize_is_idempotent() -> None:
    once = normalize("Oʻzbekiston  Respublikasi")
    assert normalize(once) == once


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
        ("salom", None),  # Latin: untagged in v1
    ],
)
def test_language_guess_uses_script_exclusive_letters(text: str, lang: str | None) -> None:
    assert guess_lang(text) == lang
