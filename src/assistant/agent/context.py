"""Context builders (ARCHITECTURE §7.3).

Explicit, testable functions per run type. Order follows the prompt-caching
rule: the stable prompt first, then the rarely-changing owner profile, then the
volatile run context, then the conversation, then the new turn.

`owner_request` sees the owner's profile and recent control-chat turns. Turns
flagged `contains_managed` are excluded unless `ai_managed` is active
(ARCHITECTURE §14.3) — defence in depth behind the consent wrapper.
Forwarded and quoted third-party text is wrapped as untrusted, and a context
that carries any of it, or managed content, starts the run tainted (§7.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from assistant.accounts.models import Account, ControlTurn
from assistant.agent.prompt_registry import Prompt
from assistant.agent.untrusted import Source, wrap
from assistant.llm.port import Provenance, TextItem

#: How many earlier control turns an owner request sees.
OWNER_HISTORY_TURNS = 20


@dataclass(frozen=True, slots=True)
class OwnerRequestContext:
    instructions: str
    items: tuple[TextItem, ...]
    #: The context already carries third-party text or managed content.
    tainted: bool = False


def build_owner_request(
    *,
    prompt: Prompt,
    account: Account,
    history: list[ControlTurn],
    turn: ControlTurn,
    now: datetime,
) -> OwnerRequestContext:
    profile = [
        "Owner profile:",
        f"- preferred language: {account.locale_pref or 'not set'}",
        f"- timezone: {account.timezone}",
    ]
    run_context = [f"Current time (UTC): {now:%Y-%m-%d %H:%M}"]
    instructions = "\n\n".join([prompt.text, "\n".join(profile), "\n".join(run_context)])

    items = [
        TextItem(
            role="user" if past.role == "owner" else "assistant",
            text=render_turn(past),
            provenance=Provenance.MANAGED if past.contains_managed else Provenance.OWNER_CONTROL,
        )
        for past in history
        if past.text
    ]
    items.append(
        TextItem(
            role="user",
            text=render_turn(turn) or "",
            provenance=Provenance.MANAGED if turn.contains_managed else Provenance.OWNER_CONTROL,
        )
    )
    tainted = any(carries_third_party(t) for t in [*history, turn])
    return OwnerRequestContext(instructions=instructions, items=tuple(items), tainted=tainted)


def carries_third_party(turn: ControlTurn) -> bool:
    """Whether a turn brings third-party text or managed content into the
    context: such a run starts tainted (§7.1, M03 D45)."""
    return bool(turn.third_party or turn.quoted_text is not None or turn.contains_managed)


def render_turn(turn: ControlTurn) -> str:
    """A control turn as the model sees it. Third-party text is wrapped as
    untrusted: a forwarded message entirely, a quote next to the owner's own
    words (§7.3)."""
    text = turn.text or ""
    if turn.role != "owner":
        return text
    if turn.quoted_text is not None:
        quote = wrap(turn.quoted_text, Source.QUOTED)
        return f"{text}\n\n(The owner is quoting this from another chat:)\n{quote}"
    if turn.third_party:
        forwarded = wrap(text, Source.FORWARDED)
        return f"(The owner forwarded this message from someone else:)\n{forwarded}"
    return text
