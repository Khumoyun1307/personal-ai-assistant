"""S03.2 summaries and catch-up, over the real inbox, jobs table and Postgres.

The required S03.2 tests (IMPLEMENTATION_PLAN §5), each findable by name:

- summary refresh triggered and debounced —
  `test_summary_refresh_is_triggered_by_new_content_and_debounced`,
  `test_the_refresh_job_waits_for_quiet_then_summarizes` (trailing debounce),
  `test_the_maximum_wait_refreshes_a_chat_that_never_goes_quiet`;
- catch-up includes possible/definite gap caveats and omits delayed-only
  gaps — `test_catch_up_includes_possible_and_definite_gap_caveats_and_omits_delayed_only_gaps`;
- FakeLLM receives only the right conversation's messages per summary call
  (isolation, §6 invariant 4) — `test_each_summary_call_sees_only_its_own_conversation`;
- `summarize` runs are given no tools (RG-17) —
  `test_rg17_summarize_runs_are_given_no_tools`;
- consent enforced (invariant 9) — `test_no_summary_without_managed_consent`,
  `test_consent_revoked_between_defer_and_run_stops_the_refresh`,
  `test_a_summarize_route_whose_processor_is_not_named_in_consent_sends_nothing`;
- crash between commit and defer → summary job re-created —
  `test_crash_between_commit_and_defer_the_reconciler_recreates_the_summary_job`.

Synthetic text only; FakeLLM stands in for the provider (AGENTS §11).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from structlog.testing import capture_logs

from assistant.agent import owner_texts, prompt_registry
from assistant.agent.locales import SupportedLocale, supported_locale
from assistant.agent.models import AgentRun
from assistant.agent.summarize import SummaryStatus
from assistant.agent.tools.conversations import GetConversationArgs, get_conversation_tool
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    CHAT_ACCESS_OFF_REASON,
    CapabilityTier,
    ToolContext,
    ToolOutcome,
    ToolServices,
    default_registry,
)
from assistant.agent.tools.summaries import (
    GetAttentionListArgs,
    SummarizePeriodArgs,
    get_attention_list_tool,
    summarize_period_tool,
)
from assistant.conversations.models import Conversation
from assistant.inbox.notify import OwnerAlerts
from assistant.jobs.tasks import JobHandlers
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    StopReason,
    TextItem,
    ToolResultItem,
)
from assistant.memory import catch_up as waiting
from assistant.memory.models import ConversationSummary
from tests.fakes.channel import FakeChannel
from tests.fakes.deletions import RecordingDeleter
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import args, ask, one_id, runtime, summarizer
from tests.integration.business_env import (
    CONTACT,
    NOW,
    OTHER_CONTACT,
    OWNER,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    environment,
    grant_managed,
    private_message,
)

pytestmark = pytest.mark.integration

#: Message dates: 09:00 UTC on the test day, before the FakeClock's noon.
AT = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
TEXT_A = "SYNTH-A Ertaga soat 10 da uchrashamizmi?"
TEXT_A2 = "SYNTH-A Manzilni yuboraman."
TEXT_B = "SYNTH-B Hujjatlarni juma kuni yuboraman."
THIRD_CONTACT = 7000000007
DEBOUNCE = timedelta(minutes=30)
SUMMARY_FLAGS = frozenset(
    {"tool.summarize_period", "tool.get_attention_list", "tool.get_conversation"}
)


def ts(minutes: int) -> int:
    return int((AT + timedelta(minutes=minutes)).timestamp())


@pytest.fixture
async def senv(migrated_url: str) -> AsyncIterator[Env]:
    """The S02 environment with the summary trigger wired (flag on)."""
    async with environment(migrated_url, summaries=True) as built:
        yield built


async def seed(env: Env, *, managed: bool = True) -> tuple[int, int]:
    """Owner connected at 08:00; chat A (two contact messages) and chat B
    (one). Returns the two conversation ids."""
    await connect(env, date=ts(-60))
    if managed:
        await grant_managed(env)
    await deliver(
        env, business_message(message_id=11, message_text=TEXT_A, first_name="Kamola", date=ts(0))
    )
    await deliver(
        env, business_message(message_id=12, message_text=TEXT_A2, first_name="Kamola", date=ts(2))
    )
    await deliver(
        env,
        business_message(
            message_id=21,
            from_id=OTHER_CONTACT,
            chat_id=OTHER_CONTACT,
            message_text=TEXT_B,
            first_name="Bobur",
            date=ts(5),
        ),
    )
    return await conversation_id(env, CONTACT), await conversation_id(env, OTHER_CONTACT)


async def conversation_id(env: Env, chat_id: int) -> int:
    return await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=chat_id
    )


async def account_id(env: Env) -> int:
    return await one_id(env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER)


# ------------------------------------------------------------ scripted output
def output(
    *,
    overview: str = "Synthetic overview.",
    questions: list[tuple[str, list[int]]] | None = None,
    facts: list[tuple[str, list[int]]] | None = None,
    needs_reply: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "overview": overview,
        "open_questions_to_owner": [{"text": t, "message_ids": ids} for t, ids in questions or []],
        "commitments": [],
        "decisions_requested": [],
        "key_facts": [{"text": t, "message_ids": ids} for t, ids in facts or []],
        "needs_reply": needs_reply,
        "needs_reply_reason": reason,
        "language": "uz",
    }


def everything(request: LLMRequest) -> str:
    parts = [request.instructions]
    parts.extend(item.text for item in request.items if isinstance(item, TextItem))
    return "\n".join(parts)


def shown_ids(request: LLMRequest) -> list[int]:
    return [int(i) for i in re.findall(r"^\[(\d+)\]", everything(request), flags=re.MULTILINE)]


def echo(request: LLMRequest) -> dict[str, Any]:
    """A summary citing every message shown, needing a reply for chat A."""
    ids = shown_ids(request)
    needs = "SYNTH-A" in everything(request)
    return output(
        overview="SUMMARY-OF-A" if needs else "SUMMARY-OF-B",
        questions=[("Uchrashuv vaqti?", ids[:1])] if needs else [],
        facts=[("Synthetic fact", ids)],
        needs_reply=needs,
        reason="Savol bor" if needs else None,
    )


def summary_requests(fake: FakeLLM) -> list[LLMRequest]:
    return [r for r in fake.requests if r.purpose == "summarize"]


# ------------------------------------------------------------ jobs and rows
@dataclass(frozen=True)
class JobRow:
    conversation_id: int
    status: str
    scheduled_at: datetime | None
    queueing_lock: str | None
    lock: str | None


async def refresh_jobs(env: Env) -> list[JobRow]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT (args->>'conversation_id')::bigint, status::text, scheduled_at, "
                "queueing_lock, lock FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'memory.refresh_summary' ORDER BY id"
            )
        )
        return [JobRow(int(r[0]), str(r[1]), r[2], r[3], r[4]) for r in rows.all()]


async def take_job(env: Env, conversation: int) -> None:
    """A worker picks the queued job up (`doing`), freeing its queueing lock."""
    async with env.factory() as session, session.begin():
        worker = (
            await session.execute(
                text(
                    "INSERT INTO procrastinate.procrastinate_workers (last_heartbeat) "
                    "VALUES (now()) RETURNING id"
                )
            )
        ).scalar_one()
        await session.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs SET status = 'doing', worker_id = :w "
                "WHERE task_name = 'memory.refresh_summary' AND status = 'todo' "
                "AND args->>'conversation_id' = :c"
            ),
            {"w": worker, "c": str(conversation)},
        )


def handlers(env: Env, fake: FakeLLM, **config: Any) -> JobHandlers:
    assert env.scheduler is not None

    class NoRuntime:
        async def run(self, turn_id: int) -> Any:
            raise AssertionError("not used")

    class NoApplier:
        async def apply(self, confirmation_id: int) -> Any:
            raise AssertionError("not used")

    return JobHandlers(
        session_factory=env.factory,
        clock=env.clock,
        scheduler=env.scheduler,
        owner_runtime=NoRuntime(),
        alerts=OwnerAlerts(env.factory, FakeChannel(), env.clock),
        monthly_budget_usd=None,
        heartbeat_url=None,
        confirmation_applier=NoApplier(),
        summary_refresher=summarizer(env, fake, **config),
        data_deleter=RecordingDeleter(),
        summaries_enabled=True,
        summary_debounce=DEBOUNCE,
        summary_max_wait=timedelta(hours=6),
    )


async def rolling(env: Env, conversation: int) -> ConversationSummary | None:
    async with env.factory() as session:
        return (
            await session.execute(
                select(ConversationSummary)
                .where(ConversationSummary.conversation_id == conversation)
                .where(ConversationSummary.kind == "rolling")
            )
        ).scalar_one_or_none()


async def summary_rows(env: Env) -> list[ConversationSummary]:
    async with env.factory() as session:
        return list(
            (await session.execute(select(ConversationSummary).order_by(ConversationSummary.id)))
            .scalars()
            .all()
        )


async def flag(env: Env, conversation: int) -> datetime | None:
    async with env.factory() as session:
        return (
            await session.execute(
                select(Conversation.pending_ai_since).where(Conversation.id == conversation)
            )
        ).scalar_one()


async def message_ids(env: Env, conversation: int) -> list[int]:
    async with env.factory() as session:
        rows = await session.execute(
            text("SELECT id FROM app.messages WHERE conversation_id = :c ORDER BY id"),
            {"c": conversation},
        )
        return [int(r[0]) for r in rows.all()]


async def summarize_runs(env: Env) -> list[AgentRun]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(AgentRun).where(AgentRun.run_type == "summarize").order_by(AgentRun.id)
                )
            )
            .scalars()
            .all()
        )


# ------------------------------------------------------------ calling tools
async def tool_context(env: Env, fake: FakeLLM | None) -> ToolContext:
    return ToolContext(
        account_id=await account_id(env),
        run_id=0,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory,
            clock=env.clock,
            monthly_budget_usd=None,
            summarizer=None if fake is None else summarizer(env, fake),
        ),
    )


async def catch_up(
    env: Env,
    fake: FakeLLM | None,
    *,
    since: str,
    until: str | None,
    contact_id: int | None = None,
    conversation: int | None = None,
) -> ToolOutcome:
    ctx = await tool_context(env, fake)
    arguments = SummarizePeriodArgs(
        since=since, until=until, contact_id=contact_id, conversation_id=conversation
    )
    return await summarize_period_tool(ctx, arguments)


async def attention(env: Env, fake: FakeLLM | None, *, limit: int | None = None) -> ToolOutcome:
    ctx = await tool_context(env, fake)
    return await get_attention_list_tool(
        ctx, GetAttentionListArgs(active_within_days=None, limit=limit)
    )


# ============================================================ triggering
async def test_summary_refresh_is_triggered_by_new_content_and_debounced(senv: Env) -> None:
    a, b = await seed(senv)

    jobs = await refresh_jobs(senv)
    assert sorted(job.conversation_id for job in jobs) == sorted([a, b])
    for job in jobs:
        # One debounce out, a queueing lock, and never a `lock` (AGENTS §13).
        assert job.status == "todo"
        assert job.scheduled_at == NOW + DEBOUNCE
        assert job.queueing_lock == f"sum:{job.conversation_id}"
        assert job.lock is None
    assert await flag(senv, a) == NOW

    # A burst in the same chat — a new message and an edit — queues nothing more.
    senv.clock.advance(minutes=3)
    await deliver(senv, business_message(message_id=13, message_text="SYNTH-A uch", date=ts(3)))
    await deliver(
        senv,
        business_message(message_id=11, message_text=TEXT_A + " (tahrir)", date=ts(0), edit=True),
    )
    assert [job.conversation_id for job in await refresh_jobs(senv)].count(a) == 1


async def test_no_job_without_the_flag(env: Env) -> None:
    """Flag `summary.refresh` off (the default): content is flagged, nothing queued."""
    a, _ = await seed(env)
    assert await refresh_jobs(env) == []
    assert await flag(env, a) is not None


async def test_the_refresh_job_waits_for_quiet_then_summarizes(senv: Env) -> None:
    """Trailing debounce: a job that comes due while the chat is still
    active re-defers itself; once quiet it makes one incremental call."""
    a, _ = await seed(senv)
    fake = FakeLLM()
    jobs = handlers(senv, fake)

    senv.clock.advance(minutes=20)  # 12:20, another message
    await deliver(senv, business_message(message_id=13, message_text="SYNTH-A uch", date=ts(20)))
    senv.clock.advance(minutes=10)  # 12:30: the first job comes due
    await take_job(senv, a)

    assert await jobs.refresh_summary(a) is None
    assert fake.requests == []
    todo = [j for j in await refresh_jobs(senv) if j.conversation_id == a and j.status == "todo"]
    assert [j.scheduled_at for j in todo] == [NOW + timedelta(minutes=50)]

    senv.clock.advance(minutes=20)  # 12:50: quiet for 30 minutes
    await take_job(senv, a)
    fake.parsed.append(echo)
    outcome = await jobs.refresh_summary(a)

    assert outcome is not None
    assert outcome.status is SummaryStatus.REFRESHED
    assert len(summary_requests(fake)) == 1
    stored = await rolling(senv, a)
    assert stored is not None
    assert stored.covers_through_message_id == max(await message_ids(senv, a))
    assert stored.message_count == 3
    assert await flag(senv, a) is None


async def test_the_maximum_wait_refreshes_a_chat_that_never_goes_quiet(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    jobs = handlers(senv, fake)
    for step in range(1, 19):  # a message every 20 minutes for six hours
        senv.clock.advance(minutes=20)
        await deliver(
            senv,
            business_message(message_id=100 + step, message_text="SYNTH-A davom", date=ts(step)),
        )
    assert senv.clock.now() == NOW + timedelta(hours=6)
    await take_job(senv, a)
    fake.parsed.append(echo)

    outcome = await jobs.refresh_summary(a)

    assert outcome is not None
    assert outcome.status is SummaryStatus.REFRESHED
    assert len(summary_requests(fake)) == 1


async def test_refresh_is_incremental_and_records_what_it_covers(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo]
    summaries = summarizer(senv, fake)
    assert (await summaries.refresh(a)).status is SummaryStatus.REFRESHED
    first = await rolling(senv, a)
    assert first is not None
    assert first.message_count == 2

    senv.clock.advance(minutes=40)
    await deliver(senv, business_message(message_id=14, message_text="SYNTH-A NEW", date=ts(40)))
    fake.parsed.append(echo)
    assert (await summaries.refresh(a)).status is SummaryStatus.REFRESHED

    second_request = summary_requests(fake)[-1]
    body = everything(second_request)
    assert "SYNTH-A NEW" in body
    assert TEXT_A not in body  # already summarized: only the new message is sent
    assert "SUMMARY-OF-A" in body  # ...with the previous summary, wrapped
    assert '<untrusted source="summary" trust="untrusted">' in body
    second = await rolling(senv, a)
    assert second is not None
    assert second.covers_through_message_id == max(await message_ids(senv, a))
    assert second.message_count == 3
    assert second.prompt_version == prompt_registry.active("summarize").label
    # Nothing new: no call, and the flag stays clear.
    assert (await summaries.refresh(a)).status is SummaryStatus.UP_TO_DATE
    assert len(summary_requests(fake)) == 2


# ============================================================ RG-17, isolation
async def test_rg17_summarize_runs_are_given_no_tools(senv: Env) -> None:
    """RG-17 (§6 invariants 2, 3a): a `summarize` run is a structured call with
    no tools, whatever flags are on, and the registry offers it nothing."""
    a, b = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo, echo, echo]
    await summarizer(senv, fake).refresh(a)
    result = await catch_up(senv, fake, since="2026-10-18 08:00", until=None)
    assert result.status == "ok"

    requests = summary_requests(fake)
    assert len(requests) == 3
    assert all(request.tools == () for request in requests)
    runs = await summarize_runs(senv)
    assert {(run.run_type, run.prompt_version, run.status) for run in runs} == {
        ("summarize", prompt_registry.active("summarize").label, "succeeded")
    }
    registry = default_registry()
    every_flag = frozenset(tool.release_flag for tool in registry.all())
    assert (
        registry.for_run(
            "summarize",
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=every_flag,
            managed_consent=True,
        )
        == {}
    )
    assert b  # chat B was summarized by the catch-up too


async def test_each_summary_call_sees_only_its_own_conversation(senv: Env) -> None:
    """§6 invariant 4: one conversation per summary call — the catch-up's
    map calls and a rolling refresh alike (FakeLLM inspects every prompt)."""
    a, b = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo, echo, echo]

    result = await catch_up(senv, fake, since="2026-10-18 08:00", until="2026-10-18 10:00")
    await summarizer(senv, fake).refresh(b)

    requests = summary_requests(fake)
    assert len(requests) == 3
    for request in requests:
        body = everything(request)
        assert ("SYNTH-A" in body) != ("SYNTH-B" in body), "one chat per call"
        # Each call names its own contact only (S03.7); the next test checks it.
        assert not ("Kamola" in body and "Bobur" in body)
    a_ids, b_ids = set(await message_ids(senv, a)), set(await message_ids(senv, b))
    for request in requests:
        cited = set(shown_ids(request))
        assert cited <= a_ids or cited <= b_ids
    # The refresh of B saw only B.
    assert "SYNTH-B" in everything(requests[-1])
    assert "SYNTH-A" not in everything(requests[-1])
    by_id = {e["conversation_id"]: e for e in result.data["conversations"]}
    assert "SUMMARY-OF-A" in by_id[a]["summary"]["overview"]
    assert "SUMMARY-OF-B" in by_id[b]["summary"]["overview"]


async def test_each_summary_call_sees_only_its_own_contacts_name(senv: Env) -> None:
    """§6 invariant 4 (S03.7): the summarize context names the contact of ITS
    chat, once and wrapped as untrusted, and no other contact."""
    a, b = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo, echo, echo]

    await catch_up(senv, fake, since="2026-10-18 08:00", until="2026-10-18 10:00")
    await summarizer(senv, fake).refresh(b)

    requests = summary_requests(fake)
    assert len(requests) == 3
    for request in requests:
        body = everything(request)
        own, other = ("Kamola", "Bobur") if "SYNTH-A" in body else ("Bobur", "Kamola")
        assert f'<untrusted source="contact_name" trust="untrusted">{own}</untrusted>' in body
        assert body.count(own) == 1
        assert other not in body
    assert a != b


async def test_the_summarize_context_says_when_an_edited_message_was_edited(senv: Env) -> None:
    """c36 (S03.7 A2): the stored edit time reaches the model with the edited
    message, which shows its current text, so a later "I changed the time" is
    read against the corrected value."""
    a, _ = await seed(senv)
    await deliver(
        senv,
        business_message(
            message_id=11,
            message_text=TEXT_A + " soat 18:00",
            date=ts(0),
            edit=True,
            edit_date=ts(40),
        ),
    )
    fake = FakeLLM()
    fake.parsed = [echo]
    assert (await summarizer(senv, fake).refresh(a)).status is SummaryStatus.REFRESHED

    (request,) = summary_requests(fake)
    lines = request.items[-1].text  # type: ignore[union-attr]
    assert "CONTACT (edited at 2026-10-18 09:40; this is its current text): " in lines
    assert lines.count("(edited at") == 1


# ============================================================ attribution (S03.7)
#: A message line of the summarize context: id and the sender's fixed label.
_LINE = re.compile(r"^\[(\d+)\] \S+ \S+ (CONTACT|YOU)", re.MULTILINE)


def sides(request: LLMRequest) -> dict[str, list[int]]:
    """The shown message ids by side, as the context labels them."""
    found: dict[str, list[int]] = {"CONTACT": [], "YOU": []}
    for message_id, label in _LINE.findall(everything(request)):
        found[label].append(int(message_id))
    return found


def commitment(by: str, status: str, text: str, ids: list[int]) -> dict[str, Any]:
    return {"by": by, "status": status, "text": text, "due": None, "message_ids": ids}


async def owner_writes(env: Env, *, message_id: int, minutes: int) -> None:
    """The owner's own message in chat A (Kamola), sent from their account."""
    await deliver(
        env,
        business_message(
            message_id=message_id,
            from_id=OWNER,
            chat_id=CONTACT,
            message_text="SYNTH-A owner words",
            first_name="Kamola",
            date=ts(minutes),
        ),
    )


async def test_a_misattributed_promise_is_stored_as_a_request(senv: Env) -> None:
    """RG-17 family (S03.7): a "promise" of the owner that cites only the
    contact's messages is the contact's request; it is stored as one, never as
    the owner's promise; a question citing only the owner is dropped."""
    a, _ = await seed(senv)
    await owner_writes(senv, message_id=13, minutes=1)
    fake = FakeLLM()

    def misattributed(request: LLMRequest) -> dict[str, Any]:
        shown = sides(request)
        return {
            **output(questions=[("Own question", shown["YOU"][:1])]),
            "commitments": [
                commitment("owner", "promised", "Return the cans", shown["CONTACT"][:1]),
                commitment("contact", "agreed", "Send the address", shown["CONTACT"][1:2]),
            ],
        }

    fake.parsed = [misattributed]
    assert (await summarizer(senv, fake).refresh(a)).status is SummaryStatus.REFRESHED

    stored = await rolling(senv, a)
    assert stored is not None
    assert [(c["by"], c["status"]) for c in stored.summary["commitments"]] == [
        ("owner", "requested"),
        ("contact", "promised"),
    ]
    assert stored.summary["open_questions_to_owner"] == []

    # The owner's views read who asked from the fields, in fixed words.
    ctx = await tool_context(senv, None)
    arguments = GetConversationArgs(conversation_id=a, contact_id=None, cursor=None, limit=None)
    view = (await get_conversation_tool(ctx, arguments)).data["summary"]
    assert view["attribution_rule"].startswith("Say who asked")
    request, promise = view["commitments"]
    assert request["who"].startswith('<untrusted source="contact_name" trust="untrusted">')
    assert "Kamola asks you to (not agreed yet)" in request["who"]
    assert "Kamola promised you" in promise["who"]
    listed = (await attention(senv, None)).data["conversations"]
    entry = next(e for e in listed if e["conversation_id"] == a)
    assert [c["status"] for c in entry["requests_to_owner"]] == ["requested"]


async def test_a_rolling_refresh_checks_carried_items_against_stored_authors(
    senv: Env,
) -> None:
    """A refresh shows only the new messages; items carried from the previous
    summary cite older ones. Their authors come from the database, so a carried
    misattribution is caught too."""
    a, _ = await seed(senv)
    await owner_writes(senv, message_id=13, minutes=1)
    fake = FakeLLM()
    first: dict[str, list[int]] = {}

    def initial(request: LLMRequest) -> dict[str, Any]:
        first.update(sides(request))
        return output(facts=[("Synthetic fact", first["CONTACT"] + first["YOU"])])

    fake.parsed = [initial]
    summaries = summarizer(senv, fake)
    assert (await summaries.refresh(a)).status is SummaryStatus.REFRESHED

    senv.clock.advance(minutes=40)
    await deliver(senv, business_message(message_id=14, message_text="SYNTH-A NEW", date=ts(40)))

    def carried(request: LLMRequest) -> dict[str, Any]:
        new = sides(request)
        assert set(new["CONTACT"]).isdisjoint(first["CONTACT"])  # only the new one shown
        return {
            **output(
                questions=[("Carried owner question", first["YOU"])],
                facts=[("Synthetic fact", first["CONTACT"])],
            ),
            "commitments": [
                commitment("owner", "promised", "Carried promise", first["CONTACT"][:1]),
                commitment("contact", "promised", "New promise", new["CONTACT"]),
            ],
        }

    fake.parsed = [carried]
    assert (await summaries.refresh(a)).status is SummaryStatus.REFRESHED
    stored = await rolling(senv, a)
    assert stored is not None
    assert [(c["text"], c["status"]) for c in stored.summary["commitments"]] == [
        ("Carried promise", "requested"),
        ("New promise", "promised"),
    ]
    assert stored.summary["open_questions_to_owner"] == []


async def test_a_period_summary_cached_under_an_older_prompt_version_is_not_served(
    senv: Env,
) -> None:
    """The cache key names the prompt version and the model (S03.7): a v4 period
    summary (no attribution status) is never served as a v5 one."""
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo, echo, echo]
    since, until = "2026-10-18 08:30", "2026-10-18 10:00"

    await catch_up(senv, fake, since=since, until=until, conversation=a)
    await catch_up(senv, fake, since=since, until=until, conversation=a)
    assert len(summary_requests(fake)) == 1  # the same version: cached

    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversation_summaries SET prompt_version = 'summarize.v4'")
        )
    await catch_up(senv, fake, since=since, until=until, conversation=a)
    assert len(summary_requests(fake)) == 2

    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversation_summaries SET model = 'an-older-model'")
        )
    await catch_up(senv, fake, since=since, until=until, conversation=a)
    assert len(summary_requests(fake)) == 3
    periods = [row for row in await summary_rows(senv) if row.kind == "period"]
    assert [(p.prompt_version, p.model) for p in periods] == [
        (prompt_registry.active("summarize").label, "gpt-6-luna")
    ]


# ============================================================ consent
async def test_no_summary_without_managed_consent(senv: Env) -> None:
    a, _ = await seed(senv, managed=False)
    fake = FakeLLM()
    senv.clock.advance(minutes=31)
    await take_job(senv, a)

    outcome = await handlers(senv, fake).refresh_summary(a)

    assert outcome is not None
    assert outcome.status is SummaryStatus.NO_CONSENT
    assert fake.requests == []
    assert await summarize_runs(senv) == []
    assert await flag(senv, a) is not None  # processed once consent is granted


async def test_consent_revoked_between_defer_and_run_stops_the_refresh(senv: Env) -> None:
    """A job queued under consent re-checks it when it runs (§14.3)."""
    a, _ = await seed(senv)
    assert any(job.conversation_id == a for job in await refresh_jobs(senv))
    await deliver(senv, private_message(OWNER, "/privacy revoke"))
    fake = FakeLLM()
    senv.clock.advance(minutes=31)
    await take_job(senv, a)

    outcome = await handlers(senv, fake).refresh_summary(a)

    assert outcome is not None
    assert outcome.status is SummaryStatus.NO_CONSENT
    assert fake.requests == []
    assert await rolling(senv, a) is None
    # ...and the reconciler does not bring it back while consent is off.
    senv.clock.advance(minutes=5)
    await handlers(senv, fake).reconcile_pending()
    assert not [
        j for j in await refresh_jobs(senv) if j.status == "todo" and j.conversation_id == a
    ]


async def test_a_summarize_route_whose_processor_is_not_named_in_consent_sends_nothing(
    senv: Env,
) -> None:
    """LLM_ROUTE_SUMMARIZE_MODEL moved to another provider's model without
    re-consent: no call at all, background or on demand (M03 D48)."""
    a, _ = await seed(senv)
    fake = FakeLLM()
    summaries = summarizer(senv, fake, model="claude-synthetic-1")

    assert (await summaries.refresh(a)).status is SummaryStatus.NO_CONSENT
    period = await summaries.summarize_period(
        account_id=await account_id(senv),
        conversation_id=a,
        start=AT,
        end=AT + timedelta(hours=1),
        open_ended=False,
        parent_run_id=None,
    )
    assert period.status is SummaryStatus.NO_CONSENT
    assert fake.requests == []


# ============================================================ reconciler
class CrashingScheduler:
    """The defer after the ingesting commit never happens (a crash)."""

    async def defer(self, task: str, **kwargs: Any) -> int | None:
        raise ConnectionError("synthetic crash between commit and defer")


async def test_crash_between_commit_and_defer_the_reconciler_recreates_the_summary_job(
    senv: Env,
) -> None:
    assert senv.summary_trigger is not None
    senv.summary_trigger.scheduler = CrashingScheduler()  # type: ignore[assignment]
    a, b = await seed(senv)
    assert await refresh_jobs(senv) == []
    assert await flag(senv, a) is not None  # committed: the flag survived the crash
    jobs = handlers(senv, FakeLLM())

    await jobs.reconcile_pending()  # within the grace period: not yet
    assert await refresh_jobs(senv) == []

    senv.clock.advance(minutes=1)
    await jobs.reconcile_pending()
    recreated = await refresh_jobs(senv)
    assert sorted(job.conversation_id for job in recreated) == sorted([a, b])
    assert all(job.status == "todo" and job.lock is None for job in recreated)
    assert all(job.scheduled_at == senv.clock.now() + DEBOUNCE for job in recreated)

    await jobs.reconcile_pending()  # queued now: nothing more
    assert len(await refresh_jobs(senv)) == 2


async def test_the_reconciler_skips_ignored_chats_and_chats_without_consent(senv: Env) -> None:
    assert senv.summary_trigger is not None
    senv.summary_trigger.scheduler = CrashingScheduler()  # type: ignore[assignment]
    a, b = await seed(senv)
    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"), {"c": b}
        )
    senv.clock.advance(minutes=1)
    await handlers(senv, FakeLLM()).reconcile_pending()
    assert [job.conversation_id for job in await refresh_jobs(senv)] == [a]


# ============================================================ output handling
async def test_a_refused_or_invalid_summary_stores_nothing(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [StopReason.REFUSAL, LLMError(LLMErrorKind.INVALID_OUTPUT)]
    summaries = summarizer(senv, fake)

    assert (await summaries.refresh(a)).status is SummaryStatus.REFUSED
    assert (await summaries.refresh(a)).status is SummaryStatus.FAILED
    assert await rolling(senv, a) is None
    assert await flag(senv, a) is not None
    runs = await summarize_runs(senv)
    assert [(r.status, r.error_class) for r in runs] == [
        ("refused", "refusal"),
        ("failed", "invalid_output"),
    ]


async def test_repeated_useless_failures_pause_the_background_refresh(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [StopReason.REFUSAL, StopReason.REFUSAL, StopReason.REFUSAL]
    summaries = summarizer(senv, fake)
    for _ in range(3):
        assert (await summaries.refresh(a)).status is SummaryStatus.REFUSED

    assert (await summaries.refresh(a)).status is SummaryStatus.BACKED_OFF
    assert len(summary_requests(fake)) == 3

    senv.clock.advance(hours=25)  # out of the window: tried again
    fake.parsed.append(echo)
    assert (await summaries.refresh(a)).status is SummaryStatus.REFRESHED


async def test_a_truncated_summary_is_retried_once_with_a_larger_budget(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [StopReason.MAX_TOKENS, echo]

    assert (await summarizer(senv, fake).refresh(a)).status is SummaryStatus.REFRESHED

    first, second = summary_requests(fake)
    assert second.max_output_tokens == 2 * first.max_output_tokens
    assert second.items == first.items  # the same request, never a prefill


async def test_items_citing_no_shown_message_are_dropped(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()

    def invented(request: LLMRequest) -> dict[str, Any]:
        ids = shown_ids(request)
        return output(
            questions=[("Real question", ids[:1]), ("Invented question", [999_999_999])],
            facts=[("Uncited fact", [])],
        )

    fake.parsed = [invented]
    await summarizer(senv, fake).refresh(a)

    stored = await rolling(senv, a)
    assert stored is not None
    assert [q["text"] for q in stored.summary["open_questions_to_owner"]] == ["Real question"]
    assert stored.summary["key_facts"] == []


# ============================================================ catch-up
async def insert_gap(
    env: Env,
    *,
    kind: str,
    certainty: str,
    start: datetime,
    end: datetime,
    conversation: int | None = None,
    resolved: bool = False,
) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO app.coverage_gaps (account_id, conversation_id, kind, certainty, "
                "at_risk_from, at_risk_to, resolved_at) VALUES "
                "(:a, :c, :k, :ce, :f, :t, :r)"
            ),
            {
                "a": await account_id(env),
                "c": conversation,
                "k": kind,
                "ce": certainty,
                "f": start,
                "t": end,
                "r": NOW if resolved else None,
            },
        )


async def test_catch_up_includes_possible_and_definite_gap_caveats_and_omits_delayed_only_gaps(
    senv: Env,
) -> None:
    a, b = await seed(senv)
    hour = timedelta(hours=1)
    # Inside the period: a delayed outage (late, nothing missing), a possible
    # outage and a definite dead letter in chat B; plus a resolved definite
    # gap inside and a possible gap outside the period.
    await insert_gap(senv, kind="outage", certainty="delayed", start=AT - hour, end=AT - hour / 2)
    await insert_gap(
        senv, kind="outage", certainty="possible", start=AT - hour / 3, end=AT - hour / 6
    )
    await insert_gap(
        senv,
        kind="dead_letter",
        certainty="definite",
        start=AT + hour / 6,
        end=AT + hour / 6,
        conversation=b,
    )
    await insert_gap(
        senv, kind="outage", certainty="definite", start=AT, end=AT + hour / 2, resolved=True
    )
    await insert_gap(
        senv, kind="outage", certainty="possible", start=AT - 8 * hour, end=AT - 7 * hour
    )
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake, since="2026-10-18 07:30", until="2026-10-18 10:00")

    lines = result.data["coverage_caveats"]
    gap_lines = [line for line in lines if "gap" in line.lower()]
    assert len(gap_lines) == 2
    assert any(
        line.startswith("Possible gap") and "08:40" in line and "08:50" in line
        for line in gap_lines
    )
    assert any(
        line.startswith("Definite gap") and "09:10" in line and f"conversation_id {b}" in line
        for line in gap_lines
    )
    assert not any("08:00" in line and "08:30" in line for line in lines)  # delayed: omitted
    assert not any("01:00" in line for line in lines)  # outside the period
    # The period starts before the connection (08:00): pre-connection caveat.
    assert any(line.startswith("Coverage began at 2026-10-18 08:00") for line in lines)
    assert result.data["note"].startswith("Relay every coverage caveat")
    assert {e["conversation_id"] for e in result.data["conversations"]} == {a, b}


async def test_catch_up_mentions_ignored_and_paused_chats_and_expired_messages(
    senv: Env,
) -> None:
    a, b = await seed(senv)
    await deliver(
        senv,
        business_message(
            message_id=31,
            from_id=THIRD_CONTACT,
            chat_id=THIRD_CONTACT,
            message_text="SYNTH-C",
            date=ts(10),
        ),
    )
    c = await conversation_id(senv, THIRD_CONTACT)
    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"), {"c": c}
        )
        await session.execute(
            text("UPDATE app.conversations SET availability = 'possibly_paused' WHERE id = :c"),
            {"c": b},
        )
        await session.execute(
            text(
                "UPDATE app.messages SET text = NULL, search_key = NULL, purged_at = now() "
                "WHERE conversation_id = :c AND telegram_message_id = 12"
            ),
            {"c": a},
        )
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake, since="2026-10-18 08:30", until="2026-10-18 10:00")

    lines = result.data["coverage_caveats"]
    assert any(line.startswith("1 chat(s) in Ignore mode") for line in lines)
    assert any(line.startswith("1 chat(s) may be paused") for line in lines)
    assert any(line.startswith("1 message(s) from this period expired") for line in lines)
    assert c not in {e["conversation_id"] for e in result.data["conversations"]}
    assert "SYNTH-C" not in json.dumps([everything(r) for r in fake.requests])


async def test_catch_up_groups_chats_needing_the_owner_first(senv: Env) -> None:
    a, b = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake, since="2026-10-18 08:30", until=None)

    entries = result.data["conversations"]
    assert [e["conversation_id"] for e in entries] == [a, b]
    assert [e["importance"] for e in entries] == ["needs_attention", "other"]
    assert entries[0]["summary"]["open_questions_to_owner"][0]["text"].startswith(
        '<untrusted source="summary"'
    )
    assert entries[0]["contact_name"].startswith('<untrusted source="contact_name"')
    assert result.contains_managed
    assert result.taints
    assert result.refs.conversation_ids == frozenset({a, b})


async def test_a_period_summary_is_cached_until_a_message_in_the_range_changes(
    senv: Env,
) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo]
    since, until = "2026-10-18 08:30", "2026-10-18 10:00"

    await catch_up(senv, fake, since=since, until=until, conversation=a)
    again = await catch_up(senv, fake, since=since, until=until, conversation=a)
    assert len(summary_requests(fake)) == 1  # served from the cache
    assert "SUMMARY-OF-A" in again.data["conversations"][0]["summary"]["overview"]

    senv.clock.advance(minutes=1)
    await deliver(
        senv,
        business_message(message_id=11, message_text=TEXT_A + " (tahrir)", date=ts(0), edit=True),
    )
    fake.parsed.append(echo)
    await catch_up(senv, fake, since=since, until=until, conversation=a)
    assert len(summary_requests(fake)) == 2  # an edit in the range: summarized again
    periods = [row for row in await summary_rows(senv) if row.kind == "period"]
    assert len(periods) == 1  # replaced, not accumulated


async def test_catch_up_with_more_chats_than_the_cap_lists_the_rest(senv: Env) -> None:
    from assistant.agent.tools import summaries as tool_module

    a, b = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo]
    original = tool_module.MAX_CATCH_UP_CONVERSATIONS
    tool_module.MAX_CATCH_UP_CONVERSATIONS = 1
    try:
        result = await catch_up(senv, fake, since="2026-10-18 08:30", until=None)
    finally:
        tool_module.MAX_CATCH_UP_CONVERSATIONS = original
    assert [e["conversation_id"] for e in result.data["conversations"]] == [a]
    assert result.data["not_summarized"] == [
        {
            "conversation_id": b,
            "contact_id": result.data["not_summarized"][0]["contact_id"],
            "contact_name": result.data["not_summarized"][0]["contact_name"],
            "messages_in_period": 1,
            "reason": "too_many_chats_in_one_catch_up",
        }
    ]


async def test_an_empty_period_says_so_without_calling_the_model(senv: Env) -> None:
    await seed(senv)
    fake = FakeLLM()
    result = await catch_up(senv, fake, since="2026-10-18 10:30", until="2026-10-18 11:00")
    assert result.data["conversations"] == []
    assert "No stored messages" in result.data["note_empty"]
    assert fake.requests == []


# ============================================================ get_conversation
async def test_get_conversation_returns_the_rolling_summary_wrapped(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    ctx = await tool_context(senv, fake)
    arguments = GetConversationArgs(conversation_id=a, contact_id=None, cursor=None, limit=None)
    assert (await get_conversation_tool(ctx, arguments)).data["summary"] is None

    fake.parsed = [echo]
    await summarizer(senv, fake).refresh(a)
    summary = (await get_conversation_tool(ctx, arguments)).data["summary"]

    assert summary["overview"].startswith('<untrusted source="summary" trust="untrusted">')
    assert "SUMMARY-OF-A" in summary["overview"]
    assert summary["needs_reply"] is True
    assert summary["up_to_date"] is True
    await deliver(senv, business_message(message_id=15, message_text="SYNTH-A yana", date=ts(30)))
    assert (await get_conversation_tool(ctx, arguments)).data["summary"]["up_to_date"] is False


# ============================================================ attention list
async def test_attention_list_lists_unanswered_chats_with_the_models_check(senv: Env) -> None:
    a, b = await seed(senv)
    # B: the owner answered. C: the contact wrote last, but needs nothing.
    await deliver(
        senv,
        business_message(
            message_id=22,
            from_id=OWNER,
            chat_id=OTHER_CONTACT,
            message_text="SYNTH-B owner reply",
            date=ts(6),
        ),
    )
    await deliver(
        senv,
        business_message(
            message_id=31,
            from_id=THIRD_CONTACT,
            chat_id=THIRD_CONTACT,
            message_text="SYNTH-C rahmat",
            first_name="Dilnoza",
            date=ts(1),
        ),
    )
    c = await conversation_id(senv, THIRD_CONTACT)
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await attention(senv, fake)

    entries = result.data["conversations"]
    assert [e["conversation_id"] for e in entries] == [a, c]  # B is answered
    assert (entries[0]["needs_reply"], entries[0]["check"]) == (True, "model")
    assert (entries[1]["needs_reply"], entries[1]["check"]) == (False, "model")
    assert entries[0]["reason"].startswith('<untrusted source="summary"')
    assert len(summary_requests(fake)) == 2  # both stale: refreshed inline

    again = await attention(senv, fake)  # fresh now: no further calls
    assert len(summary_requests(fake)) == 2
    assert [e["needs_reply"] for e in again.data["conversations"]] == [True, False]
    assert b not in result.refs.conversation_ids


async def test_attention_list_without_a_summarizer_is_honest_about_unchecked_chats(
    senv: Env,
) -> None:
    a, b = await seed(senv)
    result = await attention(senv, None)
    entries = result.data["conversations"]
    assert {e["conversation_id"] for e in entries} == {a, b}
    assert all((e["needs_reply"], e["check"]) == (None, "not_checked") for e in entries)


# ============================================================ waiting contacts (C1, P9)
async def owner_locale(env: Env) -> SupportedLocale:
    async with env.factory() as session:
        tag = (
            await session.execute(
                text("SELECT locale_pref FROM app.accounts WHERE telegram_user_id = :u"),
                {"u": OWNER},
            )
        ).scalar_one()
    return supported_locale(tag)


async def stored_id(env: Env, chat_id: int, telegram_message_id: int) -> int:
    return await one_id(
        env,
        "SELECT m.id FROM app.messages m JOIN app.conversations c ON c.id = m.conversation_id "
        "WHERE c.business_chat_id = :c AND m.telegram_message_id = :m",
        c=chat_id,
        m=telegram_message_id,
    )


async def chat(env: Env, chat_id: int, name: str, *lines: tuple[int, int, str, int]) -> None:
    """Messages `(telegram id, sender, text, minute)` in the chat with `chat_id`."""
    for message_id, sender, words, minute in lines:
        await deliver(
            env,
            business_message(
                message_id=message_id,
                from_id=sender,
                chat_id=chat_id,
                message_text=words,
                first_name=name,
                date=ts(minute),
            ),
        )


def no_reply_needed(request: LLMRequest) -> dict[str, Any]:
    """A summary that misses the contact's question (C1, step 7)."""
    return output(
        facts=[("Synthetic fact", shown_ids(request)[:1])],
        needs_reply=False,
        reason="SYNTH nothing expected",
    )


async def test_c1_a_contact_who_asked_after_the_owners_last_answer_is_listed_first(
    senv: Env,
) -> None:
    """C1 (owner checklist step 7, failed twice): the contact asked after the
    owner's last message, said thanks, and an away message followed; the
    freshly refreshed summary said no reply was needed. The application's own
    rule lists the chat as possibly waiting, with the question, first; the
    limit applies after that ranking, not to the chats waiting longest."""
    await connect(senv, date=ts(-60))
    await grant_managed(senv)
    # Bobur (older): his question was answered; his later thanks asks nothing.
    await chat(
        senv,
        OTHER_CONTACT,
        "Bobur",
        (21, OTHER_CONTACT, "SYNTH-B Qachon kelasiz?", 0),
        (22, OWNER, "SYNTH-B owner reply", 1),
        (23, OTHER_CONTACT, "SYNTH-B rahmat", 2),
    )
    # Kamola (newer): the owner's own messages (step 4's swapped traffic), her
    # question, her thanks, then the owner's automatic away message.
    await chat(
        senv,
        CONTACT,
        "Kamola",
        (11, OWNER, "SYNTH-A owner words", 3),
        (12, OWNER, "SYNTH-A owner words", 4),
        (13, CONTACT, "SYNTH-A Ertaga kelasizmi?", 10),
        (14, CONTACT, "SYNTH-A rahmat", 11),
    )
    await deliver(
        senv,
        business_message(
            message_id=15,
            from_id=OWNER,
            message_text="SYNTH-A away",
            first_name="Kamola",
            date=ts(12),
            extra={"is_from_offline": True},
        ),
    )
    kamola, bobur = await conversation_id(senv, CONTACT), await conversation_id(senv, OTHER_CONTACT)
    fake = FakeLLM()
    fake.parsed = [no_reply_needed] * 4
    locale = await owner_locale(senv)

    with capture_logs() as logs:
        result = await attention(senv, fake, limit=1)

    [entry] = result.data["conversations"]
    assert entry["conversation_id"] == kamola
    assert (entry["check"], entry["needs_reply"]) == ("model", False)  # the model missed it
    assert entry["may_be_waiting"] is True
    assert entry["waiting_reason"] == owner_texts.MAY_BE_WAITING[locale]
    question = entry["question"]
    asked = await stored_id(senv, CONTACT, 13)
    assert question["message_id"] == asked
    assert question["text"].startswith('<untrusted source="contact" trust="untrusted">')
    assert "SYNTH-A Ertaga kelasizmi?" in question["text"]
    assert (result.citable[asked].conversation_id, result.citable[asked].direction) == (
        kamola,
        "contact",
    )
    assert (
        result.data["waiting_count"],
        result.data["not_listed"],
        result.data["not_listed_may_be_waiting"],
    ) == (1, 1, 0)
    assert "1 more chat(s)" in result.data["note_not_listed"]
    assert len(summary_requests(fake)) == 1  # only the listed chat was checked
    [built] = [line for line in logs if line["event"] == "attention_list_built"]
    assert {k: v for k, v in built.items() if k not in ("event", "log_level")} == {
        "candidates": 2,
        "listed": 1,
        "waiting": 1,
        "may_be_waiting": 1,
        "not_checked": 0,
        "inline_checks": 1,
    }

    again = await attention(senv, fake)

    entries = again.data["conversations"]
    assert [e["conversation_id"] for e in entries] == [kamola, bobur]
    assert [e["may_be_waiting"] for e in entries] == [True, False]
    assert "question" not in entries[1]  # answered by the owner; the away message answers nothing
    assert "not_listed" not in again.data


async def test_c1_the_waiting_rule_reads_captions_and_never_a_deleted_question(
    senv: Env,
) -> None:
    """A caption is a message's text; a deleted or expired question is no
    evidence (D63); a link's '?' is not a question."""
    await connect(senv, date=ts(-60))
    await grant_managed(senv)
    photo = [{"file_id": "SYNTH-f", "file_unique_id": "SYNTH-u", "width": 10, "height": 10}]
    await deliver(
        senv,
        business_message(
            message_id=11,
            message_text=None,
            first_name="Kamola",
            date=ts(0),
            extra={"photo": photo, "caption": "SYNTH-A Bu qanchaga?"},
        ),
    )
    await chat(
        senv,
        OTHER_CONTACT,
        "Bobur",
        (21, OTHER_CONTACT, "SYNTH-B Qachon?", 1),
        (22, OTHER_CONTACT, "SYNTH-B https://example.com/?a=1", 2),
    )
    await deliver(senv, deleted_messages(chat_id=OTHER_CONTACT, message_ids=[21]))
    await chat(senv, THIRD_CONTACT, "Dilnoza", (31, THIRD_CONTACT, "SYNTH-C Qayerdasiz?", 3))
    async with senv.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE app.messages SET text = NULL, search_key = NULL, purged_at = now() "
                "WHERE telegram_message_id = 31"
            )
        )
    kamola = await conversation_id(senv, CONTACT)
    bobur = await conversation_id(senv, OTHER_CONTACT)
    dilnoza = await conversation_id(senv, THIRD_CONTACT)

    result = await attention(senv, None)

    by_id = {e["conversation_id"]: e for e in result.data["conversations"]}
    assert set(by_id) == {kamola, bobur, dilnoza}
    assert by_id[kamola]["may_be_waiting"] is True
    assert "SYNTH-A Bu qanchaga?" in by_id[kamola]["question"]["text"]
    for other in (bobur, dilnoza):
        assert by_id[other]["may_be_waiting"] is False
        assert "question" not in by_id[other]
    assert result.data["conversations"][0]["conversation_id"] == kamola
    assert "SYNTH-B Qachon?" not in json.dumps(result.data)


async def test_c1_a_catch_up_puts_a_chat_whose_contact_asked_last_under_needs_attention(
    senv: Env,
) -> None:
    a, b = await seed(senv)  # A asks "...uchrashamizmi?" and is waiting; B states a fact
    fake = FakeLLM()
    fake.parsed = [output(), output()]  # summaries that see nothing to answer
    locale = await owner_locale(senv)

    result = await catch_up(senv, fake, since="2026-10-18 08:30", until=None)

    by_id = {e["conversation_id"]: e for e in result.data["conversations"]}
    assert (by_id[a]["importance"], by_id[a]["may_be_waiting"]) == ("needs_attention", True)
    assert by_id[a]["waiting_reason"] == owner_texts.MAY_BE_WAITING[locale]
    # The question itself, as the attention list shows it: wrapped, citable.
    asked = await stored_id(senv, CONTACT, 11)
    question = by_id[a]["question"]
    assert question["message_id"] == asked
    assert question["text"].startswith('<untrusted source="contact" trust="untrusted">')
    assert TEXT_A in question["text"]
    assert (result.citable[asked].conversation_id, result.citable[asked].direction) == (
        a,
        "contact",
    )
    assert (by_id[b]["importance"], by_id[b]["may_be_waiting"]) == ("other", False)
    assert "question" not in by_id[b]
    assert set(result.citable) == {asked}
    assert [e["conversation_id"] for e in result.data["conversations"]] == [a, b]


async def test_c1_the_limit_counts_cut_chats_that_may_be_waiting_or_were_not_checked(
    senv: Env,
) -> None:
    """What the limit cuts is counted honestly: a cut chat with a question
    may be waiting; a cut chat nobody checked is unknown, never "not waiting"."""
    await connect(senv, date=ts(-60))
    await grant_managed(senv)
    await chat(senv, CONTACT, "Kamola", (11, CONTACT, "SYNTH-A Kelasizmi?", 0))
    await chat(senv, OTHER_CONTACT, "Bobur", (21, OTHER_CONTACT, "SYNTH-B Qachon?", 1))
    await chat(senv, THIRD_CONTACT, "Dilnoza", (31, THIRD_CONTACT, "SYNTH-C rahmat", 2))
    kamola = await conversation_id(senv, CONTACT)

    result = await attention(senv, None, limit=1)

    [entry] = result.data["conversations"]
    assert entry["conversation_id"] == kamola  # among the waiting, waiting longest
    assert (
        result.data["not_listed"],
        result.data["not_listed_may_be_waiting"],
        result.data["not_listed_not_checked"],
    ) == (2, 1, 1)
    note = result.data["note_not_listed"]
    assert "2 more chat(s)" in note
    assert "1 may be waiting" in note
    assert "1 not checked" in note


@pytest.mark.parametrize("legacy", [False, True], ids=["request", "legacy_v4_promise"])
async def test_c1_the_limit_ranks_a_stored_summary_on_its_checked_verdict(
    senv: Env, legacy: bool
) -> None:
    """Before the limit, a fresh stored summary is ranked on the verdict the
    owner will see: P9, on the attribution-checked summary. A v1-v4 owner
    "promise" citing only the contact is the contact's open request (D96), so
    that chat outranks a newer one nobody checked."""
    await connect(senv, date=ts(-60))
    await grant_managed(senv)
    await chat(senv, CONTACT, "Kamola", (11, CONTACT, "SYNTH-A Hujjatlarni yuborib bering.", 0))
    kamola = await conversation_id(senv, CONTACT)
    fake = FakeLLM()

    def asked_for_documents(request: LLMRequest) -> dict[str, Any]:
        return {
            **output(needs_reply=False, reason="SYNTH nothing expected"),
            "commitments": [
                commitment("owner", "requested", "SYNTH documents", sides(request)["CONTACT"][:1])
            ],
        }

    fake.parsed = [asked_for_documents]
    assert (await summarizer(senv, fake).refresh(kamola)).status is SummaryStatus.REFRESHED
    if legacy:
        # As a v1-v4 row was stored: the item has no status (read as a promise).
        async with senv.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE app.conversation_summaries SET summary = jsonb_set(summary, "
                    "'{commitments,0}', (summary->'commitments'->0) - 'status') "
                    "WHERE conversation_id = :c AND kind = 'rolling'"
                ),
                {"c": kamola},
            )
    await chat(senv, OTHER_CONTACT, "Bobur", (21, OTHER_CONTACT, "SYNTH-B rahmat", 5))
    locale = await owner_locale(senv)

    result = await attention(senv, None, limit=1)

    [entry] = result.data["conversations"]
    assert entry["conversation_id"] == kamola
    assert (entry["check"], entry["needs_reply"], entry["may_be_waiting"]) == (
        "model",
        True,
        False,
    )
    assert entry["reason"] == owner_texts.STILL_OPEN[locale]
    assert (
        result.data["not_listed"],
        result.data["not_listed_may_be_waiting"],
        result.data["not_listed_not_checked"],
    ) == (1, 0, 1)


async def test_c1_unanswered_keeps_the_newest_chats_under_its_cap(senv: Env) -> None:
    """Past the cap the chats waiting longest are left out, never the newest
    (the owner's step-7 chat); the rest still come waiting longest first."""
    await connect(senv, date=ts(-60))
    await grant_managed(senv)
    await chat(senv, CONTACT, "Kamola", (11, CONTACT, "SYNTH-A one", 0))
    await chat(senv, OTHER_CONTACT, "Bobur", (21, OTHER_CONTACT, "SYNTH-B two", 1))
    await chat(senv, THIRD_CONTACT, "Dilnoza", (31, THIRD_CONTACT, "SYNTH-C three", 2))
    bobur = await conversation_id(senv, OTHER_CONTACT)
    dilnoza = await conversation_id(senv, THIRD_CONTACT)
    account = await account_id(senv)

    async with senv.factory() as session:
        found = await waiting.unanswered(session, account, since=AT - timedelta(hours=1), limit=2)

    assert [c.conversation_id for c in found] == [bobur, dilnoza]


async def test_p9_an_open_request_keeps_needs_reply_on_after_a_thank_you(senv: Env) -> None:
    """P9 (owner, 2026-10-27; E1 c30): the contact asked for help with a SIM
    card, the owner answered other things, the contact said thanks, and the
    model turned needs_reply off. The open request keeps it on. It is computed
    when read, so the stored summary keeps the model's own verdict."""
    await connect(senv, date=ts(-60))
    await grant_managed(senv)
    await chat(
        senv,
        CONTACT,
        "Daniel",
        (11, CONTACT, "SYNTH-A SIM karta olishga yordam berasizmi? Dollar olib kelaymi?", 0),
        (12, OWNER, "SYNTH-A owner words", 5),
        (13, CONTACT, "SYNTH-A Zo‘r, rahmat", 6),
    )
    daniel = await conversation_id(senv, CONTACT)
    fake = FakeLLM()

    def thanked(request: LLMRequest) -> dict[str, Any]:
        return {
            **output(needs_reply=False, reason="SYNTH thanked"),
            "commitments": [
                commitment("owner", "requested", "SYNTH SIM card", sides(request)["CONTACT"][:1])
            ],
        }

    fake.parsed = [thanked]
    locale = await owner_locale(senv)

    result = await attention(senv, fake)

    [entry] = result.data["conversations"]
    assert entry["conversation_id"] == daniel
    assert (entry["check"], entry["needs_reply"]) == ("model", True)
    assert entry["reason"] == owner_texts.STILL_OPEN[locale]
    assert entry["may_be_waiting"] is False  # the question came before the owner's answer
    assert [c["status"] for c in entry["requests_to_owner"]] == ["requested"]
    assert result.data["waiting_count"] == 1
    stored = await rolling(senv, daniel)
    assert stored is not None
    assert stored.summary["needs_reply"] is False  # the model's own verdict, unchanged
    ctx = await tool_context(senv, None)
    arguments = GetConversationArgs(
        conversation_id=daniel, contact_id=None, cursor=None, limit=None
    )
    view = (await get_conversation_tool(ctx, arguments)).data["summary"]
    assert view["needs_reply"] is True
    assert view["needs_reply_reason"] == owner_texts.STILL_OPEN[locale]


# ============================================================ invalidation
async def test_a_deleted_message_drops_the_summaries_that_covered_it(senv: Env) -> None:
    """PRD §20: deleted messages are not kept — not in a summary either."""
    a, b = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo, echo, echo]
    summaries = summarizer(senv, fake)
    await summaries.refresh(a)
    await summaries.refresh(b)
    await catch_up(senv, fake, since="2026-10-18 08:30", until=None, conversation=a)
    assert {(row.conversation_id, row.kind) for row in await summary_rows(senv)} == {
        (a, "rolling"),
        (b, "rolling"),
        (a, "period"),
    }

    await deliver(senv, deleted_messages(message_ids=[11]))

    assert {(row.conversation_id, row.kind) for row in await summary_rows(senv)} == {(b, "rolling")}
    assert await flag(senv, a) is not None  # rebuilt from what is retained
    fake.parsed.append(echo)
    await summaries.refresh(a)
    rebuilt = summary_requests(fake)[-1]
    assert TEXT_A not in everything(rebuilt)
    assert TEXT_A2 in everything(rebuilt)


async def test_deleting_a_message_the_summary_never_saw_keeps_it(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo]
    await summarizer(senv, fake).refresh(a)
    await deliver(senv, business_message(message_id=16, message_text="SYNTH-A late", date=ts(50)))

    await deliver(senv, deleted_messages(message_ids=[16]))

    assert await rolling(senv, a) is not None


# ============================================================ through the agent
async def test_the_owner_agent_catches_up_and_the_run_is_tainted(senv: Env) -> None:
    """End to end through the owner loop: the tool is offered with its flag
    and `ai_managed`, its summaries are separate `summarize` runs, its
    result (model text from contact chats) taints the run, and the reply
    turn records the chats it drew on."""
    a, b = await seed(senv)
    turn = await ask(senv, "Men o'qiyotganimda nima bo'ldi?")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "summarize_period",
                args(since="2026-10-18 08:30", until=None, contact_id=None, conversation_id=None),
            )
        ),
        text_response("Kamola savol berdi."),
    )
    fake.parsed = [echo, echo]
    channel = FakeChannel()

    outcome = await runtime(senv, fake, channel, flags=SUMMARY_FLAGS, with_summaries=True).run(turn)

    assert outcome.run_id is not None
    owner_calls = [r for r in fake.requests if r.purpose == "owner_request"]
    assert "summarize_period" in {tool.name for tool in owner_calls[0].tools}
    assert len(summary_requests(fake)) == 2
    async with senv.factory() as session:
        run = await session.get(AgentRun, outcome.run_id)
        assert run is not None
        assert run.tainted is True
        refs = (
            await session.execute(
                text(
                    "SELECT refs FROM app.control_turns WHERE role = 'assistant' "
                    "ORDER BY id DESC LIMIT 1"
                )
            )
        ).scalar_one()
    assert set(refs["conversation_ids"]) == {a, b}


async def test_another_accounts_conversation_is_not_found(migrated_url: str) -> None:
    other = 7000000002
    async with environment(migrated_url, owner_ids=frozenset({OWNER, other})) as env:
        await seed(env)
        await deliver(env, private_message(other, "/start"))
        await deliver(env, private_message(other, "yes"))
        await deliver(env, connection_update_for(other))
        await deliver(
            env,
            business_message(
                "synthetic-conn-b",
                from_id=7000000077,
                chat_id=7000000077,
                message_text="OTHER-SYNTH-secret?",
                date=ts(1),
            ),
        )
        theirs = await conversation_id(env, 7000000077)
        their_contact = await one_id(
            env, "SELECT id FROM app.contacts WHERE telegram_user_id = 7000000077"
        )
        fake = FakeLLM()
        for scope in ({"conversation": theirs}, {"contact_id": their_contact}):
            with pytest.raises(LookupError):
                await catch_up(env, fake, since="2026-10-18 08:30", until=None, **scope)
        assert fake.requests == []
        # Their contact's question is never evidence for this account (C1 rule).
        their_account = await one_id(
            env, "SELECT account_id FROM app.conversations WHERE id = :c", c=theirs
        )
        async with env.factory() as session:
            assert await waiting.waiting_questions(session, await account_id(env), [theirs]) == {}
            assert set(await waiting.waiting_questions(session, their_account, [theirs])) == {
                theirs
            }
        listed = (await attention(env, None)).data["conversations"]
        assert theirs not in {e["conversation_id"] for e in listed}
        assert "OTHER-SYNTH" not in json.dumps(listed)


def connection_update_for(user_id: int) -> dict[str, Any]:
    from tests.integration.business_env import connection_update

    return connection_update(user_id=user_id, connection_id="synthetic-conn-b", date=ts(-60))


async def test_a_repeated_catch_up_call_id_summarizes_once(senv: Env) -> None:
    await seed(senv)
    turn = await ask(senv, "Nima bo'ldi?")
    call = (
        "same",
        "summarize_period",
        args(since="2026-10-18 08:30", until=None, contact_id=None, conversation_id=None),
    )
    fake = FakeLLM(tool_response(call), tool_response(call), text_response("ok"))
    fake.parsed = [echo, echo]

    await runtime(senv, fake, FakeChannel(), flags=SUMMARY_FLAGS, with_summaries=True).run(turn)

    assert len(summary_requests(fake)) == 2  # two chats, once each
    owner_calls = [r for r in fake.requests if r.purpose == "owner_request"]
    assert len(owner_calls) == 3


async def test_without_managed_consent_the_summary_tools_are_blocked_stubs(senv: Env) -> None:
    """Invariant 9. S03.7 B5 (D104): without `ai_managed` the summary tools
    are offered only as blocked stubs — a call makes no summary and reads
    nothing — and nothing managed reaches the model."""
    await seed(senv, managed=False)
    turn = await ask(senv, "Nima bo'ldi?")
    fake = FakeLLM(
        tool_response(("c1", "get_attention_list", args(active_within_days=None, limit=None))),
        text_response("I cannot see your chats."),
    )

    await runtime(senv, fake, FakeChannel(), flags=SUMMARY_FLAGS, with_summaries=True).run(turn)

    offered = {tool.name for tool in fake.requests[0].tools}
    assert {"summarize_period", "get_attention_list", "get_conversation"} <= offered
    [blocked] = [
        json.loads(item.output)
        for item in fake.requests[1].items
        if isinstance(item, ToolResultItem)
    ]
    assert blocked == {"status": CHAT_ACCESS_OFF, "reason": CHAT_ACCESS_OFF_REASON}
    assert summary_requests(fake) == []
    assert "SYNTH-" not in json.dumps([everything(r) for r in fake.requests])
    assert not any(request.contains_managed() for request in fake.requests)


# ============================================================ retention
async def test_period_summaries_expire_after_thirty_days_rolling_ones_stay(senv: Env) -> None:
    """§10.4: a period summary is a catch-up cache (30 days); the rolling
    summary is memory and stays until deleted (R-07)."""
    from assistant.privacy.retention import sweep_all

    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [echo, echo]
    await summarizer(senv, fake).refresh(a)
    await catch_up(senv, fake, since="2026-10-18 08:30", until=None, conversation=a)

    senv.clock.advance(days=29)
    assert (await sweep_all(senv.factory, senv.clock)).period_summaries == 0
    senv.clock.advance(days=2)
    report = await sweep_all(senv.factory, senv.clock)

    assert report.period_summaries == 1
    assert [(row.conversation_id, row.kind) for row in await summary_rows(senv)] == [(a, "rolling")]
