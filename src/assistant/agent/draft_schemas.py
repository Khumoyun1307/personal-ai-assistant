"""Strict structured draft output and deterministic, side-effect-free validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict


class DraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    language: str
    needs_reply: bool
    skip_reason: Literal["no_reply_needed", "unclear", "not_for_owner"] | None
    revision_kind: Literal["new", "replacement", "instruction"]
    text: str
    confidence: Literal["low", "medium", "high"]
    uses_only_permitted_information: bool
    sensitive_topic: bool
    makes_commitment: bool
    should_escalate: bool
    escalation_reason: (
        Literal["decision_needed", "outside_knowledge", "sensitive", "conflict", "other"] | None
    )


@dataclass(frozen=True, slots=True)
class DraftCheck:
    valid: bool
    result: DraftResult
    error_class: str | None = None


def validate_draft(result: DraftResult, *, run_kind: Literal["draft", "revise"]) -> DraftCheck:
    allowed = {"new"} if run_kind == "draft" else {"replacement", "instruction"}
    if result.revision_kind not in allowed:
        return DraftCheck(False, result, "revision_kind")
    text = result.text.replace("\r\n", "\n").strip() if result.needs_reply else ""
    normalized = result.model_copy(update={"text": text})
    if result.needs_reply and not 1 <= len(text) <= 4096:
        return DraftCheck(False, normalized, "text_length")
    if not result.needs_reply and result.skip_reason is None:
        return DraftCheck(False, normalized, "skip_reason")
    return DraftCheck(True, normalized)
