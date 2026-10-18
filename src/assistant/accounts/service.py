"""Account, consent and pending-input services (ARCHITECTURE §6.7, §14.3).

Three things live here because they share one transactional story: who the
owner is, what they have agreed to, and what the control chat is currently
waiting for them to type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, Consent, ControlTurn, PendingInput
from assistant.core.clock import Clock

#: How long the control chat waits for an answer before giving up on it.
PENDING_INPUT_TTL = timedelta(minutes=30)

#: Consent kinds (ARCHITECTURE §10.3).
CONSENT_AI_CONTROL = "ai_control"
CONSENT_AI_MANAGED = "ai_managed"


class ConsentMissingError(RuntimeError):
    """An LLM call was attempted without the consent that gates it.

    Raised rather than returned: §6 invariant 9 is not a condition to branch
    on, it is a wall. The caller should never have got this far.
    """


@dataclass(frozen=True, slots=True)
class ConsentState:
    """What an account has currently agreed to."""

    kind: str
    granted: bool
    policy_version: str | None
    processors: tuple[str, ...]


async def get_or_create_account(
    session: AsyncSession, *, telegram_user_id: int, clock: Clock
) -> tuple[Account, bool]:
    """Find the owner's account, creating it on first contact.

    Returns `(account, created)`. Only ever called for an allowlisted user —
    routing rejects everyone else before this point (§6 invariant 12).
    """
    result = await session.execute(
        select(Account).where(Account.telegram_user_id == telegram_user_id)
    )
    account = result.scalar_one_or_none()
    if account is not None:
        return account, False

    account = Account(
        telegram_user_id=telegram_user_id,
        status="onboarding",
        timezone="UTC",
    )
    session.add(account)
    await session.flush()
    return account, True


async def set_control_chat(session: AsyncSession, account: Account, chat_id: int) -> None:
    """Record where the owner can be reached.

    Until this is set the bot cannot message the owner at all — a send returns
    `400 chat not found`, not a permission error (TF §12 LV-09).
    """
    account.control_chat_id = chat_id
    await session.flush()


async def grant_consent(
    session: AsyncSession,
    account: Account,
    *,
    kind: str,
    policy_version: str,
    processors: list[str],
    clock: Clock,
) -> Consent:
    """Record a grant. Append-only: a re-grant inserts, never updates.

    The history of what was agreed to, and when, has to survive — that is the
    whole point of keeping consent rather than a boolean.
    """
    consent = Consent(
        account_id=account.id,
        kind=kind,
        policy_version=policy_version,
        processors=processors,
    )
    session.add(consent)
    await session.flush()
    return consent


async def revoke_consent(
    session: AsyncSession, account: Account, *, kind: str, clock: Clock
) -> int:
    """Revoke every active consent of this kind. Returns how many."""
    result = await session.execute(
        select(Consent)
        .where(Consent.account_id == account.id)
        .where(Consent.kind == kind)
        .where(Consent.revoked_at.is_(None))
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.revoked_at = clock.now()
    await session.flush()
    return len(rows)


async def active_consent(session: AsyncSession, account_id: int, *, kind: str) -> ConsentState:
    """The current consent of a kind: the latest row that is not revoked."""
    result = await session.execute(
        select(Consent)
        .where(Consent.account_id == account_id)
        .where(Consent.kind == kind)
        .where(Consent.revoked_at.is_(None))
        .order_by(Consent.granted_at.desc())
        .limit(1)
    )
    consent = result.scalar_one_or_none()
    if consent is None:
        return ConsentState(kind=kind, granted=False, policy_version=None, processors=())

    return ConsentState(
        kind=kind,
        granted=True,
        policy_version=consent.policy_version,
        processors=tuple(consent.processors or ()),
    )


async def require_consent(
    session: AsyncSession, account_id: int, *, kind: str, processor: str | None = None
) -> ConsentState:
    """Assert consent before an LLM call, or raise (§6 invariant 9).

    `processor` is checked too: consent names who the data goes to, so
    switching provider without re-consent is exactly what this prevents.
    """
    state = await active_consent(session, account_id, kind=kind)
    if not state.granted:
        raise ConsentMissingError(f"no active '{kind}' consent for this account")
    if processor is not None and processor not in state.processors:
        raise ConsentMissingError(f"'{kind}' consent does not name processor '{processor}'")
    return state


# ------------------------------------------------------------ pending inputs
async def set_pending_input(
    session: AsyncSession,
    *,
    account_id: int,
    kind: str,
    clock: Clock,
    ref_id: int | None = None,
    prompt_message_id: int | None = None,
    ttl: timedelta = PENDING_INPUT_TTL,
) -> PendingInput:
    """Ask the owner for something, consuming their next plain message.

    Only one per account at a time: two overlapping questions would make the
    next message ambiguous, so a new one replaces any existing.
    """
    await clear_pending_input(session, account_id=account_id)
    pending = PendingInput(
        account_id=account_id,
        kind=kind,
        ref_id=ref_id,
        prompt_message_id=prompt_message_id,
        expires_at=clock.now() + ttl,
    )
    session.add(pending)
    await session.flush()
    return pending


async def get_pending_input(
    session: AsyncSession, *, account_id: int, clock: Clock
) -> PendingInput | None:
    """The live pending input, if any. Expired ones are cleared, not returned."""
    result = await session.execute(
        select(PendingInput)
        .where(PendingInput.account_id == account_id)
        .order_by(PendingInput.id.desc())
        .limit(1)
    )
    pending = result.scalar_one_or_none()
    if pending is None:
        return None
    if pending.expires_at <= clock.now():
        await session.delete(pending)
        await session.flush()
        return None
    return pending


async def clear_pending_input(session: AsyncSession, *, account_id: int) -> int:
    """Drop any pending input. Returns how many were cleared."""
    result = await session.execute(
        select(PendingInput).where(PendingInput.account_id == account_id)
    )
    rows = list(result.scalars().all())
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)


# -------------------------------------------------------------- control turns
async def record_control_turn(
    session: AsyncSession,
    *,
    account_id: int,
    role: str,
    text: str | None,
    telegram_message_id: int | None = None,
    contains_managed: bool = False,
) -> ControlTurn:
    """Append a turn to the owner's conversation with the assistant."""
    turn = ControlTurn(
        account_id=account_id,
        role=role,
        text=text,
        telegram_message_id=telegram_message_id,
        contains_managed=contains_managed,
    )
    session.add(turn)
    await session.flush()
    return turn
