"""Business connection registry, end to end through the inbox (S02.1).

Real `InboxProcessor`, real `route_update`, real Postgres; Telegram's
`getBusinessConnection` is a fake. Covers the S02.1 test list
(IMPLEMENTATION_PLAN §4):

- a connection from a non-allowlisted user leaves no tenant row and no content;
- a connection before /start stays inert and goes live on onboarding;
- a connection id rotation re-links the same account;
- a missing initial connection update is bootstrapped from the first message;
- disabled ⇒ business processing stops; rights absent ⇒ no permissions.

Payloads are synthetic and shaped like aiogram's JSON dump (TF §12 LV-01/05/12).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from assistant.accounts import service
from assistant.accounts.models import (
    Account,
    BusinessConnection,
    BusinessConnectionEvent,
    ControlTurn,
    ForeignConnection,
    PendingInput,
)
from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.core.errors import TransientError
from assistant.db.uow import transaction
from assistant.telegram.ingress.writer import write_batch
from tests.integration.business_env import (
    BOT_ID,
    OWNER,
    STRANGER,
    Env,
    business_message,
    connection_body,
    connection_row,
    connection_update,
    count,
    decline_managed,
    deliver,
    environment,
    events,
    grant_managed,
    inbound,
    notify_jobs,
    onboard,
    payload_is_sql_null,
    press,
    private_message,
)

pytestmark = pytest.mark.integration


# ------------------------------------------------------------ non-owners (RG-07)
async def test_rg07_a_connection_from_a_non_allowlisted_user_leaves_no_tenant_trace(
    env: Env,
) -> None:
    """RG-07 (never weaken): a non-owner connection creates no domain rows and
    its payload is cleared (§6 invariants 7, 12)."""
    update = connection_update(user_id=STRANGER, connection_id="synthetic-foreign")
    await deliver(env, update)

    row = await inbound(env, update)
    assert row.status == "rejected"
    assert row.account_id is None
    assert await payload_is_sql_null(env, update)
    assert await count(env, Account) == 0
    assert await count(env, BusinessConnection) == 0
    assert await count(env, BusinessConnectionEvent) == 0
    assert await count(env, ForeignConnection) == 1
    assert env.sender.sends == []


async def test_traffic_on_a_known_foreign_connection_is_rejected_without_asking_telegram(
    env: Env,
) -> None:
    await deliver(env, connection_update(user_id=STRANGER, connection_id="synthetic-foreign"))
    message = business_message("synthetic-foreign", from_id=STRANGER)
    await deliver(env, message)

    row = await inbound(env, message)
    assert row.status == "rejected"
    assert await payload_is_sql_null(env, message)
    assert env.lookup.calls == []
    assert await count(env, Account) == 0


async def test_rg07_an_owner_removed_from_the_allowlist_stores_nothing_more(
    migrated_url: str,
) -> None:
    """RG-07: the allowlist is re-checked on every business update. An owner
    whose id was removed from OWNER_TELEGRAM_IDS keeps their old rows, but
    their still-active connection stores nothing further — not live traffic,
    not a dead letter — and the payload is cleared (§6 invariants 7, 12)."""
    # A deployment restarted with the owner no longer in the allowlist.
    async with environment(migrated_url, owner_ids=frozenset({STRANGER})) as env:
        await _seed_live_owner(env)
        live = business_message(message_text="synthetic after")
        poison = business_message(message_text="synthetic poison", extra={"message_id": None})
        await deliver(env, live)
        for _ in range(3):
            await deliver(env, poison)

        row = await inbound(env, live)
        assert row.status == "rejected"
        assert row.account_id is None
        assert await payload_is_sql_null(env, live)
        assert (await inbound(env, poison)).status in {"rejected", "failed"}
        assert await count(env, Message) == 0
        assert await count(env, Contact) == 0
        assert await count(env, Conversation) == 0
        assert await count(env, CoverageGap) == 0


async def _seed_live_owner(env: Env) -> None:
    """An onboarded owner with an active connection, written directly (the
    allowlist in this environment would refuse to create them)."""
    async with transaction(env.factory) as uow:
        account, _ = await service.get_or_create_account(
            uow.session, telegram_user_id=OWNER, clock=env.clock
        )
        account.status = "active"
        await service.set_control_chat(uow.session, account, OWNER)
        uow.session.add(
            BusinessConnection(
                account_id=account.id,
                connection_id="synthetic-conn-a",
                is_enabled=True,
                rights=["can_reply"],
                capability_tier="can_reply",
                user_chat_id=OWNER,
                status="active",
                connected_at=env.clock.now(),
            )
        )


# ------------------------------------------------------- before onboarding
async def test_a_connection_before_start_stays_inert(env: Env) -> None:
    await deliver(env, connection_update())

    connection = await connection_row(env)
    assert connection is not None
    assert connection.status == "pending_owner"
    async with env.factory() as session:
        account = (await session.execute(select(Account))).scalar_one()
    assert account.status == "onboarding"
    assert await notify_jobs(env) == []  # nobody to tell yet

    message = business_message("synthetic-conn-a")
    await deliver(env, message)
    row = await inbound(env, message)
    assert row.status == "ignored"
    assert row.account_id == account.id
    assert await payload_is_sql_null(env, message)


async def test_finishing_onboarding_activates_the_waiting_connection(env: Env) -> None:
    await deliver(env, connection_update())
    await onboard(env)

    connection = await connection_row(env)
    assert connection is not None
    assert connection.status == "active"
    history = await events(env)
    assert [event.change for event in history] == ["pending_owner", "activated"]
    assert history[-1].source == "onboarding"
    assert await notify_jobs(env) == [history[-1].id]


# ------------------------------------------------------------ lifecycle
async def test_a_connection_for_an_onboarded_owner_goes_live_with_its_tier(env: Env) -> None:
    await onboard(env)
    update = connection_update(rights=("can_reply", "can_read_messages", "can_edit_bio"))
    await deliver(env, update)

    connection = await connection_row(env)
    assert connection is not None
    assert (connection.status, connection.capability_tier) == ("active", "extended")
    assert connection.rights == ["can_edit_bio", "can_read_messages", "can_reply"]
    assert (await inbound(env, update)).status == "processed"
    history = await events(env)
    assert [event.change for event in history] == ["connected"]
    assert await notify_jobs(env) == [history[0].id]


async def test_rights_absent_means_no_permissions(env: Env) -> None:
    await onboard(env)
    await deliver(env, connection_update(rights=None))

    connection = await connection_row(env)
    assert connection is not None
    assert connection.rights == []
    assert connection.capability_tier == "observe_only"


async def test_a_disabled_connection_has_no_permissions_and_stops_business_processing(
    env: Env,
) -> None:
    await onboard(env)
    await deliver(env, connection_update(rights=("can_reply",)))
    # LV-01: a disconnect keeps the id, sets is_enabled=false and drops rights.
    await deliver(env, connection_update(enabled=False, rights=("can_reply",)))

    connection = await connection_row(env)
    assert connection is not None
    assert (connection.status, connection.capability_tier, connection.rights) == (
        "disabled",
        "none",
        [],
    )
    assert [event.change for event in await events(env)] == ["connected", "disabled"]
    assert len(await notify_jobs(env)) == 2

    message = business_message("synthetic-conn-a")
    await deliver(env, message)
    assert (await inbound(env, message)).status == "ignored"
    assert await payload_is_sql_null(env, message)


async def test_a_rights_change_is_recorded_and_announced(env: Env) -> None:
    await onboard(env)
    await deliver(env, connection_update(rights=()))
    await deliver(env, connection_update(rights=("can_reply",)))

    connection = await connection_row(env)
    assert connection is not None
    assert connection.capability_tier == "can_reply"
    assert [event.change for event in await events(env)] == ["connected", "rights_changed"]
    assert len(await notify_jobs(env)) == 2


async def test_the_same_state_twice_records_and_announces_once(env: Env) -> None:
    await onboard(env)
    await deliver(env, connection_update())
    await deliver(env, connection_update())

    assert len(await events(env)) == 1
    assert len(await notify_jobs(env)) == 1


async def test_a_reconnect_with_a_new_id_relinks_the_same_account(env: Env) -> None:
    await onboard(env)
    await deliver(env, connection_update(connection_id="synthetic-conn-a", date=1792350000))
    await deliver(env, connection_update(connection_id="synthetic-conn-b", date=1792360000))

    connection = await connection_row(env)
    assert connection is not None
    assert connection.connection_id == "synthetic-conn-b"
    assert await count(env, BusinessConnection) == 1
    assert [event.change for event in await events(env)] == ["connected", "reconnected"]

    # A late message carrying the replaced id is still the owner's traffic.
    message = business_message("synthetic-conn-a")
    await deliver(env, message)
    row = await inbound(env, message)
    assert row.account_id == connection.account_id
    assert env.lookup.calls == []


async def test_a_late_event_about_an_older_id_does_not_undo_a_reconnect(env: Env) -> None:
    await onboard(env)
    await deliver(env, connection_update(connection_id="synthetic-conn-b", date=1792360000))
    await deliver(env, connection_update(connection_id="synthetic-conn-a", date=1792350000))

    connection = await connection_row(env)
    assert connection is not None
    assert connection.connection_id == "synthetic-conn-b"
    assert [event.change for event in await events(env)] == ["connected", "stale_event"]
    assert len(await notify_jobs(env)) == 1


# ------------------------------------------------------------ bootstrap
async def test_a_missing_connection_update_is_bootstrapped_from_the_first_message(
    env: Env,
) -> None:
    await onboard(env)
    env.lookup.known["synthetic-conn-a"] = connection_body()

    first = business_message("synthetic-conn-a")
    await deliver(env, first)
    await deliver(env, business_message("synthetic-conn-a"))

    assert env.lookup.calls == ["synthetic-conn-a"]  # once, then cached
    connection = await connection_row(env)
    assert connection is not None
    assert connection.status == "active"
    history = await events(env)
    assert [(event.change, event.source) for event in history] == [("connected", "bootstrap")]
    assert await notify_jobs(env) == [history[0].id]
    assert (await inbound(env, first)).account_id == connection.account_id


async def test_a_bootstrapped_foreign_connection_is_rejected(env: Env) -> None:
    env.lookup.known["synthetic-foreign"] = connection_body(
        user_id=STRANGER, connection_id="synthetic-foreign"
    )
    message = business_message("synthetic-foreign", from_id=STRANGER)
    await deliver(env, message)

    assert (await inbound(env, message)).status == "rejected"
    assert await payload_is_sql_null(env, message)
    assert await count(env, Account) == 0


async def test_an_id_telegram_does_not_know_fails_closed(env: Env) -> None:
    message = business_message("synthetic-unknown")
    await deliver(env, message)

    row = await inbound(env, message)
    assert row.status == "ignored"
    assert row.account_id is None
    assert await payload_is_sql_null(env, message)


async def test_a_bootstrap_network_failure_leaves_the_update_pending(env: Env) -> None:
    env.lookup.fail = TransientError("network")
    message = business_message("synthetic-conn-a")
    async with transaction(env.factory) as uow:
        await write_batch(uow.session, bot_id=BOT_ID, updates=[message])

    with pytest.raises(TransientError):
        await env.processor.process_batch()

    row = await inbound(env, message)
    assert (row.status, row.attempts) == ("pending", 0)
    assert row.payload is not None


# ------------------------------------------------------------ boundary
async def test_a_business_message_from_the_owner_never_reaches_the_control_chat(
    env: Env,
) -> None:
    """§6 invariant 5, now with a live connection: still no control reply."""
    await onboard(env)
    await deliver(env, connection_update())
    sends_before = len(env.sender.sends)

    await deliver(env, business_message("synthetic-conn-a", from_id=OWNER))

    assert len(env.sender.sends) == sends_before
    async with env.factory() as session:
        consents = await service.active_consent(
            session,
            (await session.execute(select(Account.id))).scalar_one(),
            kind=service.CONSENT_AI_MANAGED,
        )
    assert not consents.granted


# ------------------------------------------------------- managed consent
async def managed_consent(env: Env) -> service.ConsentState:
    async with env.factory() as session:
        account_id = (await session.execute(select(Account.id))).scalar_one()
        return await service.active_consent(session, account_id, kind=service.CONSENT_AI_MANAGED)


async def test_managed_consent_is_asked_and_recorded_with_the_policy_and_processor(
    env: Env,
) -> None:
    await onboard(env)
    await deliver(env, connection_update())

    await deliver(env, private_message(OWNER, "/privacy managed"))
    question = env.sender.sends[-1]
    assert question["text"].startswith("<b>Let Alviro's AI agent work with these chats?</b>")
    assert "The text is processed by OpenAI" in question["text"]
    assert "can read the messages saved from the chats you connected" in question["text"]
    assert "You can change this any time with /privacy." in question["text"]
    keyboard = question["reply_markup"]
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert labels == ["Turn on", "Not now"]
    ops = [b.callback_data.rsplit(":", 1)[-1] for row in keyboard.inline_keyboard for b in row]
    assert ops == ["agree", "decline"]  # only the labels changed, not the callback data
    await press(env, "managed_consent", "agree")

    state = await managed_consent(env)
    assert state.granted
    assert (state.policy_version, state.processors) == ("v5", ("OpenAI",))


async def test_declining_managed_consent_grants_nothing(env: Env) -> None:
    await onboard(env)
    await deliver(env, connection_update())
    await decline_managed(env)

    assert not (await managed_consent(env)).granted
    # The question card is edited to the answer; the deletion card follows (S02.4).
    assert any("Not now." in edit["text"] for edit in env.sender.edits)
    assert "Delete what I stored" in env.sender.texts[-1]


async def test_an_unrelated_message_is_never_taken_as_an_answer(env: Env) -> None:
    """S02.6: the question is a card. A message typed while it is open — an
    ordinary request, or even "yes"/"no" meant for something else — neither
    grants nor declines, and is handled as what it is."""
    await onboard(env)
    await deliver(env, connection_update())
    await deliver(env, private_message(OWNER, "/privacy managed"))
    sends_before = len(env.sender.sends)

    for text in ("what did I miss today?", "no", "yes"):
        await deliver(env, private_message(OWNER, text))

    assert not (await managed_consent(env)).granted
    assert all("Delete what I stored" not in s["text"] for s in env.sender.sends[sends_before:])
    async with env.factory() as session:
        card = (
            await session.execute(
                select(PendingInput).where(PendingInput.kind == "managed_consent")
            )
        ).scalar_one()
        turns = (await session.execute(select(ControlTurn))).scalars().all()
    assert card.token is not None  # still open, still tappable
    assert len(turns) == 3  # each message went to the assistant as a turn


async def test_managed_consent_is_not_asked_before_a_connection_exists(env: Env) -> None:
    await onboard(env)
    await deliver(env, private_message(OWNER, "/privacy managed"))
    not_connected = env.sender.sends[-1]["text"]
    await deliver(env, private_message(OWNER, "yes"))

    assert "not connected" in not_connected
    assert not (await managed_consent(env)).granted


async def test_privacy_shows_both_consents_and_revoke_withdraws_both(env: Env) -> None:
    await onboard(env)
    await deliver(env, connection_update())
    await grant_managed(env)

    await deliver(env, private_message(OWNER, "/privacy"))
    shown = env.sender.sends[-1]["text"]
    assert "AI for this chat: granted" in shown
    assert "AI for your chats with other people: granted" in shown

    await deliver(env, private_message(OWNER, "/privacy revoke"))
    assert not (await managed_consent(env)).granted


async def test_privacy_says_connected_chats_are_saved_whether_or_not_ai_is_on(env: Env) -> None:
    """Review finding B: `/privacy` must not claim other people's messages are
    discarded. Messages from connected chats are stored regardless of
    `ai_managed`; only a stranger's direct message is not kept (policy v4)."""
    await onboard(env)
    await deliver(env, connection_update())

    await deliver(env, private_message(OWNER, "/privacy"))
    shown = env.sender.sends[-1]["text"]
    assert "AI for your chats with other people: <b>not granted</b>" in shown
    assert "discarded" not in shown
    assert "I save new messages (text kept up to 90 days), whether or not AI is on" in shown
    assert "If someone else writes to me directly, I don't keep their message." in shown
    # Policy v5: what outlives the 90-day message limit.
    assert (
        "Notes you ask me to remember and summaries of your chats are kept until you "
        "delete them." in shown
    )
    assert "a server in the US (Oracle, Ashburn)" in shown
    assert "Backblaze B2 in the EU, for up to 35 days" in shown
