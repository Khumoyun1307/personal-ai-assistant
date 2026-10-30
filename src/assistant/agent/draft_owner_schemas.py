"""The owner-requested draft's strict output, and its deterministic checks (PF-10).

`draft_owner` (a prompt key under run type `draft_reply`, the ADR-21
pattern) writes every draft the owner asks for. Owner decision D1
(2026-10-30): an explicit owner request is never vetoed, so the schema has
no `needs_reply`, no skip reason and no way to decline — the model writes
the message. Only a provider refusal or an invalid output ends without a
draft, and that is reported as a failure. Pure: no I/O.

`draft_owner.v3` (PF-F, after the 2026-10-30 owner_drafting eval) adds
`answers_message_id`: in `reply` mode with no message named by the
owner_request model, the draft writer — which sees the chat's message ids
and the owner's own words — says which shown contact message the owner's
request answers, or null. The application accepts only a shown contact
message of that chat (`chosen_answer`); anything else is ignored and the
newest contact message is answered, as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from assistant.core.delivery import MAX_TEXT_LENGTH

#: How the owner's draft relates to the chat.
OwnerDraftMode = Literal["reply", "new_message", "revise"]
OWNER_DRAFT_MODES: tuple[OwnerDraftMode, ...] = ("reply", "new_message", "revise")


class OwnerDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    language: str
    #: The shown contact message the owner's request answers (`[msg:<id>]`),
    #: or null (v3). Used only in `reply` mode without a named message, and
    #: only after `chosen_answer` checked it.
    answers_message_id: int | None
    text: str
    confidence: Literal["low", "medium", "high"]
    sensitive_topic: bool
    makes_commitment: bool


class OwnerRevisionResult(OwnerDraftResult):
    """A `revise` run's output (PF-13, `draft_owner.v2`): the draft plus what
    the owner's message was. `replacement`: the message is itself the whole
    new text (after the card's Edit button the application then keeps the
    owner's words verbatim, P2-T08); `instruction`: a change to apply."""

    revision_kind: Literal["replacement", "instruction"]


def output_model_for(mode: OwnerDraftMode) -> type[OwnerDraftResult]:
    """The structured output a `draft_owner` run of this mode returns."""
    return OwnerRevisionResult if mode == "revise" else OwnerDraftResult


class ShownMessages(Protocol):
    """What a draft was shown (`draft_context.DraftContext`)."""

    @property
    def shown_ids(self) -> frozenset[int]: ...

    @property
    def contact_ids(self) -> frozenset[int]: ...


#: Why a draft writer's `answers_message_id` was ignored (content-free codes).
AnswerIgnored = Literal["not_shown", "not_contact"]


def chosen_answer(
    result: OwnerDraftResult, shown: ShownMessages
) -> tuple[int | None, AnswerIgnored | None]:
    """The draft writer's answered message, checked deterministically: only
    a contact message the draft was shown, in that chat, is accepted.
    Returns `(message_id, None)`, `(None, None)` when it named none, or
    `(None, reason)` when its choice is ignored."""
    chosen = result.answers_message_id
    if chosen is None:
        return None, None
    if chosen in shown.contact_ids:
        return chosen, None
    if chosen in shown.shown_ids:
        return None, "not_contact"
    return None, "not_shown"


@dataclass(frozen=True, slots=True)
class OwnerDraftCheck:
    valid: bool
    result: OwnerDraftResult
    error_class: str | None = None


def validate_owner_draft(result: OwnerDraftResult) -> OwnerDraftCheck:
    """Our own check of a parsed output: the text, newline-normalised and
    stripped, must be 1 to 4096 characters (never "fixed" by us)."""
    text = result.text.replace("\r\n", "\n").strip()
    normalized = result.model_copy(update={"text": text})
    if not 1 <= len(text) <= MAX_TEXT_LENGTH:
        return OwnerDraftCheck(False, normalized, "text_length")
    return OwnerDraftCheck(True, normalized)


def word_count(text: str) -> int:
    """Whitespace-separated words: the count the owner can check by eye."""
    return len(text.split())


def char_count(text: str) -> int:
    """Characters as a person counts them (Unicode code points)."""
    return len(text)


@dataclass(frozen=True, slots=True)
class WordBounds:
    """The owner's length in words ("5-7 words", "at most 20"); either end
    may be open."""

    min_words: int | None = None
    max_words: int | None = None

    def __post_init__(self) -> None:
        if self.min_words is None and self.max_words is None:
            raise ValueError("word bounds need at least one end")
        if (
            self.min_words is not None
            and self.max_words is not None
            and self.min_words > self.max_words
        ):
            raise ValueError("min_words is above max_words")

    def contains(self, count: int) -> bool:
        return (self.min_words is None or count >= self.min_words) and (
            self.max_words is None or count <= self.max_words
        )

    def distance(self, count: int) -> int:
        """How many words outside the range (0 inside it)."""
        if self.min_words is not None and count < self.min_words:
            return self.min_words - count
        if self.max_words is not None and count > self.max_words:
            return count - self.max_words
        return 0

    def describe(self) -> str:
        """The range in plain words, for the model and the tool result."""
        if self.min_words is not None and self.max_words is not None:
            if self.min_words == self.max_words:
                return f"exactly {self.min_words} words"
            return f"between {self.min_words} and {self.max_words} words"
        if self.min_words is not None:
            return f"at least {self.min_words} words"
        return f"at most {self.max_words} words"
