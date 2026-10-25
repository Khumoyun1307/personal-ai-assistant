"""S03.2 review fixes (2026-10-25): each finding of the general and safety
reviews, pinned by a test that failed before its fix.

1. Nothing deleted is stored: a deletion, managed-data deletion or a
   bot-chat purge committed while a summary call is in flight leaves no
   summary row and no deleted text (§6 invariant 9, D63, PRD §20).
2. An edit of a covered message rebuilds the rolling summary (D66).
3. A redelivered deletion keeps the rolling summary.
4. The catch-up's coverage start is the earliest one, not the latest
   reconnect; a connection that is not active gets a caveat.
5. The attention list makes at most one summary call per stale chat (D62).
6. An unexpected error closes the run as failed; one chat's failure does
   not abort a catch-up.
7. Content arriving during a call restarts the maximum wait from it.
Safety: a cached period summary needs consent too; a scoped catch-up of an
Ignore-mode chat says so. Nits: slightly future messages are included in an
open-ended catch-up.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.agent.models import AgentRun
from assistant.agent.summarize import SummaryStatus
from assistant.agent.tools.registry import ToolContext, ToolOutcome, ToolServices
from assistant.agent.tools.summaries import (
    GetAttentionListArgs,
    SummarizePeriodArgs,
    get_attention_list_tool,
    summarize_period_tool,
)
from assistant.conversations import service
from assistant.conversations.models import Conversation
from assistant.llm.port import LLMRequest
from assistant.memory.models import ConversationSummary
from assistant.privacy.deletion import delete_managed_data
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import summarizer
from tests.integration.business_env import (
    CONTACT,
    NOW,
    OTHER_CONTACT,
    Env,
    business_message,
    deleted_messages,
    deliver,
    environment,
)
from tests.integration.test_summaries import (
    TEXT_A,
    TEXT_A2,
    account_id,
    conversation_id,
    echo,
    everything,
    seed,
    shown_ids,
    summary_requests,
    summary_rows,
    ts,
)

pytestmark = pytest.mark.integration

_UNTRUSTED = re.compile(
    r'<untrusted source="(?:contact|owner_business_chat)" trust="untrusted">(.*?)</untrusted>'
)


@pytest.fixture
async def senv(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url, summaries=True) as built:
        yield built


def with_texts(request: LLMRequest) -> dict[str, Any]:
    """A summary whose overview repeats every message text it was shown —
    so a test can see exactly which texts could end up stored."""
    ids = shown_ids(request)
    texts = _UNTRUSTED.findall(everything(request))
    return {
        "overview": " | ".join(texts),
        "open_questions_to_owner": [],
        "commitments": [],
        "decisions_requested": [],
        "key_facts": [{"text": t, "message_ids": ids} for t in texts[:1]],
        "needs_reply": True,
        "needs_reply_reason": None,
        "language": "uz",
    }


def during(
    action: Callable[[], Awaitable[None]],
    then: Callable[[LLMRequest], dict[str, Any]] = with_texts,
) -> Callable[[LLMRequest], Awaitable[dict[str, Any]]]:
    """A FakeLLM answer that first commits `action` while the call is in flight."""

    async def hook(request: LLMRequest) -> dict[str, Any]:
        await action()
        return then(request)

    return hook


async def stored_texts(env: Env) -> str:
    async with env.factory() as session:
        rows = await session.execute(text("SELECT summary::text FROM app.conversation_summaries"))
        return "\n".join(str(r[0]) for r in rows.all())


async def summary_kinds(env: Env) -> list[tuple[int, str]]:
    async with env.factory() as session:
        rows = await session.execute(
            select(ConversationSummary.conversation_id, ConversationSummary.kind).order_by(
                ConversationSummary.id
            )
        )
        return [(int(c), str(k)) for c, k in rows.all()]


async def pending_since(env: Env, conversation: int) -> Any:
    async with env.factory() as session:
        return (
            await session.execute(
                select(Conversation.pending_ai_since).where(Conversation.id == conversation)
            )
        ).scalar_one()


async def run_statuses(env: Env) -> list[tuple[str, str | None]]:
    async with env.factory() as session:
        rows = await session.execute(
            select(AgentRun.status, AgentRun.error_class)
            .where(AgentRun.run_type == "summarize")
            .order_by(AgentRun.id)
        )
        return [(str(s), e) for s, e in rows.all()]


def delete(
    env: Env, telegram_message_id: int, chat: int = CONTACT
) -> Callable[[], Awaitable[None]]:
    async def action() -> None:
        await deliver(env, deleted_messages(chat_id=chat, message_ids=[telegram_message_id]))

    return action


async def tool_ctx(env: Env, fake: FakeLLM, **config: Any) -> ToolContext:
    return ToolContext(
        account_id=await account_id(env),
        run_id=0,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory,
            clock=env.clock,
            monthly_budget_usd=None,
            summarizer=summarizer(env, fake, **config),
        ),
    )


async def catch_up(env: Env, fake: FakeLLM, **values: Any) -> ToolOutcome:
    arguments = {
        "since": "2026-10-18 08:30",
        "until": None,
        "contact_id": None,
        "conversation_id": None,
    }
    arguments.update(values)
    return await summarize_period_tool(
        await tool_ctx(env, fake), SummarizePeriodArgs.model_validate(arguments)
    )


# ============================================================ 1. nothing deleted is stored
async def test_a_deletion_during_a_first_build_stores_nothing(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [during(delete(senv, 12))]
    summaries = summarizer(senv, fake)

    outcome = await summaries.refresh(a)

    assert outcome.status is SummaryStatus.DISCARDED
    assert await summary_kinds(senv) == []
    assert TEXT_A2 not in await stored_texts(senv)
    assert await pending_since(senv, a) is not None
    assert ("failed", "discarded:changed") in await run_statuses(senv)
    # The next refresh rebuilds from what is retained — without the deleted text.
    fake.parsed.append(with_texts)
    assert (await summaries.refresh(a)).status is SummaryStatus.REFRESHED
    stored = await stored_texts(senv)
    assert TEXT_A in stored
    assert TEXT_A2 not in stored


async def test_a_deletion_of_a_new_message_during_an_incremental_refresh_stores_nothing(
    senv: Env,
) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [with_texts]
    summaries = summarizer(senv, fake)
    await summaries.refresh(a)
    senv.clock.advance(minutes=5)
    await deliver(
        senv, business_message(message_id=14, message_text="SYNTH-A NEW-DELETED", date=ts(40))
    )
    fake.parsed.append(during(delete(senv, 14)))

    assert (await summaries.refresh(a)).status is SummaryStatus.DISCARDED

    assert "NEW-DELETED" not in await stored_texts(senv)
    assert await summary_kinds(senv) == [(a, "rolling")]  # the earlier one is kept


CHANGED_NOTICE = "changed while it was being summarized"


async def test_a_deletion_during_both_period_summary_calls_is_reported_not_summarized(
    senv: Env,
) -> None:
    """Owner addition to item 1: one immediate retry; if that is discarded too,
    the chat is NOT summarized (`changed_during_summary`) with our own notice
    — never an empty or complete summary (invariant 11), never the content."""
    a, b = await seed(senv)
    fake = FakeLLM()

    def per_chat(request: LLMRequest) -> Any:
        if "SYNTH-B" in everything(request):
            return with_texts(request)
        hook = deletions.pop(0)
        return hook(request)

    deletions = [during(delete(senv, 12)), during(delete(senv, 11))]
    fake.parsed = [per_chat, per_chat, per_chat]

    result = await catch_up(senv, fake)

    assert [(r.conversation_id, r.kind) for r in await summary_rows(senv)] == [(b, "period")]
    assert [e["conversation_id"] for e in result.data["conversations"]] == [b]  # unaffected
    skipped = result.data["not_summarized"]
    assert [(n["conversation_id"], n["reason"]) for n in skipped] == [(a, "changed_during_summary")]
    notices = result.data["notices"]
    assert len(notices) == 1
    assert CHANGED_NOTICE in notices[0]
    assert "deleted or edited" in notices[0]
    assert "ask again" in notices[0].lower()
    dumped = json.dumps(result.data)
    assert TEXT_A not in dumped
    assert TEXT_A2 not in dumped
    assert len(summary_requests(fake)) == 3  # A twice (one retry), B once


async def test_a_deletion_during_the_first_period_call_is_retried_once_without_notice(
    senv: Env,
) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [during(delete(senv, 12)), with_texts]

    result = await catch_up(senv, fake, conversation_id=a)

    [entry] = result.data["conversations"]
    assert entry["conversation_id"] == a
    dumped = json.dumps(result.data)
    assert TEXT_A2 not in dumped
    assert TEXT_A in dumped
    assert "notices" not in result.data
    assert TEXT_A2 not in await stored_texts(senv)
    assert len(summary_requests(fake)) == 2


async def test_managed_data_deletion_during_a_refresh_leaves_no_summary(senv: Env) -> None:
    a, _ = await seed(senv)
    owner = await account_id(senv)

    async def wipe() -> None:
        async with senv.factory() as session, session.begin():
            await delete_managed_data(session, account_id=owner, clock=senv.clock)

    fake = FakeLLM()
    fake.parsed = [during(wipe)]

    await summarizer(senv, fake).refresh(a)

    assert await summary_kinds(senv) == []
    assert "SYNTH-A" not in await stored_texts(senv)


async def test_a_managed_data_deletion_during_a_period_summary_leaves_nothing(
    senv: Env,
) -> None:
    a, _ = await seed(senv)
    owner = await account_id(senv)

    async def wipe() -> None:
        async with senv.factory() as session, session.begin():
            await delete_managed_data(session, account_id=owner, clock=senv.clock)

    fake = FakeLLM()
    fake.parsed = [during(wipe)]

    result = await catch_up(senv, fake, conversation_id=a)

    assert await summary_kinds(senv) == []
    assert "SYNTH-A" not in json.dumps(result.data)


async def test_a_bot_chat_purge_during_a_refresh_leaves_no_summary(senv: Env) -> None:
    a, _ = await seed(senv)
    owner = await account_id(senv)

    async def purge() -> None:
        async with senv.factory() as session, session.begin():
            await service.mark_bot_chat(
                session, account_id=owner, business_chat_id=CONTACT, clock=senv.clock
            )

    fake = FakeLLM()
    fake.parsed = [during(purge)]

    await summarizer(senv, fake).refresh(a)

    assert await summary_kinds(senv) == []
    assert "SYNTH-A" not in await stored_texts(senv)


# ============================================================ 2. edits (D66)
async def test_an_edit_of_a_covered_message_rebuilds_the_summary(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [with_texts]
    summaries = summarizer(senv, fake)
    await summaries.refresh(a)
    assert "soat 10" in await stored_texts(senv)

    senv.clock.advance(minutes=5)
    await deliver(
        senv,
        business_message(
            message_id=11, message_text=TEXT_A.replace("10", "12"), date=ts(0), edit=True
        ),
    )
    assert await summary_kinds(senv) == []  # the covering summary is dropped
    fake.parsed.append(with_texts)
    assert (await summaries.refresh(a)).status is SummaryStatus.REFRESHED

    rebuilt = everything(fake.requests[-1])
    assert "soat 12" in rebuilt
    assert "soat 10" not in rebuilt
    stored = await stored_texts(senv)
    assert "soat 12" in stored
    assert "soat 10" not in stored


# ============================================================ 3. redelivered deletion
async def test_a_redelivered_deletion_keeps_the_rolling_summary(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [with_texts, with_texts]
    summaries = summarizer(senv, fake)
    await summaries.refresh(a)
    # The first message: the rebuilt summary covers a later id, so a stale
    # "purged" id from the repeat would fall under its coverage.
    await deliver(senv, deleted_messages(message_ids=[11]))
    await summaries.refresh(a)  # rebuilt once, without the deleted message
    assert await summary_kinds(senv) == [(a, "rolling")]

    await deliver(senv, deleted_messages(message_ids=[11]))  # Telegram sends it again

    assert await summary_kinds(senv) == [(a, "rolling")]
    assert len(fake.requests) == 2


# ============================================================ 4. connection caveats
async def test_the_coverage_caveat_uses_the_earliest_coverage_not_the_latest_reconnect(
    senv: Env,
) -> None:
    await seed(senv)
    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.business_connections SET connected_at = :t"),
            {"t": NOW - timedelta(hours=1)},  # reconnected at 11:00
        )
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake, since="2026-10-18 07:30")

    lines = result.data["coverage_caveats"]
    assert not any("11:00" in line for line in lines)
    assert any(line.startswith("Coverage began at 2026-10-18 09:00") for line in lines)


@pytest.mark.parametrize(("status", "enabled"), [("disabled", False), ("stale", True)])
async def test_a_connection_that_is_not_active_gets_a_caveat(
    senv: Env, status: str, enabled: bool
) -> None:
    await seed(senv)
    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.business_connections SET status = :s, is_enabled = :e"),
            {"s": status, "e": enabled},
        )
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake)

    assert any(
        line.startswith("The business connection is not active")
        for line in result.data["coverage_caveats"]
    )


# ============================================================ 5. attention-list cost
async def _attention(env: Env, fake: FakeLLM) -> ToolOutcome:
    return await get_attention_list_tool(
        await tool_ctx(env, fake), GetAttentionListArgs(active_within_days=None, limit=None)
    )


async def test_the_attention_check_retries_a_changed_chat_once_without_notice(
    senv: Env,
) -> None:
    a, b = await seed(senv)
    fake = FakeLLM()

    def per_chat(request: LLMRequest) -> Any:
        if "SYNTH-B" in everything(request):
            return echo(request)
        return hooks.pop(0)(request)

    hooks: list[Any] = [during(delete(senv, 12)), with_texts]
    fake.parsed = [per_chat, per_chat, per_chat]

    result = await _attention(senv, fake)

    checks = {e["conversation_id"]: e["check"] for e in result.data["conversations"]}
    assert checks == {a: "model", b: "model"}
    assert "notices" not in result.data
    assert TEXT_A2 not in await stored_texts(senv)
    assert len(summary_requests(fake)) == 3


async def test_the_attention_check_reports_a_chat_that_changed_during_both_calls(
    senv: Env,
) -> None:
    a, b = await seed(senv)
    fake = FakeLLM()

    def per_chat(request: LLMRequest) -> Any:
        if "SYNTH-B" in everything(request):
            return echo(request)
        return hooks.pop(0)(request)

    hooks: list[Any] = [during(delete(senv, 12)), during(delete(senv, 11))]
    fake.parsed = [per_chat, per_chat, per_chat]

    result = await _attention(senv, fake)

    by_id = {e["conversation_id"]: e for e in result.data["conversations"]}
    assert (by_id[a]["check"], by_id[a]["needs_reply"]) == ("changed_during_summary", None)
    assert by_id[b]["check"] == "model"  # unaffected
    [notice] = result.data["notices"]
    assert CHANGED_NOTICE in notice
    assert "deleted or edited" in notice
    assert TEXT_A not in json.dumps(result.data)
    assert TEXT_A2 not in json.dumps(result.data)
    assert [r.conversation_id for r in await summary_rows(senv)] == [b]


async def test_the_attention_list_makes_at_most_one_summary_call_per_chat(senv: Env) -> None:
    a, b = await seed(senv)  # chat A has two unseen messages, B one
    fake = FakeLLM()
    fake.parsed = [echo, echo, echo, echo]
    ctx = await tool_ctx(senv, fake, messages_per_call=1)

    result = await get_attention_list_tool(
        ctx, GetAttentionListArgs(active_within_days=None, limit=None)
    )

    assert len(fake.requests) == 2  # one per stale chat, never chained
    checks = {e["conversation_id"]: e["check"] for e in result.data["conversations"]}
    assert checks == {a: "not_checked", b: "model"}  # A still has an unseen message


# ============================================================ 6. runs always closed
async def test_an_unexpected_error_fails_the_run_and_spares_the_other_chats(senv: Env) -> None:
    a, b = await seed(senv)

    def flaky(request: LLMRequest) -> dict[str, Any]:
        if "SYNTH-A" in everything(request):
            raise RuntimeError("synthetic unexpected failure")
        return echo(request)

    fake = FakeLLM()
    fake.parsed = [flaky, flaky]

    result = await catch_up(senv, fake)

    assert [e["conversation_id"] for e in result.data["conversations"]] == [b]
    assert [(n["conversation_id"], n["reason"]) for n in result.data["not_summarized"]] == [
        (a, "summary_failed")
    ]
    statuses = await run_statuses(senv)
    assert "running" not in [s for s, _ in statuses]
    assert ("failed", "exception:RuntimeError") in statuses


async def test_a_store_error_fails_the_run(senv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from assistant.memory import summaries as summaries_module

    a, _ = await seed(senv)

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("synthetic database failure")

    monkeypatch.setattr(summaries_module, "store_rolling", boom)
    fake = FakeLLM()
    fake.parsed = [echo]

    with pytest.raises(ConnectionError):
        await summarizer(senv, fake).refresh(a)

    assert await run_statuses(senv) == [("failed", "exception:ConnectionError")]


# ============================================================ 7. maximum wait
async def test_a_message_during_the_call_restarts_the_maximum_wait_from_it(senv: Env) -> None:
    a, _ = await seed(senv)
    assert await pending_since(senv, a) == NOW

    async def new_message() -> None:
        senv.clock.advance(minutes=5)
        await deliver(
            senv, business_message(message_id=15, message_text="SYNTH-A late", date=ts(9))
        )

    fake = FakeLLM()
    fake.parsed = [during(new_message, echo)]

    assert (await summarizer(senv, fake).refresh(a)).status is SummaryStatus.REFRESHED

    assert await summary_kinds(senv) == [(a, "rolling")]
    assert await pending_since(senv, a) == NOW + timedelta(minutes=5)


# ============================================================ safety
async def test_a_cached_period_summary_needs_consent_too(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo]
    await catch_up(senv, fake, conversation_id=a)  # cached now
    owner = await account_id(senv)
    async with senv.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE app.consents SET revoked_at = now() "
                "WHERE account_id = :a AND kind = 'ai_managed'"
            ),
            {"a": owner},
        )
        await session.execute(
            text(
                "INSERT INTO app.consents (account_id, kind, policy_version, processors) "
                "VALUES (:a, 'ai_managed', 'v4', CAST(:p AS jsonb))"
            ),
            {"a": owner, "p": json.dumps(["Anthropic"])},
        )

    result = await catch_up(senv, fake, conversation_id=a)

    assert result.data["conversations"] == []
    assert result.data["not_summarized"][0]["reason"] == "no_consent_for_the_summary_model"
    assert "SUMMARY-OF-A" not in json.dumps(result.data)


async def test_a_catch_up_of_an_ignored_chat_says_it_is_ignored(senv: Env) -> None:
    a, _ = await seed(senv)
    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"), {"c": a}
        )

    result = await catch_up(senv, FakeLLM(), conversation_id=a)

    assert any("Ignore mode" in line for line in result.data["coverage_caveats"])


# ============================================================ nits
async def test_an_open_ended_catch_up_includes_slightly_future_message_dates(senv: Env) -> None:
    await seed(senv)
    future = int((NOW + timedelta(minutes=1)).timestamp())
    await deliver(
        senv,
        business_message(
            message_id=41,
            from_id=OTHER_CONTACT,
            chat_id=OTHER_CONTACT,
            message_text="SYNTH-B soon",
            date=future,
        ),
    )
    fake = FakeLLM()
    fake.parsed = [echo]

    result = await catch_up(senv, fake, since="2026-10-18 11:30")

    b = await conversation_id(senv, OTHER_CONTACT)
    assert [e["conversation_id"] for e in result.data["conversations"]] == [b]
