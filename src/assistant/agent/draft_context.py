"""Context for one contact-facing draft; loaders enforce account and chat scope."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.accounts.models import Account
from assistant.accounts.preferences import current
from assistant.agent.context import SUMMARY_MESSAGE_CHARS, SUMMARY_SENDER_LABELS, note_text
from assistant.agent.prompt_registry import Prompt
from assistant.agent.untrusted import Source, wrap
from assistant.llm.port import Provenance, TextItem
from assistant.memory.items import MemoryView
from assistant.memory.summaries import SourceMessage


@dataclass(frozen=True, slots=True)
class DraftContext:
    instructions: str
    items: tuple[TextItem, ...]
    shown_ids: frozenset[int]
    base_message_id: int | None
    shown_through_message_id: int | None


@dataclass(frozen=True, slots=True)
class DraftRevision:
    """A revision's inputs (P2-T08): the card's current text, written by the
    assistant from chat content, and the owner's own revision message."""

    current_text: str
    owner_message: str


def build_draft_reply(
    *,
    prompt: Prompt,
    account: Account,
    messages: Sequence[SourceMessage],
    now: datetime,
    notes: Sequence[MemoryView] = (),
    previous: Mapping[str, Any] | None = None,
    contact_name: str | None = None,
    guidance: str | None = None,
    run_tainted: bool = False,
    revision: DraftRevision | None = None,
) -> DraftContext:
    if guidance is not None and run_tainted:
        raise ValueError("tainted runs cannot supply draft guidance")
    if guidance is not None and revision is not None:
        raise ValueError("a revision takes the owner's message, not guidance")
    preferences = current(account)
    profile = [
        "Owner profile:",
        f"- preferred language: {account.locale_pref or 'not set'}",
        f"- timezone: {account.timezone}",
        f"- reply language: {preferences['reply_language']}",
        f"- reply tone: {preferences['reply_tone']}",
        f"- reply style: {preferences['reply_style']}",
    ]
    instructions = prompt.text + "\n\n" + "\n".join(profile)
    name = wrap(contact_name, Source.CONTACT_NAME) or "(name unknown)"
    items = [
        TextItem(
            "user",
            f"Participants:\nYOU: the owner.\nCONTACT: {name}\n"
            "Write the next reply as YOU to CONTACT; never write these labels.",
            Provenance.MANAGED,
        )
    ]
    if notes:
        body = "Owner notes and style for this reply:\n" + "\n".join(
            f"- ({note.kind}) {note_text(note)}" for note in notes
        )
        provenance = (
            Provenance.MANAGED if any(note.managed for note in notes) else Provenance.OWNER_CONTROL
        )
        items.append(TextItem("user", body, provenance))
    if previous is not None:
        summary = wrap(json.dumps(previous, ensure_ascii=False, sort_keys=True), Source.SUMMARY)
        items.append(TextItem("user", f"Earlier running summary:\n{summary}", Provenance.MANAGED))
    ordered = sorted(messages, key=lambda message: (message.sent_at, message.message_id))
    body = "\n".join(_message_line(message, account.timezone) for message in ordered)
    items.append(TextItem("user", "Messages in this chat:\n" + body, Provenance.MANAGED))
    if guidance is not None:
        items.append(
            TextItem(
                "user", "Owner instruction for this draft:\n" + guidance, Provenance.OWNER_CONTROL
            )
        )
    if revision is not None:
        current_draft = wrap(revision.current_text, Source.DRAFT)
        items.append(
            TextItem(
                "user",
                "This is a revision. The current draft, written by the assistant from this "
                f"chat:\n{current_draft}",
                Provenance.MANAGED,
            )
        )
        items.append(
            TextItem(
                "user",
                "The owner's revision message (the owner's own words: either the new reply "
                "itself or an instruction for rewriting the current draft):\n"
                + revision.owner_message,
                Provenance.OWNER_CONTROL,
            )
        )
    items.append(TextItem("user", f"Current time: {now.isoformat()}", Provenance.OWNER_CONTROL))
    return DraftContext(
        instructions,
        tuple(items),
        frozenset(message.message_id for message in ordered),
        max(
            (message.message_id for message in ordered if message.direction == "contact"),
            default=None,
        ),
        max((message.message_id for message in ordered), default=None),
    )


def _message_line(message: SourceMessage, timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        zone = ZoneInfo("UTC")
    when = message.sent_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    edited = (
        f" (edited at {message.edited_at.astimezone(zone).strftime('%Y-%m-%d %H:%M')}; "
        "current text)"
        if message.edited_at
        else ""
    )
    label = SUMMARY_SENDER_LABELS.get(message.direction, "UNKNOWN SENDER")
    source = Source.CONTACT if message.direction == "contact" else Source.OWNER_IN_BUSINESS_CHAT
    content = wrap(message.text[:SUMMARY_MESSAGE_CHARS], source) if message.text is not None else ""
    media = (
        f"({message.media_kind or message.content_type}) "
        if message.media_kind or message.text is None
        else ""
    )
    return f"[msg:{message.message_id}] {when} {label}{edited}: {media}{content or ''}"
