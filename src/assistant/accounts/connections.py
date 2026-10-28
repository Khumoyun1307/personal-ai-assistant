"""Business connection registry (ARCHITECTURE §6.4; FR-ONB-03, -04, -07).

What the registry has to get right, each learned from a live test (TF §12):

- **The connection id is mutable.** A reconnect issues a new id; rights and
  chat-list edits keep it (LV-05). So a connection is keyed by *account*, and a
  late event about an older id must not overwrite the newer one.
- **Rights decide nothing on their own.** A connection with `rights=[]` still
  delivers messages (LV-12), so "connected" and "may reply" are different
  questions. Missing rights, or `is_enabled=false`, mean no permissions.
- **Only an onboarded owner's connection is live.** An allowlisted user who has
  not finished /start gets an inert `pending_owner` row; anyone else gets no
  tenant row at all, only a metadata cache entry (§6 invariant 12).

Pure domain code: the adapter turns Telegram's payload into a
`ConnectionSnapshot`; nothing here knows what aiogram is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import (
    Account,
    AuditLogEntry,
    BusinessConnection,
    BusinessConnectionEvent,
    ForeignConnection,
)
from assistant.accounts.service import get_or_create_account
from assistant.actions import lifecycle
from assistant.actions.states import StateReason
from assistant.core.clock import Clock

RIGHT_REPLY = "can_reply"
RIGHT_READ = "can_read_messages"
RIGHT_DELETE_SENT = "can_delete_sent_messages"
RIGHT_DELETE_ALL = "can_delete_all_messages"
#: Rights the product can use. Mark-as-read is opt-in and not built yet (R3);
#: deleting covers only the assistant's own messages (AGENTS §6.14), which
#: either delete right allows. Only rights in the official Bot API docs count:
#: aiogram 3.31 also models `can_delete_outgoing_messages`, which Bot API 10.3
#: does not document (TF §13), so it grants nothing here.
EXTENDED_RIGHTS: frozenset[str] = frozenset({RIGHT_READ, RIGHT_DELETE_SENT, RIGHT_DELETE_ALL})

#: Rights the product never uses: profile, gifts, Stars, stories, and deleting
#: other people's messages (AGENTS §6.14). The owner is told they can turn
#: these off (FR-ONB-04: recommend minimal rights).
UNUSED_RIGHT_PREFIXES: tuple[str, ...] = (
    "can_edit_",
    "can_change_gift",
    "can_view_gifts",
    "can_convert_gifts",
    "can_transfer_",
    "can_manage_stories",
)


class Tier(StrEnum):
    """Capability tier, derived from the rights actually granted (FR-ONB-04)."""

    NONE = "none"
    OBSERVE_ONLY = "observe_only"
    CAN_REPLY = "can_reply"
    EXTENDED = "extended"


class Status(StrEnum):
    PENDING_OWNER = "pending_owner"
    ACTIVE = "active"
    DISABLED = "disabled"
    STALE = "stale"


class Change(StrEnum):
    """What one observed connection state did to the registry."""

    CONNECTED = "connected"
    RECONNECTED = "reconnected"
    RIGHTS_CHANGED = "rights_changed"
    DISABLED = "disabled"
    PENDING_OWNER = "pending_owner"
    ACTIVATED = "activated"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    STALE_EVENT = "stale_event"
    FOREIGN = "foreign"


#: Changes the owner is told about (FR-ONB-07), once they can be reached.
NOTIFY_CHANGES: frozenset[Change] = frozenset(
    {Change.CONNECTED, Change.RECONNECTED, Change.RIGHTS_CHANGED, Change.DISABLED, Change.ACTIVATED}
)


class Source(StrEnum):
    UPDATE = "update"
    BOOTSTRAP = "bootstrap"
    ONBOARDING = "onboarding"


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """One observed connection state, from an update or `getBusinessConnection`."""

    connection_id: str
    user_id: int
    user_chat_id: int
    date: datetime
    is_enabled: bool
    rights: frozenset[str]
    #: Digest of the raw payload, for the audit trail. Never the payload.
    payload_digest: str

    @property
    def effective_rights(self) -> frozenset[str]:
        """Disabled means no permissions, whatever `rights` still says (§6.7)."""
        return self.rights if self.is_enabled else frozenset()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    change: Change
    account_id: int | None = None
    event_id: int | None = None

    @property
    def notify(self) -> bool:
        return self.event_id is not None and self.change in NOTIFY_CHANGES


class ResolutionKind(StrEnum):
    OWNER = "owner"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Resolution:
    """Whose traffic a business update carrying this connection id is."""

    kind: ResolutionKind
    account_id: int | None = None
    status: Status | None = None

    @property
    def is_live(self) -> bool:
        """Business processing runs only on an active, onboarded connection."""
        return self.kind is ResolutionKind.OWNER and self.status is Status.ACTIVE


def capability_tier(is_enabled: bool, rights: frozenset[str]) -> Tier:
    if not is_enabled:
        return Tier.NONE
    if RIGHT_REPLY not in rights:
        return Tier.OBSERVE_ONLY
    if rights & EXTENDED_RIGHTS:
        return Tier.EXTENDED
    return Tier.CAN_REPLY


def unused_rights(rights: frozenset[str]) -> list[str]:
    """Granted rights the product will never use, sorted for stable output."""
    surplus = {r for r in rights if r.startswith(UNUSED_RIGHT_PREFIXES)}
    if RIGHT_DELETE_ALL in rights:
        # Deleting the assistant's own messages needs only the narrower right.
        surplus.add(RIGHT_DELETE_ALL)
    return sorted(surplus)


def _status_for(snapshot: ConnectionSnapshot, account: Account) -> Status:
    if not snapshot.is_enabled:
        return Status.DISABLED
    return Status.ACTIVE if account.status == "active" else Status.PENDING_OWNER


def _classify(existing: BusinessConnection, snapshot: ConnectionSnapshot, new: Status) -> Change:
    old = Status(existing.status)
    if new is Status.DISABLED:
        return Change.UNCHANGED if old is Status.DISABLED else Change.DISABLED
    if new is Status.PENDING_OWNER:
        return (
            Change.PENDING_OWNER
            if old is not Status.PENDING_OWNER
            else _same_or_updated(existing, snapshot)
        )
    # new is ACTIVE
    if old is Status.PENDING_OWNER:
        return Change.CONNECTED
    if old in (Status.DISABLED, Status.STALE) or snapshot.connection_id != existing.connection_id:
        return Change.RECONNECTED
    if frozenset(existing.rights) != snapshot.effective_rights:
        return Change.RIGHTS_CHANGED
    return _same_or_updated(existing, snapshot)


def _same_or_updated(existing: BusinessConnection, snapshot: ConnectionSnapshot) -> Change:
    same = (
        existing.connection_id == snapshot.connection_id
        and existing.is_enabled == snapshot.is_enabled
        and frozenset(existing.rights) == snapshot.effective_rights
        and existing.user_chat_id == snapshot.user_chat_id
    )
    return Change.UNCHANGED if same else Change.UPDATED


async def _record_event(
    session: AsyncSession,
    *,
    account_id: int,
    snapshot: ConnectionSnapshot,
    change: Change,
    source: Source,
    clock: Clock,
) -> BusinessConnectionEvent:
    event = BusinessConnectionEvent(
        account_id=account_id,
        connection_id=snapshot.connection_id,
        payload_digest=snapshot.payload_digest,
        rights=sorted(snapshot.effective_rights),
        is_enabled=snapshot.is_enabled,
        change=change.value,
        source=source.value,
        observed_at=clock.now(),
    )
    session.add(event)
    await session.flush()
    return event


async def _remember_foreign(
    session: AsyncSession, snapshot: ConnectionSnapshot, clock: Clock
) -> None:
    now = clock.now()
    statement = insert(ForeignConnection).values(
        connection_id=snapshot.connection_id,
        telegram_user_id=snapshot.user_id,
        first_seen_at=now,
        last_seen_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ForeignConnection.connection_id], set_={"last_seen_at": now}
        )
    )


async def connection_for_account(
    session: AsyncSession, account_id: int, *, for_update: bool = False
) -> BusinessConnection | None:
    statement = select(BusinessConnection).where(BusinessConnection.account_id == account_id)
    if for_update:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalar_one_or_none()


#: Audit event when a send found the connection invalid (§6.4; decisions R15).
EVENT_MARKED_STALE = "connection_marked_stale"


async def mark_stale(
    session: AsyncSession, account_id: int, now: datetime, *, connection_id: str
) -> bool:
    """A send on `connection_id` got `BUSINESS_CONNECTION_INVALID` (§6.4):
    that connection becomes `stale`, which blocks every business send for
    the account (rule `connection_active`) until a new id arrives — a
    `business_connection` update or an unknown-id bootstrap (`_classify`
    maps stale → `RECONNECTED`). The audit row's time is "stale since".
    Conditional on that id still being the account's **active** connection:
    one row per account holds the current id, rewritten in place on a
    reconnect, so a failure on an old id must never stale its replacement
    (Antony on #107). A connection already disabled or stale is left alone."""
    row = (
        await session.execute(
            update(BusinessConnection)
            .where(
                BusinessConnection.account_id == account_id,
                BusinessConnection.connection_id == connection_id,
                BusinessConnection.status == Status.ACTIVE.value,
            )
            .values(status=Status.STALE.value, updated_at=now)
            .returning(BusinessConnection.id)
        )
    ).one_or_none()
    if row is None:
        return False
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor="system",
            event=EVENT_MARKED_STALE,
            subject_type="connection",
            subject_id=int(row[0]),
            details={"from": Status.ACTIVE.value, "to": Status.STALE.value},
        )
    )
    await session.flush()
    return True


async def apply_snapshot(
    session: AsyncSession,
    snapshot: ConnectionSnapshot,
    *,
    owner_ids: frozenset[int],
    source: Source,
    clock: Clock,
) -> ApplyResult:
    """Fold one observed connection state into the registry. Idempotent.

    Safe in any order with business messages and with itself: an identical
    state records nothing, and an event about an older connection id than the
    current one is logged but not applied.
    """
    if snapshot.user_id not in owner_ids:
        # No account, no tenant row — only enough to reject the id cheaply next
        # time (§6 invariant 12).
        await _remember_foreign(session, snapshot, clock)
        return ApplyResult(Change.FOREIGN)

    account, _ = await get_or_create_account(
        session, telegram_user_id=snapshot.user_id, clock=clock
    )
    existing = await connection_for_account(session, account.id, for_update=True)
    new_status = _status_for(snapshot, account)
    tier = capability_tier(snapshot.is_enabled, snapshot.rights).value

    if existing is None:
        change = {
            Status.ACTIVE: Change.CONNECTED,
            Status.PENDING_OWNER: Change.PENDING_OWNER,
            Status.DISABLED: Change.UNCHANGED,
        }[new_status]
        session.add(
            BusinessConnection(
                account_id=account.id,
                connection_id=snapshot.connection_id,
                is_enabled=snapshot.is_enabled,
                rights=sorted(snapshot.effective_rights),
                capability_tier=tier,
                user_chat_id=snapshot.user_chat_id,
                status=new_status.value,
                connected_at=snapshot.date,
                updated_at=clock.now(),
            )
        )
        # A first sighting of an already-disabled connection is still history.
        event = await _record_event(
            session,
            account_id=account.id,
            snapshot=snapshot,
            change=Change.DISABLED if change is Change.UNCHANGED else change,
            source=source,
            clock=clock,
        )
        return ApplyResult(change, account.id, event.id if change in NOTIFY_CHANGES else None)

    if snapshot.connection_id != existing.connection_id and snapshot.date < existing.connected_at:
        # A late event about a connection id that a reconnect has replaced.
        # Recording it keeps the old id resolvable; applying it would undo the
        # reconnect.
        await _record_event(
            session,
            account_id=account.id,
            snapshot=snapshot,
            change=Change.STALE_EVENT,
            source=source,
            clock=clock,
        )
        return ApplyResult(Change.STALE_EVENT, account.id)

    change = _classify(existing, snapshot, new_status)
    if change is Change.UNCHANGED:
        return ApplyResult(Change.UNCHANGED, account.id)

    if snapshot.connection_id != existing.connection_id:
        existing.connected_at = snapshot.date
    existing.connection_id = snapshot.connection_id
    existing.is_enabled = snapshot.is_enabled
    existing.rights = sorted(snapshot.effective_rights)
    existing.capability_tier = tier
    existing.user_chat_id = snapshot.user_chat_id
    existing.status = new_status.value
    existing.updated_at = clock.now()
    event = await _record_event(
        session, account_id=account.id, snapshot=snapshot, change=change, source=source, clock=clock
    )
    await _settle_pending_actions(session, account.id, change, clock.now())
    return ApplyResult(change, account.id, event.id)


async def _settle_pending_actions(
    session: AsyncSession, account_id: int, change: Change, now: datetime
) -> None:
    """Send-relevant connection changes reach pending actions in the same
    transaction (§6.4; S04.4 §3.8). A disabled connection cancels every
    unsent action; changed rights outdate drafts and approvals made under
    the old ones. Tx A re-checks the connection either way (the guarantee);
    a claimed action is never touched."""
    if change is Change.DISABLED:
        await lifecycle.cancel_pending(
            session,
            account_id=account_id,
            conversation_ids=None,
            reason=StateReason.CONNECTION_DISABLED,
            now=now,
        )
    elif change is Change.RIGHTS_CHANGED:
        await lifecycle.outdate_pending_for_account(
            session,
            account_id=account_id,
            reason=StateReason.RIGHTS_CHANGED,
            include_approved=True,
            now=now,
        )


async def activate_pending(session: AsyncSession, account: Account, clock: Clock) -> ApplyResult:
    """The owner finished onboarding: an inert connection becomes live (J1)."""
    existing = await connection_for_account(session, account.id, for_update=True)
    if existing is None or existing.status != Status.PENDING_OWNER.value:
        return ApplyResult(Change.UNCHANGED, account.id)
    existing.status = Status.ACTIVE.value
    existing.updated_at = clock.now()
    event = BusinessConnectionEvent(
        account_id=account.id,
        connection_id=existing.connection_id,
        payload_digest="",
        rights=list(existing.rights),
        is_enabled=existing.is_enabled,
        change=Change.ACTIVATED.value,
        source=Source.ONBOARDING.value,
        observed_at=clock.now(),
    )
    session.add(event)
    await session.flush()
    return ApplyResult(Change.ACTIVATED, account.id, event.id)


async def resolve(session: AsyncSession, connection_id: str, clock: Clock) -> Resolution:
    """Whose is this connection id? Checks current ids, then history, then the
    foreign cache. UNKNOWN means it has to be bootstrapped (§6.4)."""
    current = (
        await session.execute(
            select(BusinessConnection).where(BusinessConnection.connection_id == connection_id)
        )
    ).scalar_one_or_none()
    if current is not None:
        return Resolution(ResolutionKind.OWNER, current.account_id, Status(current.status))

    historic_account = (
        await session.execute(
            select(BusinessConnectionEvent.account_id)
            .where(BusinessConnectionEvent.connection_id == connection_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if historic_account is not None:
        # An id a reconnect has replaced. The traffic is still the owner's;
        # what may be done with it follows the account's current connection.
        connection = await connection_for_account(session, historic_account)
        status = Status(connection.status) if connection is not None else None
        return Resolution(ResolutionKind.OWNER, historic_account, status)

    foreign = await session.get(ForeignConnection, connection_id)
    if foreign is not None:
        foreign.last_seen_at = clock.now()
        return Resolution(ResolutionKind.FOREIGN)
    return Resolution(ResolutionKind.UNKNOWN)
