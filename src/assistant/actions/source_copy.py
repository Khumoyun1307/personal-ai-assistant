"""C1/C2 exact source copying. Callers hold account/conversation/request locks.

No model-written text is accepted as proof. This service copies only explicit
literal-body selections or selected assistant artifacts, never instructions.
H9 consumes the returned source ids and generated-text classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.lifecycle import sendable_length
from assistant.actions.request_models import OwnerRequestSource
from assistant.actions.requests import (
    SUGGESTION_SELECTED,
    RequestSource,
    SelectSuggestion,
    SourceUnavailable,
    load_source,
)
from assistant.core.delivery import MAX_TEXT_LENGTH


@dataclass(frozen=True)
class CopiedBody:
    text: str
    source_ids: tuple[int, ...]
    written_by: Literal["owner", "assistant_suggestion"]
    managed: bool
    untrusted: bool
    status: Literal["copied"] = "copied"


@dataclass(frozen=True)
class CopyUnavailable:
    reason: Literal["source_unavailable", "invalid_source", "length_not_met"]
    status: Literal["unavailable"] = "unavailable"


def _source(row: OwnerRequestSource) -> RequestSource | None:
    try:
        return RequestSource.model_validate(
            {
                "span": {
                    "turn_id": row.turn_id,
                    "field": row.field,
                    "start": row.start_char,
                    "end": row.end_char,
                    "field_sha256": row.field_sha256,
                },
                "purpose": row.purpose,
            }
        )
    except ValidationError:
        return None


async def copy_request_body(
    session: AsyncSession,
    *,
    account_id: int,
    request_id: int,
    source_ids: list[int],
    allow_suggestion: bool = True,
) -> CopiedBody | CopyUnavailable:
    if not source_ids or len(source_ids) > 64 or len(set(source_ids)) != len(source_ids):
        return CopyUnavailable("invalid_source")
    rows = {
        row.id: row
        for row in await session.scalars(
            select(OwnerRequestSource)
            .where(
                OwnerRequestSource.account_id == account_id,
                OwnerRequestSource.request_id == request_id,
            )
            .order_by(OwnerRequestSource.ordinal)
            .with_for_update(read=True)
        )
    }
    parts: list[str] = []
    kinds: set[str] = set()
    managed = False
    untrusted = False
    for source_id in source_ids:
        row = rows.get(source_id)
        if row is None:
            return CopyUnavailable("source_unavailable")
        source = _source(row)
        if source is None or source.span.field != "text":
            return CopyUnavailable("invalid_source")
        if source.purpose not in ("literal_body", "suggestion"):
            return CopyUnavailable("invalid_source")
        loaded = await load_source(session, account_id=account_id, source=source)
        if isinstance(loaded, SourceUnavailable):
            return CopyUnavailable("source_unavailable")
        kinds.add(source.purpose)
        parts.append(loaded.text)
        managed |= loaded.managed
        untrusted |= loaded.untrusted
    if len(kinds) != 1:
        return CopyUnavailable("invalid_source")
    suggestion = kinds == {"suggestion"}
    if suggestion:
        if not allow_suggestion:
            return CopyUnavailable("invalid_source")
        # Only the latest explicit selection of these exact artifact spans
        # can authorize copying. Chronology is never a selection binding.
        details = await session.scalar(
            select(AuditLogEntry.details)
            .where(
                AuditLogEntry.account_id == account_id,
                AuditLogEntry.event == SUGGESTION_SELECTED,
                AuditLogEntry.subject_type == "owner_request",
                AuditLogEntry.subject_id == request_id,
            )
            .order_by(AuditLogEntry.id.desc())
            .limit(1)
        )
        try:
            selection = SelectSuggestion.model_validate(details)
        except ValidationError:
            return CopyUnavailable("invalid_source")
        if selection.source_ids != tuple(source_ids):
            return CopyUnavailable("invalid_source")
        instruction_row = rows.get(selection.instruction_source_id)
        instruction = None if instruction_row is None else _source(instruction_row)
        if (
            instruction is None
            or instruction.purpose != "instruction"
            or instruction.span.field != "text"
        ):
            return CopyUnavailable("invalid_source")
        loaded = await load_source(session, account_id=account_id, source=instruction)
        if isinstance(loaded, SourceUnavailable):
            return CopyUnavailable("source_unavailable")
        if any(rows[source_id].turn_id >= loaded.turn_id for source_id in source_ids):
            return CopyUnavailable("invalid_source")
    text = "".join(parts)
    if not 1 <= sendable_length(text) <= MAX_TEXT_LENGTH:
        return CopyUnavailable("length_not_met")
    return CopiedBody(
        text,
        tuple(source_ids),
        "assistant_suggestion" if suggestion else "owner",
        managed,
        untrusted,
    )
