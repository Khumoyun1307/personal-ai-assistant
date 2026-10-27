"""What a model may see of an E1 conversation (evals/README "E1").

The runner gives a model only `messages` (current text, media markers,
edited/deleted flags) plus the coverage facts the product itself would know.
It never passes `labels`, `original_text` (pre-edit text) or `purged_text`
(deleted text): those fields exist for reviewers only. This module is the one
place that turns a conversation into prompt text, so that rule has one test.

Times are shown in the owner's timezone, as the summarizer is shown them (E-w):
a judge reading UTC flagged a correct local time ("08:30" for 03:30 UTC) as
contradicting the chat. An edited message says when it was edited and that its
text is the current one, as the summarizer is shown it (S03.7 A2).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from evals.schema import Conversation, Message

#: The dataset's owner timezone (README: relative dates resolve here); the same
#: value as `evals.corpus.OWNER_TIMEZONE`, which the product context uses.
OWNER_TIMEZONE = "Asia/Tashkent"
#: Version of what `render_conversation` produces, part of the judge's label in a
#: report: v1 showed UTC times (the 2026-10-26 baseline and summarize.v3 runs), v2
#: the owner's local times (E-w, from the summarize.v4 runs on), v3 when an edited
#: message was edited and that its text is the current one, as the summarizer is
#: shown it (S03.7 A2, from the summarize.v6 runs on).
RENDER_VERSION = "render.v3"

_MEDIA = {
    "photo": "[photo]",
    "voice": "[voice message, no transcript]",
    "document": "[document]",
}


def _local(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _line(conversation: Conversation, message: Message, zone: ZoneInfo) -> str:
    who = conversation.owner.display_name if message.direction == "owner" else None
    who = who or conversation.contact.display_name
    flags = (
        f" (edited at {_local(message.edited_at, zone)}; this is its current text)"
        if message.edited_at is not None
        else ""
    )
    if message.deleted:
        flags = " (edited)" if message.edited_at is not None else ""  # no text to call current
        body = "[deleted]"
    else:
        parts = [_MEDIA[message.media]] if message.media else []
        if message.text:
            parts.append(message.text)
        body = " ".join(parts)
    return (
        f"[{message.id}] {_local(message.sent_at, zone)} {who} ({message.direction}){flags}: {body}"
    )


def render_conversation(conversation: Conversation, timezone: str = OWNER_TIMEZONE) -> str:
    """The conversation as the product would present it to a model: messages
    in order (times in the owner's timezone), then the coverage facts. Never
    labels, pre-edit or purged text."""
    zone = ZoneInfo(timezone)
    lines = [
        f"Conversation with {conversation.contact.display_name} "
        f"(times in the owner's timezone, {timezone})"
    ]
    lines += [_line(conversation, message, zone) for message in conversation.messages]
    facts: list[str] = []
    if conversation.coverage_started_at is not None:
        started = _local(conversation.coverage_started_at, zone)
        facts.append(f"Coverage started at {started}; earlier history is not available.")
    for gap in conversation.coverage_gaps:
        window = f"{_local(gap.at_risk_from, zone)} to {_local(gap.at_risk_to, zone)}"
        facts.append(f"Coverage gap {gap.id} ({gap.certainty}, {gap.kind}): {window}.")
    if facts:
        lines += ["", "Coverage facts:", *facts]
    return "\n".join(lines)
