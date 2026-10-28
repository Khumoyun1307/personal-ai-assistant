"""The policy loader: what `decide` sees comes from locked rows (S04.1 §3.4; R1, R3).

Telegram dates are compared only with Telegram dates, ingestion-order ids
only with ids; the ingestion barrier belongs to one process run. Synthetic
rows on a real PostgreSQL.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import BusinessConnection, Consent
from assistant.actions.config import ActionsConfig
from assistant.actions.policy_loader import KEY_INGESTION_BARRIER, ingestion_state, load_context
from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.core.errors import NotInAccountError
from assistant.db.models import SystemState
from assistant.inbox.models import InboundUpdate
from assistant.policy.context import ActionFacts, Phase, PolicyContext
from tests.integration.actions_env import CONFIG, Chat, add_message, seed_chat

pytestmark = pytest.mark.integration

RUN = "synthetic-process-run"
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def facts(now: datetime, kind: ActionKind = ActionKind.SEND_MESSAGE) -> ActionFacts:
    return ActionFacts(
        action_id=None,
        kind=kind,
        origin=ActionOrigin.AGENT_PROPOSAL,
        state=ActionState.PROPOSED,
        version=1,
        text_length=12,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        shown_through_message_id=None,
        owner_retry=False,
    )


async def load(
    session: AsyncSession,
    chat: Chat,
    now: datetime,
    *,
    config: ActionsConfig = CONFIG,
    **fields: Any,
) -> PolicyContext:
    values: dict[str, Any] = {
        "account_id": chat.account_id,
        "conversation_id": chat.conversation_id,
        "facts": facts(now),
        "base_message_id": chat.contact_message_id,
        "target_message_id": None,
        "owner_requested": False,
        "phase": Phase.EXECUTION,
        "now": now,
        "config": config,
        "ai_generated": True,
        "process_run_id": RUN,
        **fields,
    }
    return await load_context(session, **values)


async def message(
    session: AsyncSession, chat: Chat, direction: str, sent_at: datetime, **fields: Any
) -> int:
    return await add_message(
        session,
        chat.account_id,
        chat.conversation_id,
        direction=direction,
        sent_at=sent_at,
        **fields,
    )


async def test_loader_reads_the_newest_owner_message_by_telegram_date_even_when_ingested_late(
    db_session: AsyncSession,
) -> None:
    now = NOW
    chat = await seed_chat(db_session, now)
    await message(db_session, chat, "owner_manual", now - timedelta(minutes=5))
    # Ingested after it (a higher id) but sent earlier: the date decides.
    await message(db_session, chat, "owner_manual", now - timedelta(minutes=30))
    # A deleted owner message still means the owner acted.
    await message(db_session, chat, "owner_manual", now - timedelta(minutes=2), deleted=True)

    context = await load(db_session, chat, now)

    assert context.freshness.newest_owner_manual_sent_at == now - timedelta(minutes=2)


async def test_loader_reads_the_newest_contact_message_by_ingestion_order(
    db_session: AsyncSession,
) -> None:
    now = NOW
    chat = await seed_chat(db_session, now)
    later_sent = await message(db_session, chat, "contact", now - timedelta(minutes=10))
    newest_id = await message(db_session, chat, "contact", now - timedelta(minutes=50))

    context = await load(db_session, chat, now)
    assert context.freshness.newest_contact_message_id == newest_id
    assert newest_id > later_sent

    tombstone = await message(db_session, chat, "contact", now - timedelta(hours=2), deleted=True)
    context = await load(db_session, chat, now)
    assert context.freshness.newest_contact_message_id == tombstone


async def test_loader_marks_a_deleted_base_message_missing(db_session: AsyncSession) -> None:
    now = NOW
    chat = await seed_chat(db_session, now)
    deleted = await message(db_session, chat, "contact", now, deleted=True)
    purged = await message(db_session, chat, "contact", now, purged_at=now)

    present = await load(db_session, chat, now)
    without_base = await load(db_session, chat, now, base_message_id=None)
    gone = await load(db_session, chat, now, base_message_id=deleted)
    erased = await load(db_session, chat, now, base_message_id=purged)
    unknown = await load(db_session, chat, now, base_message_id=purged + 1000)

    assert present.freshness.base_message_present is True
    assert without_base.freshness.base_message_present is True
    assert gone.freshness.base_message_present is False
    assert erased.freshness.base_message_present is False
    assert unknown.freshness.base_message_present is False


async def test_loader_counts_pending_inbound_rows_of_any_account(db_session: AsyncSession) -> None:
    now = NOW
    chat = await seed_chat(db_session, now)
    other = await seed_chat(db_session, now)
    for update_id, (account_id, status) in enumerate(
        [(None, "pending"), (other.account_id, "pending"), (chat.account_id, "processed")],
        start=900_001,
    ):
        db_session.add(
            InboundUpdate(
                bot_id=1, update_id=update_id, kind="message", account_id=account_id, status=status
            )
        )
    await db_session.flush()

    context = await load(db_session, chat, now)

    assert context.ingestion.pending_inbound >= 2
    before = context.ingestion.pending_inbound
    db_session.add(InboundUpdate(bot_id=1, update_id=900_010, kind="message", status="pending"))
    await db_session.flush()
    assert (await ingestion_state(db_session, process_run_id=RUN)).pending_inbound == before + 1


async def barrier(session: AsyncSession, value: dict[str, Any] | None) -> None:
    await session.execute(
        SystemState.__table__.delete().where(SystemState.key == KEY_INGESTION_BARRIER)
    )
    if value is not None:
        session.add(SystemState(key=KEY_INGESTION_BARRIER, value=value))
    await session.flush()


async def test_barrier_is_not_passed_without_the_system_state_key(
    db_session: AsyncSession,
) -> None:
    await barrier(db_session, None)
    assert (await ingestion_state(db_session, process_run_id=RUN)).barrier_passed is False


async def test_barrier_from_a_previous_process_run_is_not_passed(db_session: AsyncSession) -> None:
    caught_up = {"process_run_id": "previous-run", "caught_up_at": "2026-10-27T11:00:00+00:00"}
    await barrier(db_session, caught_up)
    assert (await ingestion_state(db_session, process_run_id=RUN)).barrier_passed is False


async def test_barrier_passes_only_for_this_process_run_after_catch_up(
    db_session: AsyncSession,
) -> None:
    await barrier(db_session, {"process_run_id": RUN, "caught_up_at": None})
    assert (await ingestion_state(db_session, process_run_id=RUN)).barrier_passed is False

    await barrier(db_session, {"process_run_id": RUN, "caught_up_at": "2026-10-27T11:00:00+00:00"})
    assert (await ingestion_state(db_session, process_run_id=RUN)).barrier_passed is True
    # No process run (a proposal, a tool): never passed.
    assert (await ingestion_state(db_session, process_run_id=None)).barrier_passed is False


async def test_loader_uses_created_at_as_the_takeover_reference_for_owner_requested_actions(
    db_session: AsyncSession,
) -> None:
    now = NOW
    chat = await seed_chat(db_session, now)

    proactive = await load(db_session, chat, now, owner_requested=False)
    requested = await load(db_session, chat, now, owner_requested=True)

    assert proactive.freshness.base_message_sent_at == now - timedelta(hours=1)
    assert requested.freshness.base_message_sent_at is None


async def test_loader_checks_managed_consent_for_the_draft_routes_processor(
    db_session: AsyncSession,
) -> None:
    now = NOW
    agreed = await seed_chat(db_session, now)
    never = await seed_chat(db_session, now, consent=False)

    ok = await load(db_session, agreed, now)
    other_processor = await load(
        db_session, agreed, now, config=replace(CONFIG, draft_route_model="claude-synthetic")
    )
    unknown_model = await load(
        db_session, agreed, now, config=replace(CONFIG, draft_route_model="mystery-model")
    )
    missing = await load(db_session, never, now)
    await db_session.execute(
        update(Consent).where(Consent.account_id == agreed.account_id).values(revoked_at=now)
    )
    revoked = await load(db_session, agreed, now)

    assert ok.account.managed_consent_ok is True
    assert other_processor.account.managed_consent_ok is False
    assert unknown_model.account.managed_consent_ok is False
    assert missing.account.managed_consent_ok is False
    assert revoked.account.managed_consent_ok is False


@pytest.mark.parametrize(
    ("enabled", "rights", "can_reply"),
    [
        (True, ["can_reply"], True),
        (True, ["can_reply", "can_read_messages"], True),
        (True, ["can_read_messages"], False),
        (True, [], False),
        (False, ["can_reply"], False),
    ],
)
async def test_loader_derives_can_reply_from_the_rights(
    db_session: AsyncSession, enabled: bool, rights: list[str], can_reply: bool
) -> None:
    now = NOW
    chat = await seed_chat(db_session, now)
    await db_session.execute(
        update(BusinessConnection)
        .where(BusinessConnection.account_id == chat.account_id)
        .values(is_enabled=enabled, rights=rights)
    )
    without = await seed_chat(db_session, now, connection=False)

    context = await load(db_session, chat, now)
    missing = await load(db_session, without, now)

    assert context.connection.exists is True
    assert context.connection.is_enabled is enabled
    assert context.connection.can_reply is can_reply
    assert (missing.connection.exists, missing.connection.can_reply) == (False, False)


async def test_loader_reads_the_edit_target_only_for_targeted_kinds(
    db_session: AsyncSession,
) -> None:
    now = NOW
    chat = await seed_chat(db_session, now)
    sent = await message(db_session, chat, "assistant_sent", now)
    owners = await message(db_session, chat, "owner_manual", now)
    edit = facts(now, ActionKind.EDIT_MESSAGE)

    assert (await load(db_session, chat, now)).freshness.target_is_assistant_sent is None
    ours = await load(db_session, chat, now, facts=edit, target_message_id=sent)
    not_ours = await load(db_session, chat, now, facts=edit, target_message_id=owners)
    assert ours.freshness.target_is_assistant_sent is True
    assert not_ours.freshness.target_is_assistant_sent is False


async def test_loader_refuses_another_accounts_conversation(db_session: AsyncSession) -> None:
    now = NOW
    mine = await seed_chat(db_session, now)
    theirs = await seed_chat(db_session, now)

    with pytest.raises(NotInAccountError):
        await load(db_session, mine, now, conversation_id=theirs.conversation_id)
    # Another account's message as base or target is simply not there.
    foreign_base = await load(db_session, mine, now, base_message_id=theirs.contact_message_id)
    assert foreign_base.freshness.base_message_present is False
