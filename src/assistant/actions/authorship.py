"""Application-assigned proof of the current body's authorship (C2 / H9).

Origin describes the trigger, not who wrote a later version. Missing, malformed
or mismatched proof is conservatively generated; legacy rows gain no owner proof.
Source ids refer to C1 sources, never to model claims or copied text archives.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SourceId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
AuthorshipKind = Literal[
    "owner_dictated", "owner_literal_edit", "assistant_written", "agent_proposal_selected"
]


class VersionAuthorship(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Annotated[int, Field(ge=1)]
    kind: AuthorshipKind
    source_ids: list[SourceId]
    parent_version: Annotated[int, Field(ge=1)] | None
    retains_generated_text: bool

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.parent_version != (None if self.version == 1 else self.version - 1):
            raise ValueError("authorship must name the preceding version")
        if len(self.source_ids) > 64 or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source ids must be unique and bounded")
        if self.kind in {"assistant_written", "agent_proposal_selected"} and (
            not self.retains_generated_text
        ):
            raise ValueError("assistant text must retain its generated classification")
        if self.kind == "owner_dictated" and self.retains_generated_text:
            raise ValueError("dictation cannot retain generated text")
        return self


def current_authorship(value: object, *, version: int) -> VersionAuthorship | None:
    try:
        proof = VersionAuthorship.model_validate(value)
    except ValidationError:
        return None
    return proof if proof.version == version else None


def is_ai_generated(value: object, *, version: int) -> bool:
    proof = current_authorship(value, version=version)
    return (
        proof is None
        or proof.retains_generated_text
        or proof.kind in {"assistant_written", "agent_proposal_selected"}
    )


def generated_authorship(version: int) -> VersionAuthorship:
    return VersionAuthorship(
        version=version,
        kind="assistant_written",
        source_ids=[],
        parent_version=None if version == 1 else version - 1,
        retains_generated_text=True,
    )
