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
from assistant.agent.draft_owner_schemas import OWNER_DRAFT_MODES, OwnerDraftMode, WordBounds
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
    #: The shown messages the contact wrote: the only ones an owner's draft
    #: may answer by id (PF-12).
    contact_ids: frozenset[int] = frozenset()


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
    instructions = _instructions(prompt, account)
    items, ordered = _chat_items(
        account, messages, notes, previous, contact_name, writing="the next reply"
    )
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


@dataclass(frozen=True, slots=True)
class OwnerDraftInputs:
    """What the owner asked for, beside the chat (PF-10, PF-11).

    `instruction` is the owner's own current control-chat turn, supplied by
    the application (never model-written; owner decision D2). `guidance` is
    the owner_request model's note on it, from an untainted run only
    (decisions R2). `answered_message_id` must be a contact message among the
    shown ones (checked by the Drafter). `current_text` is the card's text
    being revised (`revise` mode), written by the assistant from chat content."""

    mode: OwnerDraftMode = "reply"
    instruction: str | None = None
    guidance: str | None = None
    answered_message_id: int | None = None
    current_text: str | None = None
    bounds: WordBounds | None = None

    def __post_init__(self) -> None:
        if self.mode not in OWNER_DRAFT_MODES:
            raise ValueError("unknown owner draft mode")
        if (self.mode == "revise") != (self.current_text is not None):
            raise ValueError("a revision, and only a revision, has a current draft")
        if self.mode == "new_message" and self.answered_message_id is not None:
            raise ValueError("a new message answers no message")


#: The task line per mode: what kind of message the owner asked for.
OWNER_TASKS: dict[OwnerDraftMode, str] = {
    "reply": "Task: reply. Write the owner's reply to the contact's latest messages.",
    "new_message": (
        "Task: new_message. Write a new message from the owner, built from the owner's "
        "request; it does not have to answer any message in this chat."
    ),
    "revise": "Task: revise. Rewrite the current draft as the owner's request says.",
}


def build_draft_owner(
    *,
    prompt: Prompt,
    account: Account,
    messages: Sequence[SourceMessage],
    now: datetime,
    inputs: OwnerDraftInputs,
    notes: Sequence[MemoryView] = (),
    previous: Mapping[str, Any] | None = None,
    contact_name: str | None = None,
) -> DraftContext:
    """The context of one owner-requested draft: the chat exactly as
    `draft_reply` sees it (one conversation, contact text wrapped as
    untrusted, invariant 4), then the owner's request. The owner's words
    and the assistant's note are owner input (`OWNER_CONTROL`); the current
    draft is written from contact text, so it is wrapped and managed."""
    instructions = _instructions(prompt, account)
    items, ordered = _chat_items(
        account, messages, notes, previous, contact_name, writing="the message"
    )
    task = OWNER_TASKS[inputs.mode]
    if inputs.answered_message_id is not None:
        task = (
            f"Task: {inputs.mode}. Write the owner's reply to the contact's message "
            f"[msg:{inputs.answered_message_id}]."
            if inputs.mode == "reply"
            else f"{task} The message being answered is [msg:{inputs.answered_message_id}]."
        )
    items.append(TextItem("user", task, Provenance.OWNER_CONTROL))
    if inputs.current_text is not None:
        current_draft = wrap(inputs.current_text, Source.DRAFT)
        items.append(
            TextItem(
                "user",
                f"The current draft, written by the assistant from this chat:\n{current_draft}",
                Provenance.MANAGED,
            )
        )
    if inputs.instruction is not None:
        items.append(
            TextItem(
                "user",
                "The owner's request (their own words, from their chat with you):\n"
                + inputs.instruction,
                Provenance.OWNER_CONTROL,
            )
        )
    if inputs.guidance is not None:
        items.append(
            TextItem(
                "user",
                "The assistant's note on what the owner wants:\n" + inputs.guidance,
                Provenance.OWNER_CONTROL,
            )
        )
    if inputs.bounds is not None:
        items.append(
            TextItem(
                "user",
                f"Length: {inputs.bounds.describe()} (words are separated by spaces).",
                Provenance.OWNER_CONTROL,
            )
        )
    items.append(TextItem("user", f"Current time: {now.isoformat()}", Provenance.OWNER_CONTROL))
    contact_ids = [message.message_id for message in ordered if message.direction == "contact"]
    if inputs.mode == "new_message":
        base: int | None = None
    elif inputs.answered_message_id is not None:
        base = inputs.answered_message_id
    else:
        base = max(contact_ids, default=None)
    return DraftContext(
        instructions,
        tuple(items),
        frozenset(message.message_id for message in ordered),
        base,
        max((message.message_id for message in ordered), default=None),
        frozenset(contact_ids),
    )


def _instructions(prompt: Prompt, account: Account) -> str:
    """The stable prefix: the prompt, then the owner's profile and reply
    preferences (prompt caching, ARCHITECTURE §7.3)."""
    preferences = current(account)
    profile = [
        "Owner profile:",
        f"- preferred language: {account.locale_pref or 'not set'}",
        f"- timezone: {account.timezone}",
        f"- reply language: {preferences['reply_language']}",
        f"- reply tone: {preferences['reply_tone']}",
        f"- reply style: {preferences['reply_style']}",
    ]
    return prompt.text + "\n\n" + "\n".join(profile)


def _chat_items(
    account: Account,
    messages: Sequence[SourceMessage],
    notes: Sequence[MemoryView],
    previous: Mapping[str, Any] | None,
    contact_name: str | None,
    *,
    writing: str,
) -> tuple[list[TextItem], list[SourceMessage]]:
    """One chat as a draft sees it: participants, the owner's notes for this
    contact, the rolling summary and the messages, oldest first."""
    name = wrap(contact_name, Source.CONTACT_NAME) or "(name unknown)"
    items = [
        TextItem(
            "user",
            f"Participants:\nYOU: the owner.\nCONTACT: {name}\n"
            f"Write {writing} as YOU to CONTACT; never write these labels.",
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
    return items, ordered


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
