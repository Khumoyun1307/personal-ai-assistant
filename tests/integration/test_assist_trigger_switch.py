"""S05.1 §3.7: the single Assist draft trigger switch (P2-O6) on the M05 side.

`actions.drafts.request_draft` (S04.2) is not on `int/m05` until CP4, so the
store's `DraftRequester` port is a recorder here; one card per burst is then
`replies.draft`'s queueing lock (S04.2), re-checked at CP4.

Required tests, findable by name:
`test_with_the_switch_on_triage_is_the_only_assist_draft_trigger`,
`test_with_the_switch_on_a_burst_still_yields_exactly_one_card`,
`test_with_the_switch_off_triage_defers_no_draft`,
`test_owner_messages_never_trigger_drafts_in_either_mode` (RG-01 extension).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.drafts import assist_trigger
from assistant.agent.triage import TriageConfig, Triager, TriageStatus
from assistant.inbox.triage_store import TriageRepository
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import gated, one_id
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    Env,
    business_message,
    connect,
    deliver,
    environment,
    grant_managed,
)

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def ts(minutes: int) -> int:
    return int((AT + timedelta(minutes=minutes)).timestamp())


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


class Requests:
    """The `DraftRequester` port, recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def __call__(self, session: AsyncSession, account_id: int, conversation_id: int) -> None:
        self.calls.append((account_id, conversation_id))


def verdict(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "language": "en",
        "needs_reply": True,
        "contains_question_to_owner": True,
        "requests_decision": False,
        "urgency": "normal",
        "deadline": None,
        "commitments": [],
        "open_questions": [],
        "resolved_task_ids": [],
        "escalation_category": "needs_owner",
        "reasons": [],
        "script": "latin",
    }
    values.update(overrides)
    return values


async def assist_chat(env: Env, *, mode: str = "assist") -> int:
    await connect(env, date=ts(-60))
    await grant_managed(env)
    await deliver(env, business_message(message_id=11, message_text="SYNTH 1?", date=ts(0)))
    conversation = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = :m WHERE id = :c"),
            {"m": mode, "c": conversation},
        )
    return conversation


def triager(env: Env, fake: FakeLLM, switch: str, requests: Requests) -> Triager:
    return Triager(
        session_factory=env.factory,
        llm=gated(env, fake),
        clock=env.clock,
        config=TriageConfig(model="gpt-6-luna"),
        store=TriageRepository(
            clock=env.clock,
            scheduler=None,
            assist_draft_trigger=switch,
            draft_requester=requests,
        ),
    )


async def run(env: Env, conversation: int, switch: str, requests: Requests, **out: Any) -> None:
    fake = FakeLLM()
    fake.parsed = [verdict(**out)]
    outcome = await triager(env, fake, switch, requests).triage(conversation)
    assert outcome.status is TriageStatus.TRIAGED


ALL_ON = frozenset({"assist.drafts", "triage.run", "triage.assist_trigger"})


async def test_with_the_switch_on_triage_is_the_only_assist_draft_trigger(env: Env) -> None:
    # The switch (F0, computed once in bootstrap): "triage" replaces
    # "ingestion", so M04's ingestion listener defers nothing.
    assert assist_trigger(ALL_ON) == "triage"
    assert assist_trigger(ALL_ON - {"triage.assist_trigger"}) == "ingestion"
    conversation = await assist_chat(env)
    requests = Requests()
    await run(env, conversation, "triage", requests)
    account = await one_id(
        env, "SELECT account_id FROM app.conversations WHERE id = :c", c=conversation
    )
    assert requests.calls == [(account, conversation)]


async def test_triage_requests_no_draft_without_need_or_outside_assist(env: Env) -> None:
    conversation = await assist_chat(env, mode="observe")
    requests = Requests()
    await run(env, conversation, "triage", requests)  # Observe: never
    await deliver(env, business_message(message_id=12, message_text="SYNTH 2", date=ts(1)))
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'assist' WHERE id = :c"),
            {"c": conversation},
        )
    await run(env, conversation, "triage", requests, needs_reply=False)
    await deliver(env, business_message(message_id=13, message_text="SYNTH 3", date=ts(2)))
    await run(env, conversation, "triage", requests, escalation_category="suspicious")
    assert requests.calls == []


async def test_with_the_switch_on_a_burst_still_yields_exactly_one_card(env: Env) -> None:
    """Three contact messages, one triage run, one draft request (then one
    card: `replies.draft` coalesces on `draft:{id}`, S04.2)."""
    conversation = await assist_chat(env)
    await deliver(env, business_message(message_id=12, message_text="SYNTH 2", date=ts(1)))
    await deliver(env, business_message(message_id=13, message_text="SYNTH 3", date=ts(2)))
    requests = Requests()
    await run(env, conversation, "triage", requests)
    # Nothing new: the next run settles without a call or a request.
    fake = FakeLLM()
    outcome = await triager(env, fake, "triage", requests).triage(conversation)
    assert outcome.status is TriageStatus.UP_TO_DATE
    assert len(requests.calls) == 1


@pytest.mark.parametrize("switch", ["off", "ingestion"])
async def test_with_the_switch_off_triage_defers_no_draft(env: Env, switch: str) -> None:
    conversation = await assist_chat(env)
    requests = Requests()
    await run(env, conversation, switch, requests)
    assert requests.calls == []


@pytest.mark.parametrize("switch", ["triage", "ingestion"])
async def test_owner_messages_never_trigger_drafts_in_either_mode(env: Env, switch: str) -> None:
    """A burst of only the owner's own answer (to an open task) is triaged,
    but asks for no draft even when the model says a reply is needed."""
    conversation = await assist_chat(env)
    requests = Requests()
    await run(env, conversation, switch, requests, needs_reply=False)
    account = await one_id(
        env, "SELECT account_id FROM app.conversations WHERE id = :c", c=conversation
    )
    source = await one_id(
        env, "SELECT id FROM app.messages WHERE conversation_id = :c", c=conversation
    )
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO app.tasks (account_id, conversation_id, kind, title, created_by, "
                "source_message_ids, first_source_message_id) "
                "VALUES (:a, :c, 'open_question', 'Synthetic', 'agent', CAST(:s AS jsonb), :f)"
            ),
            {"a": account, "c": conversation, "s": f"[{source}]", "f": source},
        )
    await deliver(
        env,
        business_message(message_id=12, from_id=OWNER, message_text="SYNTH ok", date=ts(5)),
    )
    await run(env, conversation, switch, requests)
    assert requests.calls == []
