"""S04.2 draft runs on real Postgres, with synthetic conversations only."""

import asyncio
from decimal import Decimal
from typing import Any, NoReturn

import pytest
from sqlalchemy import select

from assistant.accounts.models import Consent
from assistant.agent.drafting import DraftConfig, Drafter, DraftStatus
from assistant.agent.models import AgentRun
from assistant.conversations.models import Conversation
from assistant.core.errors import NotInAccountError
from assistant.llm.port import LLMError, LLMErrorKind, LLMRequest, StopReason, TextItem
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import gated
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    Env,
    business_message,
    connect,
    deliver,
    grant_managed,
)
from tests.integration.test_memory_isolation import (
    CHAT_NOTE_B,
    NOTE_A,
    NOTE_B,
    OWNER_FACT,
    OWNER_PREF,
    OWNER_PRIVATE_RULE,
    OWNER_REPLY_RULE,
)
from tests.integration.test_memory_isolation import (
    seed as seed_memory,
)
from tests.unit.test_draft_units import output

pytestmark = pytest.mark.integration


async def seed(env: Env, managed: bool = True) -> tuple[int, int]:
    await connect(env)
    if managed:
        await grant_managed(env)
    await deliver(env, business_message(message_text="SYNTHETIC-CHAT-A", message_id=11))
    await deliver(
        env,
        business_message(
            from_id=OTHER_CONTACT,
            chat_id=OTHER_CONTACT,
            message_text="SYNTHETIC-CHAT-B",
            message_id=12,
        ),
    )
    async with env.factory() as session:
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.business_chat_id == CONTACT)
            )
        ).scalar_one()
        return conversation.account_id, conversation.id


def drafter(env: Env, fake: FakeLLM, budget: Decimal | None = None) -> Drafter:
    return Drafter(
        session_factory=env.factory,
        clock=env.clock,
        llm=gated(env, fake),
        config=DraftConfig(model="gpt-6-luna", monthly_budget_usd=budget),
    )


async def runs(env: Env, account_id: int) -> list[AgentRun]:
    async with env.factory() as session:
        return list(
            (
                await session.scalars(
                    select(AgentRun).where(
                        AgentRun.account_id == account_id, AgentRun.run_type == "draft_reply"
                    )
                )
            ).all()
        )


async def test_rg09_draft_prompt_never_contains_another_conversation(env: Env) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()
    fake.parsed.append(output())
    outcome = await drafter(env, fake).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    assert outcome.status is DraftStatus.DRAFTED
    text = fake.requests[0].instructions + "\n".join(
        item.text for item in fake.requests[0].items if isinstance(item, TextItem)
    )
    assert "SYNTHETIC-CHAT-A" in text
    assert "SYNTHETIC-CHAT-B" not in text
    assert outcome.base_message_id is not None
    assert outcome.shown_through_message_id is not None


async def test_rg17_draft_reply_runs_are_given_no_tools(env: Env) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()
    fake.parsed.append(output())
    await drafter(env, fake).draft(
        account_id=account_id,
        conversation_id=conversation_id,
        reason="owner_request",
        owner_run_id=10,
    )
    assert len(fake.requests) == 1
    assert fake.requests[0].tools == ()
    (row,) = await runs(env, account_id)
    assert row.prompt_version == "draft_reply.v1"
    assert row.trigger_ref == f"draft:run:10:conv:{conversation_id}"


async def test_rg08_without_managed_consent_naming_the_processor_no_draft_call_is_made(
    env: Env,
) -> None:
    account_id, conversation_id = await seed(env, managed=False)
    fake = FakeLLM()
    result = await drafter(env, fake).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    assert result.status is DraftStatus.NO_CONSENT
    assert not fake.requests
    assert not await runs(env, account_id)


async def test_over_budget_no_draft_call_is_made(env: Env) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()
    result = await drafter(env, fake, Decimal("0")).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    assert result.status is DraftStatus.OVER_BUDGET
    assert not fake.requests
    assert not await runs(env, account_id)


@pytest.mark.parametrize(
    ("script", "status", "calls"),
    [
        ([StopReason.REFUSAL], DraftStatus.REFUSED, 1),
        ([StopReason.CONTENT_FILTER], DraftStatus.REFUSED, 1),
        ([StopReason.MAX_TOKENS, StopReason.MAX_TOKENS], DraftStatus.FAILED, 2),
        ([output(text="")], DraftStatus.INVALID, 1),
        ([output(needs_reply="yes")], DraftStatus.INVALID, 1),
        ([LLMError(LLMErrorKind.UNAVAILABLE)], DraftStatus.FAILED, 1),
    ],
)
async def test_refused_invalid_or_twice_truncated_output_proposes_nothing(
    env: Env, script: list[Any], status: DraftStatus, calls: int
) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()
    fake.parsed.extend(script)
    result = await drafter(env, fake).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    assert result.status is status
    assert result.result is None
    assert len(fake.requests) == calls
    (row,) = await runs(env, account_id)
    assert row.finished_at is not None
    assert row.status != "running"
    if calls == 2:
        first, second = fake.requests
        assert first.items == second.items
        assert second.max_output_tokens == first.max_output_tokens * 2


@pytest.mark.parametrize("exception", [RuntimeError("SYNTHETIC"), asyncio.CancelledError()])
async def test_the_draft_run_row_is_closed_on_every_path(
    env: Env, exception: BaseException
) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()

    def fail(request: LLMRequest) -> NoReturn:
        raise exception

    fake.parsed.append(fail)
    with pytest.raises(type(exception)):
        await drafter(env, fake).draft(
            account_id=account_id, conversation_id=conversation_id, reason="eval"
        )
    (row,) = await runs(env, account_id)
    assert row.status == "failed"
    assert row.finished_at is not None
    assert row.error_class == f"exception:{type(exception).__name__}"


async def test_the_drafter_refuses_guidance_with_a_tainted_run(env: Env) -> None:
    fake = FakeLLM()
    with pytest.raises(ValueError, match="tainted"):
        await drafter(env, fake).draft(
            account_id=1,
            conversation_id=1,
            reason="owner_request",
            guidance="SYNTHETIC-OTHER-CHAT",
            run_tainted=True,
        )
    assert not fake.requests


async def test_a_foreign_conversation_is_not_found_and_creates_no_run(env: Env) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()
    with pytest.raises(NotInAccountError):
        await drafter(env, fake).draft(
            account_id=account_id + 1000, conversation_id=conversation_id, reason="eval"
        )
    assert not fake.requests
    assert not await runs(env, account_id)


async def test_a_consent_revoked_before_call_stops_the_run(env: Env) -> None:
    account_id, conversation_id = await seed(env)
    async with env.factory() as session, session.begin():
        consent = (
            await session.scalars(
                select(Consent).where(
                    Consent.account_id == account_id,
                    Consent.kind == "ai_managed",
                    Consent.revoked_at.is_(None),
                )
            )
        ).one()
        consent.processors = ["UnknownProcessor"]
    fake = FakeLLM()
    result = await drafter(env, fake).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    assert result.status is DraftStatus.NO_CONSENT
    assert not fake.requests


async def test_rg09_owner_scope_facts_and_other_contacts_notes_never_reach_a_draft(
    env: Env,
) -> None:

    account_id, conversation_id, _ = await seed_memory(env)
    fake = FakeLLM()
    fake.parsed.append(output())
    await drafter(env, fake).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    seen = fake.requests[0].instructions + "\n".join(
        item.text for item in fake.requests[0].items if isinstance(item, TextItem)
    )
    for allowed in (NOTE_A, OWNER_PREF, OWNER_REPLY_RULE):
        assert allowed in seen
    for forbidden in (NOTE_B, CHAT_NOTE_B, OWNER_FACT, OWNER_PRIVATE_RULE):
        assert forbidden not in seen


async def test_no_reply_needed_returns_no_text(env: Env) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()
    fake.parsed.append(output(needs_reply=False, skip_reason="no_reply_needed"))
    result = await drafter(env, fake).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    assert result.status is DraftStatus.NOT_NEEDED
    assert result.result is not None
    assert result.result.text == ""


async def test_truncation_retries_once_and_returns_the_validated_reply(env: Env) -> None:
    account_id, conversation_id = await seed(env)
    fake = FakeLLM()
    fake.parsed.extend([StopReason.MAX_TOKENS, output(text="  Synthetic\r\nreply.  ")])
    result = await drafter(env, fake).draft(
        account_id=account_id, conversation_id=conversation_id, reason="eval"
    )
    assert result.status is DraftStatus.DRAFTED
    assert result.result is not None
    assert result.result.text == "Synthetic\nreply."
    (row,) = await runs(env, account_id)
    assert row.status == "succeeded"
    assert row.iterations == 2
