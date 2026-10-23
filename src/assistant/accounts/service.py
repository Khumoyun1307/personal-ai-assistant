"""Account, consent and pending-input services (ARCHITECTURE §6.7, §14.3).

Three things live here because they share one transactional story: who the
owner is, what they have agreed to, and what the control chat is currently
waiting for them to type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import callbacks
from assistant.accounts.models import Account, AuditLogEntry, Consent, ControlTurn, PendingInput
from assistant.core.clock import Clock

#: How long the control chat waits for an answer before giving up on it.
PENDING_INPUT_TTL = timedelta(minutes=30)

#: Consent kinds (ARCHITECTURE §10.3).
CONSENT_AI_CONTROL = "ai_control"
CONSENT_AI_MANAGED = "ai_managed"

#: The privacy-policy version consents are recorded against. v2 adds managed
#: conversations, v3 deletion and "stop observing", v4 hosting and encrypted
#: backups (docs/legal/privacy-policy.md). A bump keeps earlier consents valid;
#: one that widens processing needs an entry in `inbox.policy_notice`, which
#: tells owners who consented under an older version (AGENTS §16).
POLICY_VERSION = "v4"

#: Named in every consent, because consent has to say who the data goes to
#: (SEC-02). A new processor needs a new consent, not just a new entry here.
AI_PROCESSORS: tuple[str, ...] = ("OpenAI",)

#: Card kind for the managed-conversation consent question (Turn on / Not now
#: buttons, callback ops `agree` / `decline` — never a free-text answer, so an
#: unrelated message can never be read as a decision), and how long it stays
#: open: it is asked when the connection arrives, which the owner may not see
#: for a while. A decline is also respected for this long: a reconnect does
#: not ask again.
MANAGED_CONSENT_CARD = "managed_consent"
MANAGED_CONSENT_TTL = timedelta(hours=24)


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
    """The live pending *text* question, if any (cards are not questions:
    they are resolved by a tap, see `claim_card`). Expired ones are cleared."""
    result = await session.execute(
        select(PendingInput)
        .where(PendingInput.account_id == account_id)
        .where(PendingInput.token.is_(None))
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
    """Drop any pending text question (a command cancels it — S01.3 precedence
    rule 1). Cards survive: their buttons stay valid until tapped or expired."""
    result = await session.execute(
        select(PendingInput)
        .where(PendingInput.account_id == account_id)
        .where(PendingInput.token.is_(None))
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
    created_at: datetime | None = None,
    third_party: bool = False,
    quoted_text: str | None = None,
    refs: dict[str, list[int]] | None = None,
) -> ControlTurn:
    """Append a turn to the owner's conversation with the assistant.

    `third_party` marks forwarded or quoted third-party text (taint, §7.1);
    `quoted_text` keeps the quote itself (content-bearing, §10.1).
    """
    turn = ControlTurn(
        account_id=account_id,
        role=role,
        text=text,
        telegram_message_id=telegram_message_id,
        contains_managed=contains_managed,
        third_party=third_party or quoted_text is not None,
        quoted_text=quoted_text,
        refs=refs or {},
    )
    if created_at is not None:
        # When the owner's message arrived, not when we got round to it: the
        # acknowledgement-latency metric is measured from here.
        turn.created_at = created_at
    session.add(turn)
    await session.flush()
    return turn


# ------------------------------------------------------------ confirmation cards
CARD_TTL = timedelta(hours=24)


async def create_card(
    session: AsyncSession,
    *,
    account_id: int,
    kind: str,
    clock: Clock,
    ref_id: int | None = None,
    prompt_message_id: int | None = None,
    ttl: timedelta = CARD_TTL,
    token: str | None = None,
) -> PendingInput:
    """A confirmation card's state: one row, one unguessable token (§6.7).

    Unlike a text question, several cards may be live at once, and a command
    does not cancel them. `claim_card` resolves one exactly once. `token` is
    given when the card's message was sent first and the card is recorded
    only once it is known to have arrived.
    """
    card = PendingInput(
        account_id=account_id,
        kind=kind,
        ref_id=ref_id,
        prompt_message_id=prompt_message_id,
        token=token or callbacks.new_token(),
        expires_at=clock.now() + ttl,
    )
    session.add(card)
    await session.flush()
    return card


async def claim_card(
    session: AsyncSession, *, account_id: int, token: str, clock: Clock
) -> PendingInput | None:
    """Resolve a card by its token — single use, under a row lock, scoped to
    the account that owns it. None means "no longer active": unknown,
    another owner's, expired, or already tapped (a double tap)."""
    result = await session.execute(
        select(PendingInput)
        .where(PendingInput.account_id == account_id)
        .where(PendingInput.token == token)
        .with_for_update()
    )
    card = result.scalar_one_or_none()
    if card is None:
        return None
    claimed = PendingInput(
        account_id=card.account_id,
        kind=card.kind,
        ref_id=card.ref_id,
        prompt_message_id=card.prompt_message_id,
        token=card.token,
        expires_at=card.expires_at,
    )
    await session.delete(card)
    await session.flush()
    if claimed.expires_at <= clock.now():
        return None
    return claimed


async def close_cards(session: AsyncSession, *, account_id: int, kind: str) -> int:
    """Delete every open card of `kind`: a later tap answers "no longer
    active". One DELETE statement, so a press holding the card's row lock
    (`claim_card`) finishes first and this transaction's later statements see
    what it committed."""
    result = await session.execute(
        delete(PendingInput)
        .where(PendingInput.account_id == account_id)
        .where(PendingInput.kind == kind)
        .where(PendingInput.token.is_not(None))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def live_card(
    session: AsyncSession, *, account_id: int, kind: str, clock: Clock
) -> PendingInput | None:
    """An unexpired card of this kind, if one is open."""
    result = await session.execute(
        select(PendingInput)
        .where(PendingInput.account_id == account_id)
        .where(PendingInput.kind == kind)
        .where(PendingInput.token.is_not(None))
        .where(PendingInput.expires_at > clock.now())
        .limit(1)
    )
    return result.scalar_one_or_none()


#: Audit event for a declined managed-conversation consent (content-free).
EVENT_MANAGED_CONSENT_DECLINED = "managed_consent_declined"


async def record_managed_consent_declined(
    session: AsyncSession, *, account_id: int, clock: Clock
) -> None:
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=clock.now(),
            actor="owner",
            event=EVENT_MANAGED_CONSENT_DECLINED,
        )
    )
    await session.flush()


async def managed_consent_declined_since(
    session: AsyncSession, *, account_id: int, since: datetime
) -> bool:
    result = await session.execute(
        select(AuditLogEntry.id)
        .where(AuditLogEntry.account_id == account_id)
        .where(AuditLogEntry.event == EVENT_MANAGED_CONSENT_DECLINED)
        .where(AuditLogEntry.at >= since)
        .limit(1)
    )
    return result.first() is not None


async def sweep_expired_cards(session: AsyncSession, clock: Clock) -> int:
    """Expired pending inputs of both kinds (§10.4: "until expiry")."""
    result = await session.execute(
        select(PendingInput).where(PendingInput.expires_at <= clock.now())
    )
    rows = list(result.scalars().all())
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)


# ------------------------------------------------------------ managed mode
MANAGED_MODE_KEY = "managed_mode"
MANAGED_MODE_OBSERVE = "observe"
MANAGED_MODE_STOPPED = "stopped"


def managed_mode(account: Account) -> str:
    """How the owner's included chats are handled without AI consent:
    `observe` (store for coverage and visibility — the default) or `stopped`
    (store nothing further; FR-ONB-06)."""
    value = (account.settings or {}).get(MANAGED_MODE_KEY)
    return MANAGED_MODE_STOPPED if value == MANAGED_MODE_STOPPED else MANAGED_MODE_OBSERVE


async def set_managed_mode(session: AsyncSession, account: Account, mode: str) -> None:
    if mode not in (MANAGED_MODE_OBSERVE, MANAGED_MODE_STOPPED):
        raise ValueError(mode)
    account.settings = {**(account.settings or {}), MANAGED_MODE_KEY: mode}
    await session.flush()
