"""Contact name matching for `resolve_contact` (S03.1). Synthetic names only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assistant.conversations.read import DirectoryEntry, match_contacts, resolve_contacts
from assistant.memory.search import NORMALIZER_VERSION, normalize


def entry(
    contact_id: int, display: str | None, username: str | None = None, day: int = 1
) -> DirectoryEntry:
    return DirectoryEntry(
        contact_id=contact_id,
        conversation_id=contact_id + 100,
        display_name=display,
        username=username,
        last_activity_at=datetime(2026, 10, day, tzinfo=UTC),
    )


DIRECTORY = [
    entry(1, "Kamola Aliyeva", "kamola_a"),
    entry(2, "Kamol Tursunov"),
    entry(3, "Камола Юсупова", day=5),
    entry(4, "Jasur", "jasur_dev"),
    entry(5, None, "oybek"),
]


def ids(query: str) -> list[int]:
    return [m.entry.contact_id for m in match_contacts(DIRECTORY, query)]


def test_exact_beats_prefix_and_cyrillic_matches_latin() -> None:
    matches = match_contacts(DIRECTORY, "Kamola Aliyeva")
    assert matches[0].entry.contact_id == 1
    assert matches[0].kind == "exact"


def test_a_first_name_finds_every_script_and_ranks_by_kind() -> None:
    found = match_contacts(DIRECTORY, "Kamola")
    assert {m.entry.contact_id for m in found} >= {1, 3}
    kinds = [m.kind for m in found]
    assert kinds == sorted(kinds, key=["exact", "prefix", "token_prefix", "fuzzy"].index)


def test_a_surname_matches_by_token_prefix() -> None:
    [match] = [m for m in match_contacts(DIRECTORY, "aliyev") if m.entry.contact_id == 1]
    assert match.kind == "token_prefix"


def test_usernames_match_with_or_without_at() -> None:
    assert ids("@jasur_dev")[0] == 4
    assert ids("oybek") == [5]


def test_a_typo_matches_fuzzily() -> None:
    [first, *_] = match_contacts(DIRECTORY, "Jassur")
    assert (first.entry.contact_id, first.kind) == (4, "fuzzy")


def test_nothing_similar_returns_nothing() -> None:
    assert match_contacts(DIRECTORY, "Zebiniso") == []
    assert match_contacts(DIRECTORY, "   ") == []


# ------------------------------------------------------------ PF-03 (ISSUE-004)
NAMES = [
    entry(11, "Shuhrat Karimov"),
    entry(12, "Xurshida Nazarova"),
    entry(13, "Dilnoza"),
    entry(14, "Rustam Gʻaniyev"),
    entry(15, "Jahongir"),
]


def match(query: str) -> list[tuple[int, str]]:
    return [(m.entry.contact_id, m.kind) for m in match_contacts(NAMES, query)]


def test_pf03_x_and_h_are_one_letter_in_both_directions() -> None:
    assert match("Shuxrat Karimov")[0] == (11, "exact")
    assert match("Hurshida")[0] == (12, "prefix")
    assert match("Xurshida")[0] == (12, "prefix")
    assert match("shuhrat")[0][0] == 11


def test_pf03_every_apostrophe_variant_finds_the_same_name() -> None:
    for spelling in ("Rustam G'aniyev", "Rustam Gʻaniyev", "Rustam G’aniyev", "rustam g`aniyev"):
        assert match(spelling)[0] == (14, "exact"), spelling
    assert match("Rustam Ganiyev")[0] == (14, "exact")
    assert match("ganiyev")[0] == (14, "token_prefix")


def test_pf03_a_nickname_style_short_form_is_a_weak_match() -> None:
    [(first, kind)] = match("Shuxa")
    assert (first, kind) == (11, "short_form")
    [jah] = match_contacts(NAMES, "Jaha")
    assert (jah.entry.contact_id, jah.kind, jah.is_weak) == (15, "short_form", True)


def test_a_unique_real_match_is_not_vetoed_by_a_short_form() -> None:
    directory = [*NAMES, entry(16, "Shuha Aliyeva")]
    result = resolve_contacts(directory, "Shuxa")
    assert result.verdict == "unique_strong"
    assert [m.entry.contact_id for m in result.strong] == [16]
    assert result.total_strong == 1
    assert result.selected_contact_id == 16
    assert result.selected_conversation_id == 116


def test_pf03_a_short_form_needs_three_letters_and_a_longer_name() -> None:
    assert all(kind != "short_form" for _, kind in match("Sx"))
    assert all(kind != "short_form" for _, kind in match("Dil"))  # a prefix, not a short form
    # A typo one letter off is a typo (fuzzy), not a nickname.
    assert match("Shuhat")[0] == (11, "fuzzy")
    assert match("Qwerty") == []


def test_pf03_nothing_matching_returns_the_nearest_names_as_not_sure() -> None:
    found = match_contacts(NAMES, "Lola")
    assert [(m.entry.contact_id, m.kind, m.is_weak) for m in found] == [(13, "not_sure", True)]


def test_pf03_not_sure_candidates_are_at_most_three() -> None:
    names = ("Nodira", "Zodira", "Modira", "Sodira")
    similar = [entry(30 + i, name) for i, name in enumerate(names)]
    found = match_contacts(similar, "Ladira", limit=10)
    assert len(found) == 3
    assert {m.kind for m in found} == {"not_sure"}


def test_pf03_a_stranger_name_still_finds_nobody() -> None:
    assert match_contacts(NAMES, "Zebiniso Olimova") == []


def test_pf03_the_persisted_search_normal_form_is_unchanged() -> None:
    """The fold is local to matching: `normalize` keeps x and h apart."""
    assert normalize("Shuxrat") == "shuxrat"
    assert normalize("Shuhrat") == "shuhrat"
    assert NORMALIZER_VERSION == 3


@pytest.mark.parametrize(
    "query",
    ["Kamola", "Kamola Ziyo", "Ziyo Kamola", "Камола Зиё", "Зиё Камола"],
)
def test_full_name_tokens_in_any_order_and_script_are_strong(query: str) -> None:
    result = resolve_contacts([entry(1, "Kamola Ziyo"), entry(2, "Kamariddin Nur")], query)
    assert result.verdict == "unique_strong"
    assert result.selected_contact_id == 1


@pytest.mark.parametrize("query", ["Nora Vale", "Vale Nora", "Нора Вале"])
def test_english_full_name_tokens(query: str) -> None:
    assert resolve_contacts([entry(1, "Nora Vale")], query).verdict == "unique_strong"


@pytest.mark.parametrize("query", ["Shuxrat", "Шуҳрат", "Shuhrat"])
def test_matching_fold_is_name_only(query: str) -> None:
    result = resolve_contacts([entry(1, "Шуҳрат Karimov")], query)
    assert result.verdict == "unique_strong"
    assert result.reason == "full_token"


@pytest.mark.parametrize("query", ["Kamola", "Камола", "Kam"])
def test_all_real_matches_count_before_pagination(query: str) -> None:
    result = resolve_contacts(
        [entry(1, "Kamola Ziyo"), entry(2, "Камола Сафо", day=5)], query, limit=1
    )
    assert result.verdict == "ambiguous_strong"
    assert result.total_strong == 2
    assert result.omitted == 1
    assert len(result.strong) == 1
    assert result.selected_contact_id is None
    assert result.selected_conversation_id is None
    assert result.reason == "multiple_real"


def test_shared_prefix_does_not_pick_the_more_recent_contact() -> None:
    result = resolve_contacts(
        [entry(1, "Kamola Ziyo"), entry(2, "Kamron Safi", day=5)], "Kam", limit=1
    )
    assert result.verdict == "ambiguous_strong"
    assert result.total_strong == 2
    assert result.omitted == 1
    assert result.selected_contact_id is None


def test_weak_suggestions_are_counted_before_the_page_limit() -> None:
    result = resolve_contacts([entry(1, "Shuhrat"), entry(2, "Shuhriddin")], "Shuxa", limit=1)
    assert result.verdict == "weak_only"
    assert len(result.suggestions) == 1
    assert result.omitted == 1
    assert result.reason == "short_form_guess"


@pytest.mark.parametrize("query", ["Kamloa", "Shuxa", "Lola"])
def test_typo_nickname_and_nearest_names_are_only_guesses(query: str) -> None:
    result = resolve_contacts([entry(1, "Kamola Ziyo"), *NAMES], query)
    assert result.verdict == "weak_only"
    assert result.total_strong == 0
    assert result.strong == ()
    assert result.suggestions
    assert all(m.is_weak for m in result.suggestions)
    assert result.selected_contact_id is None


@pytest.mark.parametrize("query", ["Ka", "Kam Kam", "Zebiniso Olimova", " "])
def test_short_or_reused_prefix_is_not_strong(query: str) -> None:
    result = resolve_contacts([entry(1, "Kamola Ziyo")], query)
    assert result.total_strong == 0
    assert result.selected_contact_id is None


def test_exact_full_name_narrows_prefix_alternatives_but_not_duplicates() -> None:
    directory = [entry(1, "Kamola Ziyo"), entry(2, "Kamolaxon Ziyoda")]
    result = resolve_contacts(directory, "Ziyo Kamola")
    assert result.verdict == "unique_strong"
    assert result.reason == "exact_identity"
    assert result.selected_contact_id == 1
    duplicate = resolve_contacts([*directory, entry(3, "Ziyo Kamola")], "Kamola Ziyo")
    assert duplicate.verdict == "ambiguous_strong"
    assert duplicate.total_strong == 2


def test_exact_full_name_does_not_hide_another_complete_token_match() -> None:
    result = resolve_contacts([entry(1, "Nora Vale"), entry(2, "Nora Vale Smith")], "Nora Vale")
    assert result.verdict == "ambiguous_strong"
    assert result.total_strong == 2


def test_distinct_prefixes_can_be_assigned_in_a_different_order() -> None:
    result = resolve_contacts([entry(1, "Kamola Kamron Ziyo")], "Kam Kamol")
    assert result.verdict == "unique_strong"
    assert result.reason == "token_prefix"


@pytest.mark.parametrize("query", ["Rustam G'aniyev", "Gʻaniyev Rustam", "Рустам Ғаниев"])
def test_name_apostrophes_and_script_use_existing_fold(query: str) -> None:
    result = resolve_contacts([entry(1, "Rustam Gʻaniyev")], query)
    assert result.verdict == "unique_strong"
    assert result.selected_contact_id == 1


def test_exact_username_beats_name_evidence_without_script_or_xh_fold() -> None:
    directory = [entry(1, "Nora Vale", "shuxrat"), entry(2, "Shuxrat"), entry(3, "Шухрат")]
    assert resolve_contacts(directory, "@SHUXRAT").selected_contact_id == 1
    assert resolve_contacts(directory, "shuxrat").selected_contact_id == 1
    assert resolve_contacts([entry(1, None, "shuxrat")], "shuhrat").total_strong == 0
    assert resolve_contacts([entry(1, None, "shuxrat")], "шухрат").total_strong == 0
    duplicate = resolve_contacts([entry(1, None, "nora"), entry(2, None, "nora")], "@nora")
    assert duplicate.verdict == "ambiguous_strong"


def test_no_observed_match_has_no_selection_or_suggestions() -> None:
    result = resolve_contacts(NAMES, "Zebiniso Olimova")
    assert result.verdict == "none"
    assert result.reason == "no_observed_match"
    assert result.suggestions == ()
    assert result.selected_contact_id is None
