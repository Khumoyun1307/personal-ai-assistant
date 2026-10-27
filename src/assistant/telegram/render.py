"""Control-chat rendering (ARCHITECTURE §6.7).

Telegram's HTML parse mode is not HTML: it accepts a short tag list and rejects
anything else with a 400. Worse, an unescaped `<` in a *contact's* message would
either break the send or be interpreted as markup — so sanitising is a
correctness and an injection concern, not cosmetics.

Splitting applies to the **control chat only**. A business send is one action,
one message (§6.8): silently splitting a reply sent as the owner would put half
a thought in front of a contact.
"""

from __future__ import annotations

import html
import re

from assistant.core.delivery import MAX_TEXT_LENGTH

#: Telegram's supported HTML tags. Anything else is escaped, not stripped, so
#: the owner still sees what the text actually said.
ALLOWED_TAGS: frozenset[str] = frozenset(
    {"b", "strong", "i", "em", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
)

#: Telegram's hard limit for a text message (defined once in core.delivery).
MAX_MESSAGE_CHARS = MAX_TEXT_LENGTH

#: Leave room for a continuation marker without risking the limit.
_SPLIT_BUDGET = MAX_MESSAGE_CHARS - 64

#: After escaping, a tag looks like `&lt;/b&gt;`; this matches those.
_ESCAPED_TAG_RE = re.compile(r"&lt;(/?)([a-zA-Z0-9-]+)((?:\s[^&]*?)?)&gt;")


def escape(text: str) -> str:
    """Escape every HTML-special character. For untrusted text."""
    return html.escape(text, quote=False)


def sanitize(text: str) -> str:
    """Escape everything, then restore only the allowed tags.

    Deny-by-default: a tag Telegram does not know fails the send with a 400,
    and a tag we did not expect could change how the message reads.
    """
    return _ESCAPED_TAG_RE.sub(_restore_tag, html.escape(text, quote=False))


def _restore_tag(match: re.Match[str]) -> str:
    closing, tag, attributes = match.group(1), match.group(2).lower(), match.group(3)
    if tag not in ALLOWED_TAGS:
        return match.group(0)
    # Only `a` carries an attribute we support, and only href.
    if attributes and not (tag == "a" and "href=" in attributes):
        return match.group(0)
    return f"<{closing}{tag}{attributes.replace('&quot;', '"')}>"


def split_message(text: str, limit: int = _SPLIT_BUDGET) -> list[str]:
    """Split on block boundaries, never mid-word (§6.7).

    Prefers blank lines, then newlines, then spaces. A single run longer than
    the limit (a pasted token, a long URL) is cut hard — losing it entirely
    would be worse.
    """
    if len(text) <= limit:
        return [text] if text else []

    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit  # one unbroken run; cut it rather than drop it
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def render_control_message(text: str) -> list[str]:
    """Sanitize and split a message bound for the control chat."""
    return split_message(sanitize(text))
