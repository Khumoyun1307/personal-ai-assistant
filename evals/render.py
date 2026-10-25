"""What a model may see of an E1 conversation (evals/README "E1").

The runner gives a model only `messages` (current text, media markers,
edited/deleted flags) plus the coverage facts the product itself would know.
It never passes `labels`, `original_text` (pre-edit text) or `purged_text`
(deleted text): those fields exist for reviewers only. This module is the one
place that turns a conversation into prompt text, so that rule has one test.
"""

from __future__ import annotations

from evals.schema import Conversation, Message

_MEDIA = {
    "photo": "[photo]",
    "voice": "[voice message, no transcript]",
    "document": "[document]",
}


def _line(conversation: Conversation, message: Message) -> str:
    who = conversation.owner.display_name if message.direction == "owner" else None
    who = who or conversation.contact.display_name
    stamp = message.sent_at.strftime("%Y-%m-%d %H:%M UTC")
    flags = " (edited)" if message.edited else ""
    if message.deleted:
        body = "[deleted]"
    else:
        parts = [_MEDIA[message.media]] if message.media else []
        if message.text:
            parts.append(message.text)
        body = " ".join(parts)
    return f"[{message.id}] {stamp} {who} ({message.direction}){flags}: {body}"


def render_conversation(conversation: Conversation) -> str:
    """The conversation as the product would present it to a model: messages
    in order, then the coverage facts. Never labels, pre-edit or purged text."""
    lines = [f"Conversation with {conversation.contact.display_name}"]
    lines += [_line(conversation, message) for message in conversation.messages]
    facts: list[str] = []
    if conversation.coverage_started_at is not None:
        started = conversation.coverage_started_at.strftime("%Y-%m-%d %H:%M UTC")
        facts.append(f"Coverage started at {started}; earlier history is not available.")
    for gap in conversation.coverage_gaps:
        window = (
            f"{gap.at_risk_from.strftime('%Y-%m-%d %H:%M')} to "
            f"{gap.at_risk_to.strftime('%Y-%m-%d %H:%M')} UTC"
        )
        facts.append(f"Coverage gap {gap.id} ({gap.certainty}, {gap.kind}): {window}.")
    if facts:
        lines += ["", "Coverage facts:", *facts]
    return "\n".join(lines)
