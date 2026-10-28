"""`pause_autonomy` and `resume_autonomy` (S04.4 §3.7; ARCHITECTURE §7.1, §8.3).

RG-11 extended: a tainted owner run changes no state without the owner's
tap — except the calls that only reduce autonomy, which the kill switch
must allow. `resume_autonomy` never runs from a model call, tainted or not:
it becomes a card, and only the owner's tap resumes. Synthetic text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from assistant.accounts.models import AuditLogEntry, PendingInput
from assistant.agent.models import ToolConfirmation
from assistant.agent.tools.autonomy import NoArgs, pause_autonomy
from assistant.agent.tools.registry import (
    CapabilityTier,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.llm.port import LLMRequest, ToolResultItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import (
    ALL_FLAGS,
    AUTONOMY_FLAGS,
    applier,
    args,
    ask,
    one_id,
    runtime,
)
from tests.integration.business_env import (
    CONTACT,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    grant_managed,
)
from tests.integration.execution_env import owner_account, update_rows

pytestmark = pytest.mark.integration

FLAGS = ALL_FLAGS | AUTONOMY_FLAGS


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(i.output) for i in request.items if isinstance(i, ToolResultItem)]


async def owner_with_a_chat(env: Env) -> int:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=11, message_text="Synthetic", date=T_BASE))
    return await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )


def calls(*steps: tuple[str, str, str], tainted: int | None) -> FakeLLM:
    """A run that first reads the chat (tainting it) when `tainted` is a
    conversation id, then makes `steps`, then answers."""
    script = []
    if tainted is not None:
        read = args(conversation_id=tainted, contact_id=None, cursor=None, limit=None)
        script.append(tool_response(("read", "get_conversation", read)))
    script.append(tool_response(*steps))
    script.append(text_response("Synthetic answer"))
    return FakeLLM(*script)


async def paused_flag(env: Env) -> bool:
    return (await owner_account(env)).autonomy_paused


async def test_rg11_pause_autonomy_runs_in_a_tainted_run(env: Env) -> None:
    """The kill switch works after the run read contact text: no card, the
    account is paused at once (invariant 10 wins over the confirmation gate)."""
    conversation_id = await owner_with_a_chat(env)
    turn = await ask(env, "Read the chat, then stop everything")
    fake = calls(("p1", "pause_autonomy", args()), tainted=conversation_id)

    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)

    assert await paused_flag(env)
    outcome = results(fake.requests[-1])[-1]
    assert outcome["status"] == "ok"
    assert outcome["autonomy"] == "paused"
    async with env.factory() as session:
        held = await session.scalar(select(func.count()).select_from(ToolConfirmation))
    assert held == 0


@pytest.mark.parametrize("tainted", [True, False])
async def test_rg11_resume_autonomy_from_a_model_call_only_offers_a_card(
    env: Env, tainted: bool
) -> None:
    conversation_id = await owner_with_a_chat(env)
    account = await owner_account(env)
    await update_rows(env, type(account), type(account).id == account.id, autonomy_paused=True)
    turn = await ask(env, "Resume replies")
    fake = calls(("r1", "resume_autonomy", args()), tainted=conversation_id if tainted else None)

    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)

    assert await paused_flag(env)  # nothing resumed
    assert results(fake.requests[-1])[-1]["status"] == "awaiting_confirmation"
    async with env.factory() as session:
        row = (await session.execute(select(ToolConfirmation))).scalar_one()
    assert (row.tool_name, row.status) == ("resume_autonomy", "pending")


async def test_a_confirmed_resume_card_resumes(env: Env) -> None:
    await owner_with_a_chat(env)
    account = await owner_account(env)
    await update_rows(env, type(account), type(account).id == account.id, autonomy_paused=True)
    turn = await ask(env, "Resume replies")
    channel = FakeChannel()
    await runtime(
        env, calls(("r1", "resume_autonomy", args()), tainted=None), channel, flags=FLAGS
    ).run(turn)
    async with env.factory() as session:
        confirmation = (await session.execute(select(ToolConfirmation))).scalar_one()
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm"))
        ).scalar_one()
    await deliver(
        env, callback(f"tool_confirm:{card.token}:confirm", message_id=card.prompt_message_id)
    )

    outcome = await applier(env, channel, flags=FLAGS).apply(confirmation.id)

    assert outcome.status == "applied"
    assert not await paused_flag(env)
    assert "Done" in channel.sends[-1]
    async with env.factory() as session:
        audit = await session.scalar(
            select(AuditLogEntry).where(AuditLogEntry.event == "autonomy_resumed")
        )
    assert audit is not None
    assert audit.details["source"] == "card"


async def test_autonomy_tools_are_hidden_while_their_flags_are_off() -> None:
    registry = default_registry()
    both = {"pause_autonomy", "resume_autonomy"}
    cases = (
        (ALL_FLAGS - AUTONOMY_FLAGS, set()),
        (ALL_FLAGS - {"tool.resume_autonomy"}, {"pause_autonomy"}),
        (FLAGS, both),
    )
    for flags, expected in cases:
        offered = registry.for_run(
            "owner_request",
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=flags,
            managed_consent=True,
        )
        assert both & set(offered) == expected


async def test_pause_autonomy_is_idempotent_on_its_call_id(env: Env) -> None:
    """A retried call (same run, same call id) returns its recorded result
    and writes nothing more (ARCHITECTURE §8.3 step 4)."""
    await owner_with_a_chat(env)
    account = await owner_account(env)
    context = ToolContext(
        account_id=account.id,
        run_id=77,
        run_type="owner_request",
        tainted=True,
        services=ToolServices(
            session_factory=env.factory, clock=env.clock, monthly_budget_usd=None
        ),
        call_id="call-1",
    )

    first = await pause_autonomy(context, NoArgs())
    again = await pause_autonomy(context, NoArgs())
    other_call = await pause_autonomy(
        ToolContext(**{**_fields(context), "call_id": "call-2"}), NoArgs()
    )

    assert first.status == again.status == other_call.status == "ok"
    assert first.data["changed"] is True
    assert again.data["changed"] is True  # the recorded result, not a new one
    assert other_call.data["changed"] is False  # already paused: nothing to change
    async with env.factory() as session:
        rows = await session.scalar(
            select(func.count())
            .select_from(AuditLogEntry)
            .where(AuditLogEntry.event == "autonomy_paused")
        )
    assert rows == 1
    assert await paused_flag(env)


def _fields(context: ToolContext) -> dict[str, Any]:
    return {name: getattr(context, name) for name in context.__dataclass_fields__}
