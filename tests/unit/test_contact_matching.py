"""Contact name matching for `resolve_contact` (S03.1). Synthetic names only."""

from __future__ import annotations

from datetime import UTC, datetime

from assistant.conversations.read import DirectoryEntry, match_contacts
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


def test_pf03_a_short_form_ranks_below_every_real_match() -> None:
    directory = [*NAMES, entry(16, "Shuha Aliyeva")]
    found = match_contacts(directory, "Shuxa")
    assert [(m.entry.contact_id, m.kind) for m in found] == [(16, "prefix"), (11, "short_form")]
    assert [m.is_weak for m in found] == [False, True]


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
