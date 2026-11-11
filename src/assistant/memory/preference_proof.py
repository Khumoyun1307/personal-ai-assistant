"""Content-free evidence for independently captured owner preferences (C8)."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from assistant.actions.requests import SourceSpan


class OwnerCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["owner_source_exact"]
    turn_ids: list[StrictInt]
    source_spans: list[SourceSpan]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_at: AwareDatetime | None

    @model_validator(mode="after")
    def _consistent(self) -> OwnerCapture:
        if any(value <= 0 for value in self.turn_ids):
            raise ValueError("invalid source ids")
        if set(self.turn_ids) != {span.turn_id for span in self.source_spans}:
            raise ValueError("inconsistent source ids")
        if self.confirmed_at is not None and self.confirmed_at.utcoffset() != timedelta(0):
            raise ValueError("confirmation must be UTC")
        return self


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_capture(value: object, content: str) -> OwnerCapture | None:
    try:
        proof = OwnerCapture.model_validate(value)
    except ValidationError:
        return None
    if proof.content_sha256 != content_hash(content):
        return None
    return proof
