"""S03.5 deletion tools through the real agent loop (FR-PRV-04; ARCHITECTURE §8.2).

The required S03.5 tests (IMPLEMENTATION_PLAN §5), each findable by name:

- confirmation is required, also in an untainted run —
  `test_a_deletion_call_in_an_untainted_run_only_shows_a_card`,
  `test_the_deletion_applies_only_after_the_owner_confirms`,
  `test_a_deletion_handler_refuses_to_run_without_a_confirmation`;
- AGENTS §8 tool tests: semantically invalid arguments (unit:
  `tests/unit/test_deletion_args.py`), foreign-account ids —
  `test_another_accounts_ids_get_no_card_and_delete_nothing`; registry
  filtering — `test_the_registry_offers_deletion_tools_only_with_flags_to_owner_requests`;
  idempotent retry — `test_a_repeated_deletion_call_holds_one_card_and_deletes_once`;
  content-free steps — `test_deletion_steps_and_results_are_content_free`.

Synthetic text only; FakeLLM stands in for the provider (AGENTS §11).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts.models import Account, AuditLogEntry, PendingInput
from assistant.agent.models import AgentRunStep, ToolConfirmation
from assistant.agent.tools.deletion import (
    DeleteContactDataArgs,
    DeleteConversationDataArgs,
    DeleteEverythingArgs,
    delete_contact_data_tool,
    delete_conversation_data_tool,
    delete_everything_tool,
)
from assistant.agent.tools.registry import (
    CapabilityClass,
    CapabilityTier,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.llm.port import LLMRequest, ToolResultItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import (
    DELETION_FLAGS,
    READ_FLAGS,
    applier,
    args,
    ask,
    one_id,
    runtime,
)
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
)

pytestmark = pytest.mark.integration

FLAGS = READ_FLAGS | DELETION_FLAGS
DELETION_TOOLS = {"delete_conversation_data", "delete_contact_data", "delete_everything"}
CANARY = "SYNTH-DEL-CANARY"


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def seed(env: Env) -> tuple[int, int]:
    """A connected owner and one chat; returns (conversation id, contact id)."""
    await connect(env)
    await deliver(
        env, business_message(message_id=1, message_text=f"{CANARY} one", first_name="Kamola")
    )
    conversation = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    contact = await one_id(
        env, "SELECT id FROM app.contacts WHERE telegram_user_id = :u", u=CONTACT
    )
    return conversation, contact


async def message_count(env: Env) -> int:
    return await one_id(env, "SELECT count(*) FROM app.messages")


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(item.output) for item in request.items if isinstance(item, ToolResultItem)]


async def ctx(
    env: Env, *, account_id: int | None = None, run_id: int = 0, call_id: str = "", confirmed: bool
) -> ToolContext:
    if account_id is None:
        account_id = await one_id(
            env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER
        )
    return ToolContext(
        account_id=account_id,
        run_id=run_id,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory, clock=env.clock, monthly_budget_usd=None
        ),
        call_id=call_id,
        confirmed=confirmed,
    )


# ------------------------------------------------------------ always a card
@pytest.mark.parametrize(
    ("tool", "make_args"),
    [
        (
            "delete_conversation_data",
            lambda conv, contact: args(conversation_id=conv, contact_id=None),
        ),
        ("delete_contact_data", lambda conv, contact: args(contact_id=contact)),
        ("delete_everything", lambda conv, contact: args(scope="everything")),
    ],
)
async def test_a_deletion_call_in_an_untainted_run_only_shows_a_card(
    env: Env, tool: str, make_args: Any
) -> None:
    conversation, contact = await seed(env)
    turn = await ask(env, "Delete it")
    fake = FakeLLM(
        tool_response(("c1", tool, make_args(conversation, contact))),
        text_response("Please confirm on the card."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=FLAGS).run(turn)

    [result] = results(fake.requests[1])
    assert result["status"] == "awaiting_confirmation"
    assert await message_count(env) == 1  # nothing deleted
    async with env.factory() as session:
        run_tainted = (
            await session.execute(select(ToolConfirmation.status, ToolConfirmation.tool_name))
        ).one()
    assert tuple(run_tainted) == ("pending", tool)
    card = channel.sends[0]
    assert "Confirm a change?" in card
    assert "cannot be undone" in card
    assert [b.label for b in channel.buttons[0]] == ["Confirm", "Reject"]
    if tool != "delete_everything":
        assert "Kamola" in card  # the card names whose data goes


async def test_the_deletion_applies_only_after_the_owner_confirms(env: Env) -> None:
    conversation, _ = await seed(env)
    turn = await ask(env, "Delete Kamola's chat data")
    fake = FakeLLM(
        tool_response(
            ("c1", "delete_conversation_data", args(conversation_id=conversation, contact_id=None))
        ),
        text_response("Please confirm."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    async with env.factory() as session:
        row = (await session.execute(select(ToolConfirmation))).scalar_one()
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm"))
        ).scalar_one()
    await deliver(env, callback(f"tool_confirm:{card.token}:confirm"))
    applied_channel = FakeChannel()
    outcome = await applier(env, applied_channel, flags=FLAGS).apply(row.id)
    assert outcome.status == "applied"
    assert await message_count(env) == 0
    assert "Done" in applied_channel.sends[-1]
    async with env.factory() as session:
        events = (
            (
                await session.execute(
                    select(AuditLogEntry.event).where(AuditLogEntry.event == "data_deleted")
                )
            )
            .scalars()
            .all()
        )
    assert events == ["data_deleted"]


async def test_a_rejected_deletion_deletes_nothing(env: Env) -> None:
    await seed(env)
    turn = await ask(env, "Delete everything")
    fake = FakeLLM(
        tool_response(("c1", "delete_everything", args(scope="everything"))),
        text_response("Please confirm."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    async with env.factory() as session:
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm"))
        ).scalar_one()
    await deliver(env, callback(f"tool_confirm:{card.token}:reject"))
    assert await message_count(env) == 1


async def test_a_deletion_handler_refuses_to_run_without_a_confirmation(env: Env) -> None:
    """Defence in depth behind the gate: a handler reached without the
    owner's tap (a misconfigured gate) deletes nothing."""
    conversation, contact = await seed(env)
    context = await ctx(env, confirmed=False)
    for call in (
        delete_conversation_data_tool(
            context, DeleteConversationDataArgs(conversation_id=conversation, contact_id=None)
        ),
        delete_contact_data_tool(context, DeleteContactDataArgs(contact_id=contact)),
        delete_everything_tool(context, DeleteEverythingArgs(scope="everything")),
    ):
        outcome = await call
        assert outcome.status == "not_executed"
    assert await message_count(env) == 1


# ------------------------------------------------------------ AGENTS §8
async def test_another_accounts_ids_get_no_card_and_delete_nothing(env: Env) -> None:
    conversation, contact = await seed(env)
    async with env.factory() as session, session.begin():
        other = Account(telegram_user_id=7000000099, timezone="UTC", control_chat_id=7000000099)
        session.add(other)
        await session.flush()
        other_id = other.id
    # Through the loop (the other account asks): "not found", no card.
    from assistant.accounts.models import Consent, ControlTurn

    async with env.factory() as session, session.begin():
        session.add(
            Consent(
                account_id=other_id, kind="ai_control", policy_version="v4", processors=["OpenAI"]
            )
        )
        turn = ControlTurn(account_id=other_id, role="owner", text="Delete that chat")
        session.add(turn)
        await session.flush()
        turn_id = turn.id
    fake = FakeLLM(
        tool_response(
            ("c1", "delete_conversation_data", args(conversation_id=conversation, contact_id=None)),
            ("c2", "delete_contact_data", args(contact_id=contact)),
        ),
        text_response("Not found."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=FLAGS).run(turn_id)
    assert [r["status"] for r in results(fake.requests[1])] == ["error", "error"]
    assert all(r["reason"] == "not found" for r in results(fake.requests[1]))
    async with env.factory() as session:
        assert (await session.execute(select(ToolConfirmation))).first() is None
    # Even confirmed, a foreign id is not found.
    theirs = await ctx(env, account_id=other_id, confirmed=True)
    with pytest.raises(LookupError):
        await delete_conversation_data_tool(
            theirs, DeleteConversationDataArgs(conversation_id=conversation, contact_id=None)
        )
    with pytest.raises(LookupError):
        await delete_contact_data_tool(theirs, DeleteContactDataArgs(contact_id=contact))
    assert await message_count(env) == 1


async def test_a_repeated_deletion_call_holds_one_card_and_deletes_once(env: Env) -> None:
    conversation, _ = await seed(env)
    turn = await ask(env, "Delete twice")
    call = ("c1", "delete_conversation_data", args(conversation_id=conversation, contact_id=None))
    fake = FakeLLM(tool_response(call), tool_response(call), text_response("Confirm please."))
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=FLAGS).run(turn)
    first, again = results(fake.requests[1])[0], results(fake.requests[2])[-1]
    assert first == again
    assert len([s for s in channel.sends if "Confirm a change?" in s]) == 1
    # A confirmed handler run twice with the same (run, call): one deletion.
    run_id = await one_id(env, "SELECT max(id) FROM app.agent_runs")
    context = await ctx(env, run_id=run_id, call_id="c1", confirmed=True)
    target = DeleteConversationDataArgs(conversation_id=conversation, contact_id=None)
    done = await delete_conversation_data_tool(context, target)
    await deliver(env, business_message(message_id=2, message_text="SYNTH after"))
    replay = await delete_conversation_data_tool(context, target)
    assert done.status == "ok"
    assert replay.status == "ok"
    assert replay.data.get("repeated") is True
    assert await message_count(env) == 1  # the later message was not deleted by the replay
    async with env.factory() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.event == "data_deleted")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_deletion_steps_and_results_are_content_free(env: Env) -> None:
    conversation, _ = await seed(env)
    context = await ctx(env, confirmed=True)
    outcome = await delete_conversation_data_tool(
        context, DeleteConversationDataArgs(conversation_id=conversation, contact_id=None)
    )
    assert CANARY not in json.dumps(outcome.data)
    assert "Kamola" not in json.dumps(outcome.data)  # names are managed content
    assert outcome.contains_managed is False
    turn = await ask(env, "Delete everything")
    fake = FakeLLM(
        tool_response(("c1", "delete_everything", args(scope="everything"))),
        text_response("Confirm."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    async with env.factory() as session:
        steps = (await session.execute(select(AgentRunStep))).scalars().all()
    assert [s.tool_name for s in steps] == ["delete_everything"]


async def test_the_registry_offers_deletion_tools_only_with_flags_to_owner_requests() -> None:
    registry = default_registry()
    by_name = {t.name: t for t in registry.all()}
    assert set(by_name) >= DELETION_TOOLS
    for name in DELETION_TOOLS:
        tool = by_name[name]
        assert tool.capability is CapabilityClass.CONFIGURE
        assert tool.release_flag == f"tool.{name}"
        assert tool.allowed_run_types == frozenset({"owner_request"})
        assert tool.min_tier is CapabilityTier.CONTROL_ONLY
        assert tool.returns_managed is False
        # Always a card, tainted or not.
        assert tool.needs_confirmation(tainted=False)
        assert tool.needs_confirmation(tainted=True)
    offered = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=DELETION_FLAGS,
        managed_consent=False,
    )
    assert set(offered) == DELETION_TOOLS
    assert not (
        set(
            registry.for_run(
                "owner_request",
                tier=CapabilityTier.CONTROL_ONLY,
                enabled_flags=frozenset(),
                managed_consent=True,
            )
        )
        & DELETION_TOOLS
    )
    for run_type in ("summarize", "triage", "draft_reply"):
        assert (
            registry.for_run(
                run_type,
                tier=CapabilityTier.CONTROL_ONLY,
                enabled_flags=DELETION_FLAGS,
                managed_consent=True,
            )
            == {}
        )
