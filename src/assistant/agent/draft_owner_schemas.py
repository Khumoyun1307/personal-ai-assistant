"""The owner-requested draft's strict output, and its deterministic checks (PF-10).

`draft_owner` (a prompt key under run type `draft_reply`, the ADR-21
pattern) writes every draft the owner asks for. Owner decision D1
(2026-10-30): an explicit owner request is never vetoed, so the schema has
no `needs_reply`, no skip reason and no way to decline — the model writes
the message. Only a provider refusal or an invalid output ends without a
draft, and that is reported as a failure. Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assistant.core.delivery import MAX_TEXT_LENGTH

#: How the owner's draft relates to the chat.
OwnerDraftMode = Literal["reply", "new_message", "revise"]
OWNER_DRAFT_MODES: tuple[OwnerDraftMode, ...] = ("reply", "new_message", "revise")


class OwnerDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    language: str
    text: str
    confidence: Literal["low", "medium", "high"]
    sensitive_topic: bool
    makes_commitment: bool


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
