"""Verified citations in owner replies (S03.7 B3, ARCHITECTURE §7.2).

The model marks each statement it took from a message with `[msg:<id>]`,
using a `message_id` a tool returned in this run. Before the reply goes out,
the application replaces every marker with the message's source — the
contact's name (or "you") and the local time — read from the database by
the tool, never written by the model. A marker for a message no tool
returned in the run is removed, and one fixed note says so: the owner never
sees a source the run did not retrieve.

Pure: no I/O. Contact names are contact-chosen, so `<`, `>` and `&` are
replaced by lookalikes: the reply's HTML sanitizer restores the tags it
allows, and a name must never become one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from assistant.agent import owner_texts
from assistant.agent.locales import SupportedLocale

#: One marker. A run of adjacent markers becomes one source list.
MARKER = re.compile(r"\[msg:\s*(\d{1,19})\s*\]")
_MARKER_RUN = re.compile(r"[ \t]*(?:\[msg:\s*\d{1,19}\s*\][ \t]*)+")
#: Lookalikes of the three characters, so a name can never form a tag.
_NEUTRALIZE = str.maketrans(
    {
        "<": "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}",
        ">": "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}",
        "&": "\N{FULLWIDTH AMPERSAND}",
    }
)
_TIME_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True, slots=True)
class CitableMessage:
    """A message a tool returned in this run: what its source line needs.
    Built from database rows by the tool; never persisted."""

    conversation_id: int
    contact_name: str | None
    sent_at: datetime
    direction: str


@dataclass(frozen=True, slots=True)
class CitedText:
    text: str
    #: Message ids whose markers became sources, in order of first use.
    cited_ids: tuple[int, ...]
    #: Markers removed because no tool returned their message in this run.
    unverified: int


def _source(message: CitableMessage, *, locale: SupportedLocale, timezone: str) -> str:
    if message.direction == "contact":
        name = (message.contact_name or "").strip() or owner_texts.CONTACT_FALLBACK[locale]
    else:
        name = owner_texts.YOU[locale]
    when = message.sent_at.astimezone(ZoneInfo(timezone)).strftime(_TIME_FORMAT)
    return f"{' '.join(name.split()).translate(_NEUTRALIZE)}, {when}"


def apply_citations(
    text: str,
    citable: Mapping[int, CitableMessage],
    *,
    locale: SupportedLocale,
    timezone: str,
) -> CitedText:
    """`text` with every marker replaced by its source, or removed."""
    cited: dict[int, None] = {}
    unverified = 0

    def replace_run(match: re.Match[str]) -> str:
        nonlocal unverified
        sources: dict[str, None] = {}
        for raw in MARKER.findall(match.group(0)):
            message = citable.get(int(raw))
            if message is None:
                unverified += 1
                continue
            cited.setdefault(int(raw), None)
            sources.setdefault(_source(message, locale=locale, timezone=timezone), None)
        at_line_start = match.start() == 0 or match.string[match.start() - 1] == "\n"
        at_end = match.end() == len(match.string)
        trailing = " " if match.group(0)[-1:] in (" ", "\t") and not at_end else ""
        if not sources:
            return "" if at_line_start else trailing
        lead = "" if at_line_start else " "
        return f"{lead}({'; '.join(sources)}){trailing}"

    rendered = _MARKER_RUN.sub(replace_run, text)
    if unverified:
        rendered = f"{rendered.rstrip()}\n\n{owner_texts.CITATION_REMOVED[locale]}"
    return CitedText(rendered, tuple(cited), unverified)
