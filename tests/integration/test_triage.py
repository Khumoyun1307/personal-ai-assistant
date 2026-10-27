"""S05.1 triage: the `triage` run over the real inbox and Postgres.

The run part (S05.1 §3.1-§3.4) is tested here against a recording
`TriageStore`: what the model is given and what reaches the store. The
store's effects (verdict row, tasks, closures, notifications) are tested
with the pipeline. Required tests, findable by name:

- `test_triage_runs_are_given_no_tools` (RG-17 extension, §6 invariants 2, 3a);
- `test_triage_sees_only_its_own_conversation` (RG-09 style, invariant 4);
- `test_without_managed_consent_no_triage_call_is_made` (RG-08 extension,
  invariant 9);
- `test_ignore_and_bot_chats_are_never_triaged`;
- `test_the_triage_trigger_ref_names_the_newest_shown_message` (NFR-05).

Synthetic text only; FakeLLM stands in for the provider (AGENTS §11).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.agent import prompt_registry
from assistant.agent.models import AgentRun
from assistant.agent.tools.registry import CapabilityTier, default_registry
from assistant.agent.triage import TriageConfig, Triager, TriageStatus
from assistant.inbox.tasks import TaskView
from assistant.inbox.triage_store import ApplyResult, TriageState, TriageVerdict
from assistant.llm.port import LLMError, LLMErrorKind, LLMRequest, StopReason, TextItem
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import gated, one_id
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    Env,
    business_message,
    connect,
    deliver,
    environment,
    grant_managed,
)

pytestmark = pytest.mark.integration

#: Message dates: 09:00 UTC on the test day, before the FakeClock's noon.
AT = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
TEXT_A = "SYNTH-A Hujjatlarni ertaga soat 10 gacha yuboraman."
TEXT_A2 = "SYNTH-A Juma kuni uchrashamizmi?"
TEXT_B = "SYNTH-B Narxni tasdiqlaysizmi?"


def ts(minutes: int) -> int:
    return int((AT + timedelta(minutes=minutes)).timestamp())


@pytest.fixture
async def tenv(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def seed(env: Env, *, managed: bool = True) -> tuple[int, int]:
    """Chat A (two contact messages from Kamola) and chat B (one from
    Bobur). Returns the two conversation ids."""
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


async def message_ids(env: Env, conversation: int) -> list[int]:
    async with env.factory() as session:
        rows = await session.execute(
            text("SELECT id FROM app.messages WHERE conversation_id = :c ORDER BY id"),
            {"c": conversation},
        )
        return [int(r[0]) for r in rows.all()]


# ------------------------------------------------------------ the store fake
@dataclass
class RecordingStore:
    """A `TriageStore` that records verdicts in memory (the real one needs
    the M05 tables)."""

    covers: dict[int, int] = field(default_factory=dict)
    open_tasks: dict[int, tuple[TaskView, ...]] = field(default_factory=dict)
    verdicts: list[TriageVerdict] = field(default_factory=list)
    settled: list[int] = field(default_factory=list)

    async def state(
        self, session: AsyncSession, *, account_id: int, conversation_id: int
    ) -> TriageState | None:
        return TriageState(
            self.covers.get(conversation_id), "normal", self.open_tasks.get(conversation_id, ())
        )

    async def apply(
        self, session: AsyncSession, verdict: TriageVerdict, *, now: datetime
    ) -> ApplyResult:
        self.verdicts.append(verdict)
        self.covers[verdict.conversation_id] = verdict.covers_through_message_id
        return ApplyResult.STORED

    async def settle(self, session: AsyncSession, *, account_id: int, conversation_id: int) -> None:
        self.settled.append(conversation_id)


def triager(env: Env, fake: FakeLLM, store: RecordingStore, **config: Any) -> Triager:
    values: dict[str, Any] = {"model": "gpt-6-luna"}
    values.update(config)
    return Triager(
        session_factory=env.factory,
        llm=gated(env, fake),
        clock=env.clock,
        config=TriageConfig(**values),
        store=store,
    )


# ------------------------------------------------------------ scripted output
def everything(request: LLMRequest) -> str:
    parts = [request.instructions]
    parts.extend(item.text for item in request.items if isinstance(item, TextItem))
    return "\n".join(parts)


def burst_text(request: LLMRequest) -> str:
    return next(
        item.text
        for item in request.items
        if isinstance(item, TextItem) and item.text.startswith("New messages to triage:")
    )


def shown_ids(request: LLMRequest) -> list[int]:
    return [int(i) for i in re.findall(r"^\[msg:(\d+)\]", everything(request), flags=re.MULTILINE)]


def verdict(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "language": "uz-Latn",
        "needs_reply": True,
        "contains_question_to_owner": True,
        "requests_decision": False,
        "urgency": "normal",
        "deadline": None,
        "commitments": [],
        "open_questions": [],
        "resolved_task_ids": [],
        "escalation_category": "needs_owner",
        "reasons": ["Savol bor"],
        "script": "latin",
    }
    values.update(overrides)
    return values


def echo(request: LLMRequest) -> dict[str, Any]:
    """A verdict whose question cites the newest shown message."""
    newest = max(shown_ids(request))
    return verdict(
        open_questions=[{"text": "Qachon?", "source_message_id": newest, "is_decision": False}]
    )


def triage_requests(fake: FakeLLM) -> list[LLMRequest]:
    return [r for r in fake.requests if r.purpose == "triage"]


async def triage_runs(env: Env) -> list[AgentRun]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(AgentRun).where(AgentRun.run_type == "triage").order_by(AgentRun.id)
                )
            )
            .scalars()
            .all()
        )


# ============================================================ the run
async def test_triage_runs_are_given_no_tools(tenv: Env) -> None:
    """RG-17 extension (§6 invariants 2, 3a): a `triage` run is one structured
    call with no tools, and the registry offers the run type nothing."""
    a, b = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [echo, echo]
    store = RecordingStore()
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.TRIAGED
    assert (await triager(tenv, fake, store).triage(b)).status is TriageStatus.TRIAGED

    requests = triage_requests(fake)
    assert len(requests) == 2
    assert all(request.tools == () for request in requests)
    runs = await triage_runs(tenv)
    assert {(r.prompt_version, r.status, r.iterations) for r in runs} == {
        (prompt_registry.active("triage").label, "succeeded", 1)
    }
    registry = default_registry()
    every_flag = frozenset(tool.release_flag for tool in registry.all())
    assert (
        registry.for_run(
            "triage",
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=every_flag,
            managed_consent=True,
        )
        == {}
    )


async def test_triage_sees_only_its_own_conversation(tenv: Env) -> None:
    """§6 invariant 4: one conversation per triage call — its messages, its
    contact's name (once, wrapped), its ids. FakeLLM inspects every prompt."""
    a, b = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [echo, echo]
    store = RecordingStore()
    await triager(tenv, fake, store).triage(a)
    await triager(tenv, fake, store).triage(b)

    first, second = triage_requests(fake)
    for request, own, other, own_text, other_text in (
        (first, "Kamola", "Bobur", "SYNTH-A", "SYNTH-B"),
        (second, "Bobur", "Kamola", "SYNTH-B", "SYNTH-A"),
    ):
        body = everything(request)
        assert own_text in body
        assert other_text not in body
        assert f'<untrusted source="contact_name" trust="untrusted">{own}</untrusted>' in body
        assert body.count(own) == 1
        assert other not in body
    assert set(shown_ids(first)) == set(await message_ids(tenv, a))
    assert set(shown_ids(second)) == set(await message_ids(tenv, b))
    assert [v.conversation_id for v in store.verdicts] == [a, b]
    assert set(store.verdicts[0].source) == set(await message_ids(tenv, a))


async def test_without_managed_consent_no_triage_call_is_made(tenv: Env) -> None:
    """RG-08 extension (invariant 9): without `ai_managed` nothing reaches the
    provider and no run row is written (a clean skip)."""
    a, _ = await seed(tenv, managed=False)
    fake = FakeLLM()
    store = RecordingStore()
    outcome = await triager(tenv, fake, store).triage(a)
    assert outcome.status is TriageStatus.NO_CONSENT
    assert fake.requests == []
    assert await triage_runs(tenv) == []
    assert store.verdicts == []


async def test_a_triage_route_whose_processor_is_not_named_in_consent_sends_nothing(
    tenv: Env,
) -> None:
    """The consent names OpenAI; a route moved to another processor's model
    is refused before any run row (SEC-02, invariant 9)."""
    a, _ = await seed(tenv)
    fake = FakeLLM()
    outcome = await triager(tenv, fake, RecordingStore(), model="claude-fable-5-1").triage(a)
    assert outcome.status is TriageStatus.NO_CONSENT
    assert fake.requests == []
    assert await triage_runs(tenv) == []


async def test_ignore_and_bot_chats_are_never_triaged(tenv: Env) -> None:
    a, b = await seed(tenv)
    async with tenv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"), {"c": a}
        )
        await session.execute(
            text(
                "UPDATE app.contacts SET is_bot = true WHERE id = "
                "(SELECT contact_id FROM app.conversations WHERE id = :c)"
            ),
            {"c": b},
        )
    fake = FakeLLM()
    store = RecordingStore()
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.SKIPPED
    assert (await triager(tenv, fake, store).triage(b)).status is TriageStatus.SKIPPED
    assert fake.requests == []
    assert await triage_runs(tenv) == []


async def test_the_triage_trigger_ref_names_the_newest_shown_message(tenv: Env) -> None:
    """NFR-05 reads triage latency from the newest message a run read
    (`split_part(trigger_ref, ':', 5)`, S05.2 §3.9)."""
    a, _ = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [echo]
    outcome = await triager(tenv, fake, RecordingStore()).triage(a)
    newest = max(await message_ids(tenv, a))
    assert outcome.through == newest
    (run,) = await triage_runs(tenv)
    assert run.trigger_ref == f"triage:conv:{a}:through:{newest}"
    assert run.trigger_ref.split(":")[4] == str(newest)


# ============================================================ what is shown
async def test_only_messages_after_the_verdict_are_the_burst(tenv: Env) -> None:
    """Messages a verdict covers are context ("already triaged"); only newer
    ones are the burst, and the stored verdict covers through the newest."""
    a, _ = await seed(tenv)
    first, second = await message_ids(tenv, a)
    store = RecordingStore(covers={a: first})
    fake = FakeLLM()
    fake.parsed = [echo]
    await triager(tenv, fake, store).triage(a)

    (request,) = triage_requests(fake)
    burst = burst_text(request)
    assert f"[msg:{second}]" in burst
    assert f"[msg:{first}]" not in burst
    assert f"[msg:{first}]" in everything(request)  # shown as an earlier message
    assert store.verdicts[0].burst_message_ids == (second,)
    assert store.verdicts[0].covers_through_message_id == second


async def test_nothing_new_settles_the_flag_without_a_call(tenv: Env) -> None:
    a, _ = await seed(tenv)
    newest = max(await message_ids(tenv, a))
    store = RecordingStore(covers={a: newest})
    fake = FakeLLM()
    outcome = await triager(tenv, fake, store).triage(a)
    assert outcome.status is TriageStatus.UP_TO_DATE
    assert store.settled == [a]
    assert fake.requests == []
    assert await triage_runs(tenv) == []


async def test_an_owner_only_burst_without_open_tasks_makes_no_call(tenv: Env) -> None:
    """Only the owner wrote since the verdict and no task could be answered:
    nothing for triage to say."""
    a, _ = await seed(tenv)
    newest = max(await message_ids(tenv, a))
    await deliver(
        tenv,
        business_message(
            message_id=13, from_id=OWNER, message_text="SYNTH-A owner reply", date=ts(10)
        ),
    )
    store = RecordingStore(covers={a: newest})
    fake = FakeLLM()
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.UP_TO_DATE
    assert fake.requests == []


async def test_an_owner_answer_is_triaged_when_a_task_is_open(tenv: Env) -> None:
    """ARCHITECTURE §6.5 step 4: the owner's manual answer is checked against
    the chat's open tasks, which are listed with their ids, titles wrapped."""
    a, _ = await seed(tenv)
    first, newest = await message_ids(tenv, a)
    await deliver(
        tenv,
        business_message(
            message_id=13, from_id=OWNER, message_text="SYNTH-A Ha, juma", date=ts(10)
        ),
    )
    task = TaskView(
        task_id=77,
        conversation_id=a,
        kind="open_question",
        title="Juma kuni uchrashuv?",
        due_at=None,
        status="open",
        source_message_ids=(newest,),
        created_at=AT,
    )
    store = RecordingStore(covers={a: newest}, open_tasks={a: (task,)})
    fake = FakeLLM()
    fake.parsed = [verdict(needs_reply=False, resolved_task_ids=[77, 78])]
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.TRIAGED
    body = everything(triage_requests(fake)[0])
    assert (
        '[task:77] open_question, no due date: <untrusted source="task_title" '
        'trust="untrusted">Juma kuni uchrashuv?</untrusted>'
    ) in body
    assert "YOU: " in burst_text(triage_requests(fake)[0])
    # 78 is not an open task of this chat: removed before the store.
    assert store.verdicts[0].resolved_task_ids == (77,)
    assert first


async def test_a_first_triage_reads_only_the_last_day(tenv: Env) -> None:
    """Without a verdict, switching triage on never turns a chat's whole
    history into a burst: only the day before its newest message."""
    a, _ = await seed(tenv)
    await deliver(
        tenv,
        business_message(message_id=14, message_text="SYNTH-A two days later", date=ts(2 * 1440)),
    )
    fake = FakeLLM()
    fake.parsed = [echo]
    store = RecordingStore()
    await triager(tenv, fake, store).triage(a)
    *old, newest = await message_ids(tenv, a)
    (request,) = triage_requests(fake)
    assert f"[msg:{newest}]" in burst_text(request)
    assert all(f"[msg:{i}]" not in burst_text(request) for i in old)
    assert store.verdicts[0].burst_message_ids == (newest,)


# ============================================================ validation
async def test_a_misattributed_commitment_never_reaches_the_store(tenv: Env) -> None:
    """A "promise of the owner" citing the contact's message is dropped, and a
    task id of no open task is removed (S05.1 §3.4)."""
    a, _ = await seed(tenv)
    first, second = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [
        verdict(
            commitments=[
                {
                    "by": "owner",
                    "text": "Hujjat yuborish",
                    "due": None,
                    "source_message_id": first,
                    "existing_task_id": None,
                },
                {
                    "by": "contact",
                    "text": "Hujjat yuborish",
                    "due": "2026-10-19 10:00",
                    "source_message_id": first,
                    "existing_task_id": 5,
                },
            ],
            open_questions=[{"text": "Juma?", "source_message_id": second, "is_decision": True}],
        )
    ]
    store = RecordingStore()
    await triager(tenv, fake, store).triage(a)
    (stored,) = store.verdicts
    assert [(t.kind, t.source_message_id, t.existing_task_id) for t in stored.tasks] == [
        ("contact_commitment", first, None),
        ("decision", second, None),
    ]
    # Owner-local (the onboarding default timezone) into UTC.
    assert stored.tasks[0].due_at is not None
    assert stored.tasks[0].due_at.tzinfo is not None


async def test_a_refused_or_invalid_triage_stores_nothing(tenv: Env) -> None:
    a, _ = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [StopReason.REFUSAL, LLMError(LLMErrorKind.INVALID_OUTPUT)]
    store = RecordingStore()
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.REFUSED
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.FAILED
    assert store.verdicts == []
    assert [(r.status, r.error_class) for r in await triage_runs(tenv)] == [
        ("refused", "refusal"),
        ("failed", "invalid_output"),
    ]


async def test_repeated_useless_failures_pause_that_chats_triage(tenv: Env) -> None:
    a, b = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [StopReason.REFUSAL, StopReason.REFUSAL, StopReason.REFUSAL]
    store = RecordingStore()
    for _ in range(3):
        assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.REFUSED
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.BACKED_OFF
    assert len(triage_requests(fake)) == 3
    # Another chat is not paused; this one is tried again after the window.
    fake.parsed = [echo, echo]
    assert (await triager(tenv, fake, store).triage(b)).status is TriageStatus.TRIAGED
    tenv.clock.advance(hours=25)
    assert (await triager(tenv, fake, store).triage(a)).status is TriageStatus.TRIAGED


async def test_a_truncated_triage_is_retried_once_with_a_larger_budget(tenv: Env) -> None:
    a, _ = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [StopReason.MAX_TOKENS, echo]
    store = RecordingStore()
    outcome = await triager(tenv, fake, store, max_output_tokens=1000).triage(a)
    assert outcome.status is TriageStatus.TRIAGED
    assert outcome.calls == 2
    assert [r.max_output_tokens for r in triage_requests(fake)] == [1000, 2000]
