"""Request-local recipient choices. Names are never stored as aliases or memory."""

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import ControlTurn, PendingInput
from assistant.actions.request_models import OwnerRequest
from assistant.actions.requests import (
    CandidatePair,
    RecordCandidates,
    RequestUpdateResult,
    ResolutionCandidates,
    SelectRecipient,
    update_request,
)
from assistant.conversations import read


async def remember_candidates(
    session: AsyncSession,
    *,
    account_id: int,
    request_id: int,
    expected_version: int,
    candidates: tuple[CandidatePair, ...],
    now: datetime,
) -> RequestUpdateResult:
    return await update_request(
        session,
        account_id=account_id,
        request_id=request_id,
        expected_version=expected_version,
        update=RecordCandidates(candidates=candidates),
        now=now,
    )


def candidate_number(text: str) -> int | None:
    """Recognize only a whole owner answer, never a number inside an instruction."""
    value = text.strip().casefold().rstrip(".!?")
    if value.isascii() and value.isdecimal() and 1 <= int(value) <= 10:
        return int(value)
    ordinals = (
        ("the first one", "first", "birinchisi", "биринчиси", "первый", "первая"),
        ("the second one", "second", "ikkinchisi", "иккинчиси", "второй", "вторая"),
        ("the third one", "third", "uchinchisi", "учинчиси", "третий", "третья"),
    )
    return next((i for i, words in enumerate(ordinals, 1) if value in words), None)


async def select_candidate_answer(
    session: AsyncSession,
    *,
    account_id: int,
    request_id: int,
    turn: ControlTurn,
    now: datetime,
) -> RequestUpdateResult | None:
    if (
        turn.account_id != account_id
        or turn.role != "owner"
        or not turn.text
        or (turn.third_party and not (turn.owner_text_verified and turn.quoted_text is not None))
    ):
        return None
    row = await session.scalar(
        select(OwnerRequest).where(
            OwnerRequest.account_id == account_id, OwnerRequest.id == request_id
        )
    )
    if (
        row is None
        or row.resolution_candidates is None
        or row.contact_id is not None
        or row.missing_slot != "recipient"
        or row.expires_at <= now
        or turn.id <= row.last_turn_id
    ):
        return None
    try:
        saved = ResolutionCandidates.model_validate(row.resolution_candidates)
    except ValidationError:
        return None
    if saved.request_version != row.version:
        return None
    if row.question_input_id is not None:
        question = await session.scalar(
            select(PendingInput).where(
                PendingInput.account_id == account_id,
                PendingInput.id == row.question_input_id,
                PendingInput.ref_id == row.id,
                PendingInput.kind == "owner_request",
            )
        )
        if question is not None and question.expires_at <= now:
            return await remember_candidates(
                session,
                account_id=account_id,
                request_id=request_id,
                expected_version=row.version,
                candidates=(),
                now=now,
            )
    number = candidate_number(turn.text)
    selected = None
    if number is not None:
        if number > len(saved.candidates):
            return None
        selected = saved.candidates[number - 1]
    else:
        # An explicit name answer must identify one actual observed contact;
        # a fresh weak guess never constitutes owner confirmation.
        directory = await read.contact_directory(session, account_id)
        resolved = read.resolve_contacts(directory, turn.text, limit=10)
        if resolved.verdict == "unique_strong":
            match = resolved.strong[0].entry
            selected = next(
                (pair for pair in saved.candidates if pair.contact_id == match.contact_id), None
            )
    if selected is None or selected.conversation_id is None:
        return None
    result = await update_request(
        session,
        account_id=account_id,
        request_id=request_id,
        expected_version=saved.request_version,
        update=SelectRecipient(
            contact_id=selected.contact_id,
            conversation_id=selected.conversation_id,
            basis="owner_choice",
        ),
        now=now,
    )
    if result.status == "applied":
        row.last_turn_id = turn.id
        await session.flush()
    return result
