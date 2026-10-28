"""Untrusted delimiting (ARCHITECTURE §7.3, §8.4; §6 invariant 3b).

Every string written by someone other than the owner — a contact's message,
a contact-chosen name, forwarded or quoted text, and the owner's own messages
in business chats (they quote contacts and are read back from managed
storage) — reaches the model only inside a block it cannot close:

    <untrusted source="contact" trust="untrusted">…</untrusted>

`<`, `>` and `&` inside the text are escaped, so no text can produce a tag:
not a closing `</untrusted>`, not a fake `<untrusted source="owner">`. The
model still reads the text (it is data, and the agent works with it); the
system prompt says it is never an instruction.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum


class Source(StrEnum):
    """Who wrote the wrapped text. A closed set: never taken from content."""

    CONTACT = "contact"
    """A contact's message in a managed chat."""

    OWNER_IN_BUSINESS_CHAT = "owner_business_chat"
    """The owner's own message in a managed chat (may quote the contact)."""

    CONTACT_NAME = "contact_name"
    """A display name or username the contact chose."""

    FORWARDED = "forwarded"
    """Text the owner forwarded into the control chat."""

    QUOTED = "quoted"
    """Text the owner quoted or replied to from another chat."""

    TAINTED_RUN_REPLY = "tainted_run_reply"
    """An earlier assistant reply written while third-party text was in
    context: it may restate that text (M03 D56)."""

    SUMMARY = "summary"
    """A model-written summary of a managed chat (S03.2): derived from
    contact text, so it may carry an injection forward."""

    MEMORY_FROM_TAINTED_RUN = "memory_from_tainted_run"
    """A memory item saved from a run that had other people's text in it
    (the owner confirmed it on a card, S03.3, M03 D70): the words may be
    someone else's."""

    TASK_TITLE = "task_title"
    """A task's title (S05.1): model-written from a contact's message, so it
    may carry an injection forward."""

    TRIAGE_REASON = "triage_reason"
    """A triage verdict's short reason (S05.1 attention v2): model-written
    from a contact's messages, so it may carry an injection forward."""


OPEN_TAG = '<untrusted source="{source}" trust="untrusted">'
CLOSE_TAG = "</untrusted>"

#: Characters a model may read as `<` or `>`: fullwidth and small forms
#: (NFKC also folds these), guillemets, CJK, mathematical, modifier, Canadian
#: syllabics and ornamental angle brackets. Escaped like the real ones, so a
#: lookalike cannot pass for a closing tag either.
_LOOKALIKE_LT = "＜﹤‹〈〈❮⟨˂ᐸ❬❰⧼"
_LOOKALIKE_GT = "＞﹥›〉〉❯⟩˃ᐳ❭❱⧽"
_LOOKALIKES = str.maketrans(
    {
        **dict.fromkeys(_LOOKALIKE_LT, "&lt;"),
        **dict.fromkeys(_LOOKALIKE_GT, "&gt;"),
        # Their canonical decomposition starts with `<` / `>` (+ U+0338): a
        # reader that decomposes would see a bracket. Numeric entities keep
        # the meaning without the bracket.
        "≮": "&#8814;",
        "≯": "&#8815;",
        # COMBINING LONG SOLIDUS OVERLAY: the one combining character that
        # composes with `<` or `>` (checked over all of Unicode). Leading the
        # inner text, it would turn the opening tag's `>` into ≯ for a reader
        # that normalizes to NFC/NFKC.
        "\u0338": "&#824;",
    }
)


def escape(text: str) -> str:
    """Neutralise anything that could form a tag.

    NFKC first, so compatibility forms (fullwidth, small) become the real
    characters and are escaped as such. This matters for the ampersand: the
    fullwidth and small ampersands (U+FF06, U+FE60) are not in the lookalike
    table, and without the fold one of them followed by `lt;` would read as
    `<` to a reader that NFKC-normalizes and then decodes entities.
    Then `&` (so an existing entity cannot be un-escaped into a bracket by
    the reader), `<` and `>`; then the lookalikes NFKC leaves alone, and
    U+0338.
    """
    folded = unicodedata.normalize("NFKC", text)
    escaped = folded.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.translate(_LOOKALIKES)


def wrap(text: str | None, source: Source) -> str | None:
    """`text` inside an untrusted block, or None for None."""
    if text is None:
        return None
    return f"{OPEN_TAG.format(source=Source(source).value)}{escape(text)}{CLOSE_TAG}"
