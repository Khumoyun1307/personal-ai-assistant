"""Context builders (ARCHITECTURE §7.3).

Explicit, testable functions per run type. Order follows the prompt-caching
rule: the stable prompt first, then the rarely-changing owner profile, then the
volatile run context, then the conversation, then the new turn.

`owner_request` sees the owner's profile and recent control-chat turns. Turns
flagged `contains_managed` are excluded unless `ai_managed` is active
(ARCHITECTURE §14.3) — defence in depth behind the consent wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from assistant.accounts.models import Account, ControlTurn
from assistant.agent.prompt_registry import Prompt
from assistant.llm.port import Provenance, TextItem

#: How many earlier control turns an owner request sees.
OWNER_HISTORY_TURNS = 20


@dataclass(frozen=True, slots=True)
class OwnerRequestContext:
    instructions: str
    items: tuple[TextItem, ...]


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
            text=past.text,
            provenance=Provenance.MANAGED if past.contains_managed else Provenance.OWNER_CONTROL,
        )
        for past in history
        if past.text
    ]
    items.append(
        TextItem(
            role="user",
            text=turn.text or "",
            provenance=Provenance.MANAGED if turn.contains_managed else Provenance.OWNER_CONTROL,
        )
    )
    return OwnerRequestContext(instructions=instructions, items=tuple(items))
