"""Context builders (ARCHITECTURE §7.3).

Explicit, testable functions per run type. Order follows the prompt-caching
rule: the stable prompt first, then the rarely-changing owner profile, then the
volatile run context, then the conversation, then the new turn.

`summarize` (S03.2) sees ONE conversation: its messages and its own earlier
summary, never another chat's (§6 invariant 4), all wrapped as untrusted.

`owner_request` sees the owner's profile and recent control-chat turns. Turns
flagged `contains_managed` are excluded unless `ai_managed` is active
(ARCHITECTURE §14.3) — defence in depth behind the consent wrapper.
Forwarded and quoted third-party text is wrapped as untrusted, and a context
that carries any of it, or managed content, starts the run tainted (§7.1).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.accounts.models import Account, ControlTurn
from assistant.agent.prompt_registry import Prompt
from assistant.agent.untrusted import Source, wrap
from assistant.llm.port import Provenance, TextItem
from assistant.memory.items import MemoryView
from assistant.memory.summaries import SourceMessage

#: How many earlier control turns an owner request sees.
OWNER_HISTORY_TURNS = 20


@dataclass(frozen=True, slots=True)
class OwnerRequestContext:
    instructions: str
    items: tuple[TextItem, ...]
    #: The context already carries third-party text or managed content.
    tainted: bool = False
    #: ...and it is in the current turn itself (not only inherited from
    #: history): the reply will taint later runs (D56).
    directly_tainted: bool = False


def build_owner_request(
    *,
    prompt: Prompt,
    account: Account,
    history: list[ControlTurn],
    turn: ControlTurn,
    now: datetime,
    managed_allowed: bool = False,
    memory: Sequence[MemoryView] = (),
) -> OwnerRequestContext:
    profile = [
        "Owner profile:",
        f"- preferred language: {account.locale_pref or 'not set'}",
        f"- timezone: {account.timezone}",
    ]
    sections = [prompt.text, "\n".join(profile), f"Current time (UTC): {now:%Y-%m-%d %H:%M}"]
    instructions = "\n\n".join(sections)

    items: list[TextItem] = []
    if memory:
        # The owner's saved items travel as an input item with provenance, so
        # the consent wrapper sees them (safety review V2/N3). Only the
        # owner's own words appear; an item from other people's text is a
        # placeholder, read with `list_memories` (which taints the run, V1).
        items.append(
            TextItem("user", render_owner_memory(memory, managed_allowed), Provenance.OWNER_CONTROL)
        )
    items += [
        TextItem(
            role="user" if past.role == "owner" else "assistant",
            text=render_turn(past),
            provenance=Provenance.MANAGED if past.contains_managed else Provenance.OWNER_CONTROL,
        )
        for past in history
        if past.text
    ]
    if turn.contains_managed and not managed_allowed:
        # The owner forwarded or quoted a message from one of their managed
        # chats, without `ai_managed`: its text must not reach the model
        # (§6 invariant 9). The model is told, so it can say so honestly.
        current = TextItem("user", withheld_turn(turn), Provenance.OWNER_CONTROL)
    else:
        current = TextItem(
            role="user",
            text=render_turn(turn) or "",
            provenance=Provenance.MANAGED if turn.contains_managed else Provenance.OWNER_CONTROL,
        )
    items.append(current)
    direct = carries_third_party(turn)
    tainted = direct or any(carries_third_party(t) for t in history)
    return OwnerRequestContext(
        instructions=instructions, items=tuple(items), tainted=tainted, directly_tainted=direct
    )


def note_text(item: MemoryView) -> str:
    """A note's words as a contact-facing run sees them: wrapped as
    untrusted when they may be someone else's (M03 D70)."""
    if item.tainted:
        return wrap(item.content, Source.MEMORY_FROM_TAINTED_RUN) or ""
    return item.content


def owner_memory_line(item: MemoryView, *, managed_allowed: bool) -> str | None:
    """One owner item in an owner run. Never the text of an item saved from
    other people's text (safety review V1): a placeholder points the model at
    `list_memories`, whose result taints the run; without `ai_managed` such
    an item is not mentioned at all (it may hold managed content, V2)."""
    flags = " (for replies)" if item.applies_to_replies else ""
    if item.tainted or item.managed:
        if item.managed and not managed_allowed:
            return None
        return (
            f"- [memory {item.memory_id}] {item.kind}{flags}: (saved from other people's text; "
            "not shown here — read it with list_memories if the request needs it)"
        )
    return f"- [memory {item.memory_id}] {item.kind}{flags}: {item.content}"


def render_owner_memory(memory: Sequence[MemoryView], managed_allowed: bool) -> str:
    """The owner's own saved instructions, preferences and facts (owner
    scope only; contact notes come through `list_memories`)."""
    lines = ["What the owner asked you to remember (owner's notes, newest first):"]
    lines += [
        line
        for item in memory
        if (line := owner_memory_line(item, managed_allowed=managed_allowed)) is not None
    ]
    return "\n".join(lines)


WITHHELD_FORWARD = (
    "(The owner forwarded a message from one of their chats with other people. Its text "
    "is withheld: the owner has not agreed to AI processing of those chats. Say so, and "
    "mention /privacy managed.)"
)
WITHHELD_QUOTE = (
    "(The owner quoted a message from one of their chats with other people; the quote is "
    "withheld: the owner has not agreed to AI processing of those chats.)"
)


def withheld_turn(turn: ControlTurn) -> str:
    if turn.quoted_text is not None:
        return f"{turn.text or ''}\n\n{WITHHELD_QUOTE}"
    return WITHHELD_FORWARD


def carries_third_party(turn: ControlTurn) -> bool:
    """Whether a turn taints a run that includes it (§7.1, M03 D45, D56).

    An owner turn: it carries forwarded/quoted text or managed content. An
    assistant reply: only when the run that wrote it was tainted by its OWN
    inputs (`third_party`); a reply tainted only by inherited history does
    not pass the taint on, so taint decays once its source leaves the window.
    """
    if turn.role != "owner":
        return bool(turn.third_party)
    return bool(turn.third_party or turn.quoted_text is not None or turn.contains_managed)


#: Sender labels for a summarize run, from classification — never from text.
SUMMARY_SENDER_LABELS = {
    "contact": "contact",
    "owner_manual": "owner",
    "owner_automated": "owner's account (automatic message)",
    "assistant_sent": "assistant (sent for the owner)",
}
#: One message's text is cut here in a summarize context.
SUMMARY_MESSAGE_CHARS = 2000

#: How a summary run names the owner's language tag (`accounts.locale_pref`):
#: the script spelled out, since a bare `uz-Latn` lost to Cyrillic chats (S03.6).
SUMMARY_LANGUAGE_NAMES = {
    "uz-latn": "Uzbek written in Latin letters",
    "uz-cyrl": "Uzbek written in Cyrillic letters",
    "uz": "Uzbek",
    "ru": "Russian",
    "en": "English",
}


def summary_language(tag: str | None) -> str | None:
    """The owner's language as the summary run names it, or None (no tag, or
    one we have no name for: the prompt then falls back to the tag alone)."""
    if not tag:
        return None
    return SUMMARY_LANGUAGE_NAMES.get(tag.strip().lower())


@dataclass(frozen=True, slots=True)
class SummarizeContext:
    instructions: str
    items: tuple[TextItem, ...]
    #: Ids of the messages shown: the only ones an item may cite.
    shown_ids: frozenset[int]


def build_summarize(
    *,
    prompt: Prompt,
    account: Account,
    messages: Sequence[SourceMessage],
    previous: Mapping[str, Any] | None = None,
    period: tuple[datetime, datetime] | None = None,
    notes: Sequence[MemoryView] = (),
) -> SummarizeContext:
    """The context of one `summarize` run: ONE conversation's messages and,
    when updating, that conversation's own previous summary — nothing from
    any other chat (§6 invariant 4). Contact text and the earlier summary
    are wrapped as untrusted; sender labels come from classification.
    `notes` are the owner's notes about this contact and this chat only,
    loaded by the §7.3 rule (`memory/items.py::contact_facing`, S03.3)."""
    timezone = account.timezone
    language = summary_language(account.locale_pref)
    profile = [
        "Owner profile:",
        f"- preferred language: {account.locale_pref or 'not set'}"
        + (f" ({language})" if language else ""),
        f"- timezone: {timezone} (message times below are in it)",
    ]
    if period is not None:
        task = (
            "Task: summarize what happened in this chat between "
            f"{_local(period[0], timezone)} and {_local(period[1], timezone)} "
            "(the period summary). Only the messages of that period are shown."
        )
    elif previous is not None:
        task = (
            "Task: update the running summary of this chat with the new messages "
            "(the current summary covers the earlier ones)."
        )
    else:
        task = "Task: write the running summary of this chat from its messages."
    if language:
        # Last in the instructions: in S03.6 the bare tag lost to chats written
        # in another script.
        task += (
            f" Write every part of the summary in {language}, whatever the chat's "
            "language or script."
        )
    instructions = "\n\n".join([prompt.text, "\n".join(profile), task])

    items: list[TextItem] = []
    if notes:
        lines = ["The owner's notes about this contact and chat (background, not chat messages):"]
        # No ids: a note must never be citable as a message (review nit; the
        # uncited-item guard, D65, accepts only shown message ids).
        lines += [f"- ({note.kind}) {note_text(note)}" for note in notes]
        provenance = (
            Provenance.MANAGED if any(n.managed for n in notes) else Provenance.OWNER_CONTROL
        )
        items.append(TextItem("user", "\n".join(lines), provenance))
    if previous is not None and period is None:
        body = wrap(json.dumps(previous, ensure_ascii=False, sort_keys=True), Source.SUMMARY)
        items.append(
            TextItem(
                "user", f"Current running summary (earlier messages):\n{body}", Provenance.MANAGED
            )
        )
    ordered = sorted(messages, key=lambda m: (m.sent_at, m.message_id))
    lines = [_summary_line(message, timezone) for message in ordered]
    header = "Messages of the period:" if period is not None else "New messages:"
    items.append(TextItem("user", header + "\n" + "\n".join(lines), Provenance.MANAGED))
    return SummarizeContext(
        instructions=instructions,
        items=tuple(items),
        shown_ids=frozenset(m.message_id for m in messages),
    )


def _local(moment: datetime, timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        zone = ZoneInfo("UTC")
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _summary_line(message: SourceMessage, timezone: str) -> str:
    label = SUMMARY_SENDER_LABELS.get(message.direction, "unknown sender")
    edited = " (edited)" if message.edited else ""
    parts: list[str] = []
    if message.media_kind is not None or message.text is None:
        parts.append(f"({message.media_kind or message.content_type})")
    if message.text is not None:
        text = message.text
        if len(text) > SUMMARY_MESSAGE_CHARS:
            text = text[:SUMMARY_MESSAGE_CHARS] + "…"
        source = Source.CONTACT if message.direction == "contact" else Source.OWNER_IN_BUSINESS_CHAT
        parts.append(wrap(text, source) or "")
    when = _local(message.sent_at, timezone)
    return f"[{message.message_id}] {when} {label}{edited}: {' '.join(parts)}"


def render_turn(turn: ControlTurn) -> str:
    """A control turn as the model sees it. Third-party text is wrapped as
    untrusted: a forwarded message entirely, a quote next to the owner's own
    words (§7.3), and every reply written by a tainted run (it may restate
    such text; D56(a))."""
    text = turn.text or ""
    if turn.role != "owner":
        if turn.from_tainted_run or turn.third_party:
            return wrap(text, Source.TAINTED_RUN_REPLY) or ""
        return text
    if turn.quoted_text is not None:
        quote = wrap(turn.quoted_text, Source.QUOTED)
        return f"{text}\n\n(The owner is quoting this from another chat:)\n{quote}"
    if turn.third_party:
        forwarded = wrap(text, Source.FORWARDED)
        return f"(The owner forwarded this message from someone else:)\n{forwarded}"
    return text
