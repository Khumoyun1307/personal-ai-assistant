"""S03.3 verification pass (2026-10-26): follow-ups to the safety review.

1. S3 refs survive a read and a re-save:
   `test_a_note_re_saved_after_reading_keeps_its_chat_and_goes_with_it`,
   `test_a_correction_of_a_tainted_note_keeps_its_refs_and_taint`.
2. S2/invariant 4: a tainted note about A that came from B's chat never
   reaches A's contact-facing run:
   `test_a_tainted_note_drawn_from_another_chat_is_not_contact_facing`.
3. `list_memories` honours the processor-checked consent:
   `test_list_memories_withholds_when_consent_names_another_processor`.
4. Rows saved before `managed` existed: `test_an_old_tainted_row_counts_as_managed`.
5. Each contact-facing filter proven alone:
   `test_each_owner_item_filter_holds_on_its_own`.
Synthetic text only; FakeLLM only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, update

from assistant.accounts import service
from assistant.accounts.models import Account, ControlTurn, PendingInput
from assistant.agent.models import ToolConfirmation
from assistant.agent.runtime import RunStatus
from assistant.llm.port import LLMRequest, ToolResultItem
from assistant.memory import items
from assistant.memory.models import MemoryItem
from assistant.privacy.deletion import delete_managed_data, purge_conversation
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import (
    MEMORY_FLAGS,
    READ_FLAGS,
    applier,
    args,
    ask,
    one_id,
    request_texts,
    runtime,
)
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    grant_managed,
)

pytestmark = pytest.mark.integration

FLAGS = READ_FLAGS | MEMORY_FLAGS


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(i.output) for i in request.items if isinstance(i, ToolResultItem)]


def remember_args(content: str, **extra: Any) -> str:
    values: dict[str, Any] = {
        "content": content,
        "kind": "fact",
        "contact_id": None,
        "conversation_id": None,
        "applies_to_replies": None,
        "replaces_memory_id": None,
    }
    values.update(extra)
    return args(**values)


async def seed(env: Env) -> tuple[int, int, int, int, int]:
    """Chats A (Kamola) and B (Bobur): (account, conv A, contact A, conv B, contact B)."""
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text="SYNTH A", first_name="Kamola"))
    await deliver(
        env,
        business_message(
            message_id=2,
            from_id=OTHER_CONTACT,
            chat_id=OTHER_CONTACT,
            message_text="SYNTH B",
            first_name="Bobur",
        ),
    )
    account = await one_id(env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER)
    conv_a = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    conv_b = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=OTHER_CONTACT
    )
    contact_a = await one_id(
        env, "SELECT contact_id FROM app.conversations WHERE id = :c", c=conv_a
    )
    contact_b = await one_id(
        env, "SELECT contact_id FROM app.conversations WHERE id = :c", c=conv_b
    )
    return account, conv_a, contact_a, conv_b, contact_b


async def save(env: Env, account: int, content: str, **kwargs: Any) -> int:
    async with env.factory() as session, session.begin():
        saved = await items.save(
            session,
            account_id=account,
            content=content,
            kind=kwargs.pop("kind", "fact"),
            actor="owner",
            now=env.clock.now(),
            **kwargs,
        )
    return saved.item.memory_id


async def confirm_latest(env: Env) -> None:
    async with env.factory() as session:
        row = (
            (await session.execute(select(ToolConfirmation).order_by(ToolConfirmation.id.desc())))
            .scalars()
            .first()
        )
        assert row is not None
        card = (
            await session.execute(
                select(PendingInput)
                .where(PendingInput.kind == "tool_confirm")
                .where(PendingInput.ref_id == row.id)
            )
        ).scalar_one()
    await deliver(env, callback(f"tool_confirm:{card.token}:confirm"))
    assert (await applier(env, FakeChannel(), flags=FLAGS).apply(row.id)).status == "applied"


async def purge(env: Env, account: int, conversation: int, chat_id: int) -> None:
    async with env.factory() as session, session.begin():
        await purge_conversation(
            session,
            account_id=account,
            conversation_id=conversation,
            business_chat_id=chat_id,
            clock=env.clock,
        )


# ------------------------------------------------------------ 1. S3 refs survive
async def test_a_note_re_saved_after_reading_keeps_its_chat_and_goes_with_it(env: Env) -> None:
    account, _, _, conv_b, _ = await seed(env)
    await save(
        env,
        account,
        "SYNTH-N1 from B's chat",
        tainted=True,
        refs={"conversation_ids": [conv_b], "contact_ids": []},
    )
    turn = await ask(env, "Save that again")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "list_memories",
                args(contact_id=None, conversation_id=None, scope=None, limit=None),
            )
        ),
        tool_response(("c2", "remember", remember_args("SYNTH-N2 from B's chat"))),
        text_response("SYNTH-REPLY about B"),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    assert results(fake.requests[2])[-1]["status"] == "awaiting_confirmation"
    await confirm_latest(env)
    await purge(env, account, conv_b, OTHER_CONTACT)
    async with env.factory() as session:
        left = (await session.execute(select(MemoryItem.content))).scalars().all()
        reply = (
            (
                await session.execute(
                    select(ControlTurn.text)
                    .where(ControlTurn.role == "assistant")
                    .order_by(ControlTurn.id.desc())
                )
            )
            .scalars()
            .first()
        )
    assert left == []
    assert reply is None  # the reply that listed it is redacted too


async def test_a_correction_of_a_tainted_note_keeps_its_refs_and_taint(env: Env) -> None:
    account, _, _, conv_b, _ = await seed(env)
    old = await save(
        env,
        account,
        "SYNTH old",
        tainted=True,
        refs={"conversation_ids": [conv_b], "contact_ids": []},
    )
    new = await save(env, account, "SYNTH corrected", replaces_id=old)
    async with env.factory() as session:
        row = await session.get(MemoryItem, new)
    assert row is not None
    assert row.provenance.get("tainted") is True
    assert conv_b in row.provenance["refs"]["conversation_ids"]
    await purge(env, account, conv_b, OTHER_CONTACT)
    async with env.factory() as session:
        assert await session.get(MemoryItem, new) is None


# ------------------------------------------------------------ 2. S2 / invariant 4
async def test_a_tainted_note_drawn_from_another_chat_is_not_contact_facing(env: Env) -> None:
    account, conv_a, contact_a, conv_b, contact_b = await seed(env)
    await save(
        env,
        account,
        "SYNTH-LEAK about Kamola, read in Bobur's chat",
        contact_id=contact_a,
        tainted=True,
        refs={"conversation_ids": [conv_b], "contact_ids": [contact_b]},
    )
    await save(
        env,
        account,
        "SYNTH-OWN-CHAT about Kamola, read in her chat",
        contact_id=contact_a,
        tainted=True,
        refs={"conversation_ids": [conv_a], "contact_ids": [contact_a]},
    )
    await save(env, account, "SYNTH-PLAIN about Kamola", contact_id=contact_a)
    async with env.factory() as session:
        loaded = await items.contact_facing(
            session, account, conv_a, include_owner_preferences=True
        )
    contents = {i.content for i in loaded}
    assert "SYNTH-LEAK about Kamola, read in Bobur's chat" not in contents
    assert "SYNTH-OWN-CHAT about Kamola, read in her chat" in contents
    assert "SYNTH-PLAIN about Kamola" in contents


# ------------------------------------------------------------ 3. processor-checked consent
async def test_list_memories_withholds_when_consent_names_another_processor(env: Env) -> None:
    account, _, _, _, _ = await seed(env)
    await save(env, account, "SYNTH-MANAGED note", tainted=True)
    async with env.factory() as session, session.begin():
        owner = (await session.execute(select(Account))).scalar_one()
        await service.revoke_consent(
            session, owner, kind=service.CONSENT_AI_MANAGED, clock=env.clock
        )
        await service.grant_consent(
            session,
            owner,
            kind=service.CONSENT_AI_MANAGED,
            policy_version=service.POLICY_VERSION,
            processors=["Anthropic"],
            clock=env.clock,
        )
    turn = await ask(env, "What did I save?")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "list_memories",
                args(contact_id=None, conversation_id=None, scope=None, limit=None),
            )
        ),
        text_response("One item is withheld."),
    )
    outcome = await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    assert outcome.status is RunStatus.SUCCEEDED  # not a whole-run consent abort
    listed = results(fake.requests[1])[0]
    assert listed["withheld"] == 1
    assert all("SYNTH-MANAGED" not in request_texts(r) for r in fake.requests)


# ------------------------------------------------------------ 4. old rows
async def test_an_old_tainted_row_counts_as_managed(env: Env) -> None:
    account, _, _, _, _ = await seed(env)
    old = await save(env, account, "SYNTH-OLD tainted note")
    async with env.factory() as session, session.begin():
        await session.execute(
            update(MemoryItem).where(MemoryItem.id == old).values(provenance={"tainted": True})
        )
    async with env.factory() as session:
        view = await items.get(session, account, old)
        found, _ = await items.list_items(session, account, include_managed=False)
    assert view is not None
    assert view.managed is True
    assert old not in {i.memory_id for i in found}
    async with env.factory() as session, session.begin():
        await delete_managed_data(session, account_id=account, clock=env.clock)
    async with env.factory() as session:
        assert await session.get(MemoryItem, old) is None


# ------------------------------------------------------------ 5. each filter alone
async def test_each_owner_item_filter_holds_on_its_own(env: Env) -> None:
    account, conv_a, _, _, _ = await seed(env)
    await save(env, account, "SYNTH-CLEAN-PREF", kind="preference")
    managed_only = await save(env, account, "SYNTH-MANAGED-ONLY-PREF", kind="preference")
    tainted_only = await save(env, account, "SYNTH-TAINTED-ONLY-PREF", kind="preference")
    async with env.factory() as session, session.begin():
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id == managed_only)
            .values(provenance={"tainted": False, "managed": True})
        )
        await session.execute(
            update(MemoryItem)
            .where(MemoryItem.id == tainted_only)
            .values(provenance={"tainted": True, "managed": False})
        )
    async with env.factory() as session:
        loaded = await items.contact_facing(
            session, account, conv_a, include_owner_preferences=True
        )
    assert [i.content for i in loaded] == ["SYNTH-CLEAN-PREF"]
