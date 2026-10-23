"""Connection-change notices to the owner (FR-ONB-04, -07; PRD J1 steps 5-6).

Runs as the `connections.notify_owner(event_id)` job, deferred in the same
transaction as the registry change. It reloads the event and the current
state, composes the notice and sends it through `OwnerAlerts`, deduplicated
by event id — so a re-run job, or an event processed twice, pages once.

When a connection becomes live and managed-conversation consent is not yet
given, the notice also asks for it (J1 step 5, FR-ONB-05b) as a card with
Agree / Decline buttons. The card is recorded only after the notice was
delivered; a reconnect does not ask again while a card is open or within a
day of a decline; and no free-text message is ever read as the answer.
Consent is asked, never assumed (§6 invariant 9).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.callbacks import encode, new_token
from assistant.accounts.connections import Change, Tier, connection_for_account, unused_rights
from assistant.accounts.models import Account, BusinessConnectionEvent
from assistant.core.channel import Button
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.notify import AlertKind, AlertResult, OwnerAlerts

TIER_TEXT: dict[Tier, str] = {
    Tier.NONE: "<b>No permissions</b> — the connection is off.",
    Tier.OBSERVE_ONLY: (
        "<b>Observe only</b> — I can see new messages in the chats you included, "
        "but I cannot reply in them."
    ),
    Tier.CAN_REPLY: (
        "<b>Can reply</b> — I can see new messages in the chats you included, and I "
        "can reply in them only when you approve a reply or delegate a conversation."
    ),
    Tier.EXTENDED: (
        "<b>Extended</b> — as <i>Can reply</i>, plus rights I use only for my own "
        "messages (removing them). I never touch messages you or your contacts wrote."
    ),
}

VISIBILITY = "I see the chats you included from now on. I cannot see older history."

MANAGED_CONSENT_QUESTION = (
    "<b>One more question.</b> To summarize these chats and help you with them, I "
    "need to send their text to <b>{processors}</b>. Until you agree, nothing from "
    "them is sent to any AI provider.\n\n"
    "Tap <b>Agree</b> or <b>Decline</b> below. "
    "You can change this later with /privacy."
)
#: When the question is already open, or was declined within a day.
MANAGED_CONSENT_STILL_OPEN = (
    "My question about AI for these chats is still open above. Nothing from them "
    "is sent to any AI provider until you agree."
)
MANAGED_CONSENT_DECLINED_NOTE = (
    "You declined AI for these chats, so nothing from them is sent to any AI "
    "provider. Send <code>/privacy managed</code> if you change your mind."
)


def consent_buttons(token: str) -> list[Button]:
    """Agree / Decline for a `managed_consent` card (§6.7)."""
    kind = service.MANAGED_CONSENT_CARD
    return [
        Button("Agree", encode(kind, token, "agree")),
        Button("Decline", encode(kind, token, "decline")),
    ]


def _rights_note(rights: list[str]) -> str:
    surplus = unused_rights(frozenset(rights))
    if not surplus:
        return ""
    return (
        "\n\nRights you granted that I do not use: "
        + ", ".join(f"<code>{name}</code>" for name in surplus)
        + ". You can turn them off where you connected me."
    )


def compose(change: Change, tier: Tier, rights: list[str]) -> str:
    """The notice body. Plain statements of fact (§6 invariant 11)."""
    capability = f"Capability: {TIER_TEXT[tier]}"
    match change:
        case Change.CONNECTED | Change.ACTIVATED:
            return (
                f"✅ <b>Connected to your account.</b>\n\n{capability}\n\n{VISIBILITY}"
                + _rights_note(rights)
            )
        case Change.RECONNECTED:
            return (
                f"🔄 <b>Reconnected to your account.</b>\n\n{capability}\n\n"
                "Messages sent while I was disconnected never reached me." + _rights_note(rights)
            )
        case Change.RIGHTS_CHANGED:
            return f"🔧 <b>My permissions changed.</b>\n\n{capability}" + _rights_note(rights)
        case Change.DISABLED:
            return (
                "⚠️ <b>I am disconnected from your account.</b>\n\n"
                "I have stopped processing your chats. Messages sent while I am "
                "disconnected will not reach me. Connect me again to resume."
            )
        case _:
            raise ValueError(f"no notice for change {change.value}")


async def _managed_consent_missing(session: AsyncSession, account_id: int) -> bool:
    state = await service.active_consent(session, account_id, kind=service.CONSENT_AI_MANAGED)
    return not state.granted


async def notify_connection_change(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    alerts: OwnerAlerts,
    event_id: int,
) -> AlertResult:
    async with transaction(session_factory) as uow:
        session = uow.session
        event = await session.get(BusinessConnectionEvent, event_id)
        if event is None:
            return AlertResult(False, "event_gone")
        account = await session.get(Account, event.account_id)
        connection = await connection_for_account(session, event.account_id)
        if account is None or connection is None or account.control_chat_id is None:
            return AlertResult(False, "unreachable")
        change = Change(event.change)
        # The tier now, not at event time: if rights changed again since, the
        # owner should read the current truth.
        body = compose(change, Tier(connection.capability_tier), list(connection.rights))
        live = change in (Change.CONNECTED, Change.ACTIVATED, Change.RECONNECTED)
        token: str | None = None
        if live and await _managed_consent_missing(session, account.id):
            if await service.live_card(
                session, account_id=account.id, kind=service.MANAGED_CONSENT_CARD, clock=clock
            ):
                body += "\n\n" + MANAGED_CONSENT_STILL_OPEN
            elif await service.managed_consent_declined_since(
                session,
                account_id=account.id,
                since=clock.now() - service.MANAGED_CONSENT_TTL,
            ):
                body += "\n\n" + MANAGED_CONSENT_DECLINED_NOTE
            else:
                # The card is recorded only once the question has arrived
                # (below): a failed send leaves nothing open.
                token = new_token()
                body += "\n\n" + MANAGED_CONSENT_QUESTION.format(
                    processors=", ".join(service.AI_PROCESSORS)
                )
        account_id = account.id

    result = await alerts.notify_owner(
        account_id,
        AlertKind.CONNECTION_CHANGE,
        dedupe_key=f"connection_event:{event_id}",
        buttons=consent_buttons(token) if token is not None else (),
        body=body,
    )
    if token is not None and result.sent:
        async with transaction(session_factory) as uow:
            await service.create_card(
                uow.session,
                account_id=account_id,
                kind=service.MANAGED_CONSENT_CARD,
                clock=clock,
                ttl=service.MANAGED_CONSENT_TTL,
                prompt_message_id=result.message_id,
                token=token,
            )
    return result
