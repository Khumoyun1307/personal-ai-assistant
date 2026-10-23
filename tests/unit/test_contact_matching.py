"""Contact name matching for `resolve_contact` (S03.1). Synthetic names only."""

from __future__ import annotations

from datetime import UTC, datetime

from assistant.conversations.read import DirectoryEntry, match_contacts


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
