"""Excerpts use the search normalizer and query-side alternatives; synthetic text."""

import pytest

from assistant.agent.untrusted import Source, wrap
from assistant.memory.search import search_excerpt


@pytest.mark.parametrize(
    ("passage", "query"),
    [
        ("tail marker", "tail marker"),
        ("Шаҳарга учрашув", "shaharga uchrashuv"),
        ("rahmat", "raxmat"),
        ("ўғлим", "oglim"),
        ("şahar", "shahar"),
        ("квартире", "kvartira"),
    ],
)
def test_tail_match_is_visible_in_original_script(passage: str, query: str) -> None:
    value = "Introduction. " * 100 + passage + " closing. " * 100
    excerpt = search_excerpt(value, [query], limit=400)
    assert excerpt is not None
    assert passage in excerpt
    assert excerpt.startswith("…")
    assert excerpt.endswith("…")
    assert len(excerpt) <= 400


def test_highest_ranked_phrase_then_its_first_occurrence_wins() -> None:
    value = "lower phrase " + "padding " * 100 + "winner phrase FIRST "
    value += "padding " * 100 + "winner phrase SECOND"
    excerpt = search_excerpt(value, ["winner phrase", "lower phrase"], limit=400)
    assert excerpt is not None
    assert "winner phrase FIRST" in excerpt
    assert "SECOND" not in excerpt
    assert "lower phrase" not in excerpt


def test_wrapped_excerpt_normalizes_omission_markers() -> None:
    value = "Introduction. " * 100 + "tail match" + " closing. " * 100
    excerpt = search_excerpt(value, ["tail match"], limit=400)
    assert excerpt is not None
    # Raw excerpts use U+2026; the model receives NFKC-normalized ASCII dots.
    assert excerpt.startswith("…")
    assert excerpt.endswith("…")
    wrapped = wrap(excerpt, Source.CONTACT)
    assert wrapped is not None
    assert wrapped.startswith('<untrusted source="contact" trust="untrusted">...')
    assert wrapped.endswith("...</untrusted>")
    assert "tail match" in wrapped
    assert "…" not in wrapped


def test_phrase_precedes_an_earlier_individual_term() -> None:
    value = "winner " + "padding " * 100 + "winner phrase"
    excerpt = search_excerpt(value, ["winner phrase"], limit=400)
    assert excerpt is not None
    assert "winner phrase" in excerpt


@pytest.mark.parametrize("query", ["art", "art exhibition"])
def test_phrase_ignores_substrings_inside_unrelated_words(query: str) -> None:
    value = "party exhibition " + "padding " * 100 + "art exhibition"
    excerpt = search_excerpt(value, [query], limit=400)
    assert excerpt is not None
    assert "art exhibition" in excerpt
    assert "party" not in excerpt
    assert excerpt.startswith("…")
    assert len(excerpt) <= 400


def test_term_fallback_uses_first_occurrence() -> None:
    value = "padding " * 100 + "rahmat FIRST " + "padding " * 100 + "rahmat SECOND"
    excerpt = search_excerpt(value, ["raxmat missingword"], limit=400)
    assert excerpt is not None
    assert "rahmat FIRST" in excerpt
    assert "SECOND" not in excerpt


def test_short_null_and_trigram_only_fallback() -> None:
    assert search_excerpt(None, ["query"], limit=400) is None
    assert search_excerpt("Short retained text", ["text"], limit=400) == "Short retained text"
    assert search_excerpt("a" * 900, ["typo"], limit=400) == "a" * 399 + "…"


def test_offsets_survive_expansion_apostrophes_and_collapsed_whitespace() -> None:
    value = "Шўх ўғил. \n\t" * 100 + "Шаҳарга  учрашув" + " closing. " * 100
    excerpt = search_excerpt(value, ["shaharga uchrashuv"], limit=400)
    assert excerpt is not None
    assert "Шаҳарга  учрашув" in excerpt
    assert len(excerpt) <= 400


@pytest.mark.parametrize("value", ["match " + "a" * 800, "a" * 800 + " match"])
def test_match_at_a_boundary_uses_one_marker(value: str) -> None:
    excerpt = search_excerpt(value, ["match"], limit=400)
    assert excerpt is not None
    assert "match" in excerpt
    assert excerpt.count("…") == 1
    assert len(excerpt) == 400
