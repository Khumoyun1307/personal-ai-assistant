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


OPEN_TAG = '<untrusted source="{source}" trust="untrusted">'
CLOSE_TAG = "</untrusted>"


#: Characters a model may read as `<` or `>`: fullwidth, small, single
#: guillemets, CJK and ornamental angle brackets. Escaped like the real ones
#: so a lookalike cannot pass for a closing tag either.
_LOOKALIKE_LT = "＜﹤‹〈❮〈"
_LOOKALIKE_GT = "＞﹥›〉❯〉"
_LOOKALIKES = str.maketrans(
    {**dict.fromkeys(_LOOKALIKE_LT, "&lt;"), **dict.fromkeys(_LOOKALIKE_GT, "&gt;")}
)


def escape(text: str) -> str:
    """Neutralise anything that could form a tag. `&` first, so an existing
    entity cannot be un-escaped into a bracket by the reader; lookalike
    brackets become the same entities as real ones."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.translate(_LOOKALIKES)


def wrap(text: str | None, source: Source) -> str | None:
    """`text` inside an untrusted block, or None for None."""
    if text is None:
        return None
    return f"{OPEN_TAG.format(source=Source(source).value)}{escape(text)}{CLOSE_TAG}"
