"""Settings changed outside the model are visible in the next owner run."""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts import preferences
from assistant.accounts.models import Account
from assistant.agent.models import AgentRun, AgentRunStep
from assistant.agent.tools.preferences import GetPreferencesArgs, get_preferences
from assistant.agent.tools.registry import ToolContext, ToolServices
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest, ToolResultItem
from assistant.privacy import retention_policy
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import ask, runtime
from tests.integration.business_env import OWNER, Env, connect, environment

pytestmark = pytest.mark.integration
FLAGS = frozenset({"tool.get_preferences"})


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(item.output) for item in request.items if isinstance(item, ToolResultItem)]


async def test_owner_button_preferences_are_reported_and_steps_are_content_free(env: Env) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (
            await uow.session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()
        await preferences.update(
            uow.session,
            account,
            language="uz",
            timezone="Asia/Tashkent",
            new_default_mode="ignore",
            reply_language="ru",
            reply_tone="friendly",
            reply_style="detailed",
            retention={retention_policy.MESSAGES: 30},
            quiet_hours="off",
            urgent_bypass=True,
            digest_time="07:30",
            alert_cap_per_hour=12,
            actor="owner",
            clock=env.clock,
        )
        expected = preferences.current(account)
        account_id = account.id
    turn = await ask(env, "Show my current settings")
    fake = FakeLLM(
        tool_response(("p1", "get_preferences", "{}")),
        tool_response(("p1", "get_preferences", "{}")),
        text_response("Settings shown."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    first = results(fake.requests[1])[0]
    assert first == results(fake.requests[2])[-1]  # idempotent call id
    assert first["status"] == "ok"
    settings = first["preferences"]
    assert {field: item["value"] for field, item in settings.items()} == expected
    for field, item in settings.items():
        assert item["change_via"] == ["chat", "/settings"]
        assert item["is_default"] is (
            field in {"control_chat_retention_days", "raw_update_retention_days"}
        )
    async with env.factory() as session:
        step_rows = await session.execute(
            select(AgentRunStep)
            .join(AgentRun, AgentRun.id == AgentRunStep.run_id)
            .where(AgentRun.account_id == account_id)
        )
        steps = step_rows.scalars().all()
        runs = (
            (await session.execute(select(AgentRun).where(AgentRun.account_id == account_id)))
            .scalars()
            .all()
        )
    assert [step.tool_name for step in steps] == ["get_preferences"]
    assert steps[0].status == "ok"
    assert steps[0].args_digest
    assert all(not run.tainted for run in runs)


async def test_get_preferences_uses_only_the_context_account_and_effective_defaults(
    env: Env,
) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        owner = (
            await uow.session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()
        await preferences.update(
            uow.session,
            owner,
            timezone="Asia/Tashkent",
            digest_time="09:00",
            actor="owner",
            clock=env.clock,
        )
        other = Account(
            telegram_user_id=991122,
            status="active",
            timezone="UTC",
            settings={"digest_time": "invalid", "unrelated": "SYNTH-CANARY"},
        )
        uow.session.add(other)
        await uow.session.flush()
        other_id = other.id
    context = ToolContext(
        account_id=other_id,
        run_id=0,
        run_type="owner_request",
        tainted=True,
        services=ToolServices(
            session_factory=env.factory, clock=env.clock, monthly_budget_usd=None
        ),
    )
    outcome = await get_preferences(context, GetPreferencesArgs())
    assert outcome.status == "ok"
    settings = outcome.data["preferences"]
    assert settings["preferred_language"]["value"] is None
    assert settings["timezone"]["value"] == "UTC"
    assert settings["digest_time"]["value"] == "off"
    assert all(item["is_default"] for item in settings.values())
    assert "SYNTH-CANARY" not in str(outcome.data)
    assert not outcome.contains_managed
    assert not outcome.taints
    missing = await get_preferences(replace(context, account_id=0), GetPreferencesArgs())
    assert missing.status == "error"
    assert "preferences" not in missing.data


async def test_flag_off_does_not_offer_get_preferences(env: Env) -> None:
    await connect(env)
    turn = await ask(env, "Show settings")
    fake = FakeLLM(text_response("Use /settings."))
    await runtime(env, fake, FakeChannel(), flags=frozenset()).run(turn)
    assert all(tool.name != "get_preferences" for tool in fake.requests[0].tools)
