"""Verified citations in owner replies (S03.7 B3, ARCHITECTURE §7.2).

The model marks a statement taken from a message with `[msg:<id>]`; the
application replaces each marker with the source it reads from the database
(contact, local time) and removes markers for messages no tool returned in
the run, with one fixed note. Synthetic names and times only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assistant.agent import owner_texts
from assistant.agent.citations import CitableMessage, apply_citations
from assistant.telegram.render import sanitize

TZ = "Asia/Tashkent"  # UTC+5
SENT = datetime(2026, 10, 12, 9, 5, tzinfo=UTC)
CITABLE = {
    101: CitableMessage(conversation_id=1, contact_name="Aziza", sent_at=SENT, direction="contact"),
    102: CitableMessage(
        conversation_id=1, contact_name="Aziza", sent_at=SENT, direction="owner_manual"
    ),
    103: CitableMessage(conversation_id=2, contact_name=None, sent_at=SENT, direction="contact"),
}


def test_markers_for_returned_messages_are_rendered_as_sources() -> None:
    cited = apply_citations(
        "Uchrashuv soat 5 da [msg:101]. Siz rozi bo'lgansiz[msg:102].",
        CITABLE,
        locale="uz-Latn",
        timezone=TZ,
    )

    assert cited.text == (
        "Uchrashuv soat 5 da (Aziza, 2026-10-12 14:05). "
        "Siz rozi bo'lgansiz (siz, 2026-10-12 14:05)."
    )
    assert cited.cited_ids == (101, 102)
    assert cited.unverified == 0


@pytest.mark.parametrize(
    ("locale", "fallback"),
    [("en", "the contact"), ("ru", "собеседник"), ("uz-Latn", "suhbatdosh")],
)
def test_a_source_without_a_stored_name_uses_the_fixed_fallback(locale: str, fallback: str) -> None:
    cited = apply_citations("Done [msg:103].", CITABLE, locale=locale, timezone=TZ)  # type: ignore[arg-type]
    assert cited.text == f"Done ({fallback}, 2026-10-12 14:05)."


def test_adjacent_markers_share_one_source_list() -> None:
    cited = apply_citations("Agreed [msg:101][msg:102].", CITABLE, locale="en", timezone=TZ)
    assert cited.text == "Agreed (Aziza, 2026-10-12 14:05; you, 2026-10-12 14:05)."


def test_a_marker_for_a_message_no_tool_returned_is_removed_with_a_note() -> None:
    cited = apply_citations(
        "At 5 [msg:101]. At 6 [msg:999]. At 7 [msg:998].", CITABLE, locale="en", timezone=TZ
    )

    note = owner_texts.CITATION_REMOVED["en"]
    assert cited.text == f"At 5 (Aziza, 2026-10-12 14:05). At 6. At 7.\n\n{note}"
    assert cited.text.count(note) == 1
    assert "999" not in cited.text
    assert cited.cited_ids == (101,)
    assert cited.unverified == 2


def test_text_without_markers_is_unchanged() -> None:
    cited = apply_citations("Nothing found.", CITABLE, locale="en", timezone=TZ)
    assert (cited.text, cited.cited_ids, cited.unverified) == ("Nothing found.", (), 0)


def test_a_contact_name_cannot_inject_telegram_html() -> None:
    hostile = {
        7: CitableMessage(
            conversation_id=1,
            contact_name='<b>Boss</b> <a href="https://x.invalid">&</a>',
            sent_at=SENT,
            direction="contact",
        )
    }
    cited = apply_citations("Said so [msg:7].", hostile, locale="en", timezone=TZ)

    assert "<" not in cited.text
    assert ">" not in cited.text
    assert "&" not in cited.text
    rendered = sanitize(cited.text)
    assert "<b>" not in rendered
    assert "<a" not in rendered


@pytest.mark.parametrize("locale", ["uz-Latn", "uz-Cyrl", "ru", "en"])
def test_every_fixed_text_exists_in_every_supported_locale(locale: str) -> None:
    for table in (
        owner_texts.YOU,
        owner_texts.CONTACT_FALLBACK,
        owner_texts.CITATION_REMOVED,
        owner_texts.CHAT_ACCESS_OFF,
    ):
        assert table[locale].strip()  # type: ignore[index]
