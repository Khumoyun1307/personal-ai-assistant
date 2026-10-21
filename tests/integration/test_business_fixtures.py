"""The observed 2026-10-18 session, replayed through the real pipeline.

Every record of the sanitised fixture — connection updates, messages, edits,
deletions, in `update_id` order — goes through the inbox processor exactly as
it would in production, with `getBusinessConnection` answered from the
fixture's own lookups. The assertions are structural (counts, directions,
tombstones); the content is synthetic.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from assistant.conversations.models import Contact, Conversation, Message
from assistant.inbox.models import InboundUpdate
from assistant.telegram.routing import BUSINESS_KINDS
from tests.contract.test_telegram_fixtures import bot_id, lookups, records
from tests.integration.business_env import deliver, environment, private_message

pytestmark = pytest.mark.integration


def _updates() -> list[dict[str, Any]]:
    rows = [r for r in records() if r.get("kind") == "update"]
    return sorted((r["payload"] for r in rows), key=lambda u: u["update_id"])


async def test_the_observed_session_replays_into_the_expected_store(migrated_url: str) -> None:
    updates = _updates()
    owner_id = next(
        u["business_connection"]["user"]["id"] for u in updates if "business_connection" in u
    )
    async with environment(migrated_url, owner_ids=frozenset({owner_id}), bot_id=bot_id()) as env:
        for lookup in lookups():
            env.lookup.known.setdefault(lookup["id"], lookup)
        await deliver(env, private_message(owner_id, "/start"))
        await deliver(env, private_message(owner_id, "yes"))
        for update in updates:
            await deliver(env, update)

        async with env.factory() as session:
            statuses = (
                await session.execute(
                    select(InboundUpdate.kind, InboundUpdate.status).where(
                        InboundUpdate.kind.in_(BUSINESS_KINDS)
                    )
                )
            ).all()
            stored = list((await session.execute(select(Message))).scalars().all())
            conversations = list((await session.execute(select(Conversation))).scalars().all())
            contacts = list((await session.execute(select(Contact))).scalars().all())

    # Every business update was handled; none dead-lettered.
    assert statuses
    assert {status for _, status in statuses} <= {"processed", "ignored"}
    # One test chat, one contact (the owner's second account), as observed.
    assert len(conversations) == 1
    assert len(contacts) == 1
    directions = [m.direction for m in stored if m.direction is not None]
    assert set(directions) == {"contact", "owner_manual"}
    assert directions.count("owner_manual") >= 2  # LV-02: phone and desktop
    # LV-07: five deletions in the session; each purged a row or left a tombstone.
    tombstoned = [m for m in stored if m.deleted_at is not None]
    assert len(tombstoned) >= 5
    assert all(m.text is None and m.content_hash is None for m in tombstoned)
    # LV-11: the four media kinds are recorded as kinds, never with a file_id.
    kinds = {m.content_type for m in stored}
    assert {"photo", "voice", "document", "video_note"} <= kinds
    assert all("file_id" not in (m.media or {}) for m in stored)
    conv = conversations[0]
    assert conv.last_contact_msg_at is not None
    assert conv.coverage_started_at is not None
    assert conv.coverage_started_at <= conv.last_contact_msg_at
