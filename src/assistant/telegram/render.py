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
from urllib.parse import urlsplit

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

#: Recognise only raw markup. An escaped contact tag must never become a tag.
_RAW_TAG_RE = re.compile(r"<(/?)([a-zA-Z0-9-]+)((?:\s[^<>]*?)?)>")
#: Telegram HTML supports four named entities and numerical entities (TF PQ-12).
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|#[0-9]+|#x[0-9a-fA-F]+);")
_HREF_RE = re.compile(r"""\s+href=(?:"([^"<>]*)"|'([^'<>]*)')\s*""")


def escape(text: str) -> str:
    """Escape every HTML-special character. For untrusted text."""
    return html.escape(text, quote=False)


def sanitize(text: str) -> str:
    """Keep application markup and existing entities; escape other text once.

    Callers escape untrusted fields with `escape` before adding markup. Never
    unescape the input: `&lt;b&gt;` is literal content, not application markup.
    Bare ampersands and angle brackets in application text are still safe.
    """
    matches = list(_RAW_TAG_RE.finditer(text))
    tags = _paired_tags(matches)
    parts: list[str] = []
    start = 0
    for match, tag in zip(matches, tags, strict=True):
        parts.append(_escape_text(text[start : match.start()]))
        parts.append(tag)
        start = match.end()
    parts.append(_escape_text(text[start:]))
    return "".join(parts)


def _paired_tags(matches: list[re.Match[str]]) -> list[str]:
    """Keep markup only when both sides form an accepted, nested pair.

    Default literal tokens also cover orphan/unclosed tags. Per-name stacks
    find a closer's opening without rescanning deep malformed nesting; any
    crossed openings stay literal rather than leave active unmatched tags.
    """
    rendered = [escape(match.group(0)) for match in matches]
    candidates = [_sanitize_tag(match) for match in matches]
    stack: list[int] = []
    by_name: dict[str, list[int]] = {}
    for index, match in enumerate(matches):
        name = match.group(2).lower()
        if not match.group(1):
            stack.append(index)
            by_name.setdefault(name, []).append(index)
            continue
        openings = by_name.get(name)
        if not openings:
            continue
        opening = openings[-1]
        while stack:
            popped = stack.pop()
            by_name[matches[popped].group(2).lower()].pop()
            if popped == opening:
                break
        if candidates[opening].startswith("<") and candidates[index].startswith("<"):
            rendered[opening] = candidates[opening]
            rendered[index] = candidates[index]
    return rendered


def _escape_text(text: str) -> str:
    parts: list[str] = []
    start = 0
    for entity in _ENTITY_RE.finditer(text):
        parts.append(escape(text[start : entity.start()]))
        parts.append(entity.group(0))
        start = entity.end()
    parts.append(escape(text[start:]))
    return "".join(parts)


def _sanitize_tag(match: re.Match[str]) -> str:
    closing, tag, attributes = match.group(1), match.group(2).lower(), match.group(3)
    if tag not in ALLOWED_TAGS:
        return escape(match.group(0))
    if not attributes:
        if tag == "a" and not closing:
            return escape(match.group(0))
        return f"<{closing}{tag}>"
    # Only application HTTP(S) links with exactly one quoted href are allowed.
    href = _HREF_RE.fullmatch(attributes) if tag == "a" and not closing else None
    if href is None:
        return escape(match.group(0))
    url = href.group(1) if href.group(1) is not None else href.group(2)
    try:
        parsed = urlsplit(html.unescape(url))
    except ValueError:
        return escape(match.group(0))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return escape(match.group(0))
    # Preserve entities in the URL too, without unescaping them into syntax.
    quote = '"' if href.group(1) is not None else "'"
    return f"<a href={quote}{_escape_text(url)}{quote}>"


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
