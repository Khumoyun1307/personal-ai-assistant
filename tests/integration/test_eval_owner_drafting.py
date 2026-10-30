"""PF-16b: the `owner_drafting` target over the real owner runtime, drafter and
card machinery, on a real PostgreSQL, with `FakeLLM` only.

Small synthetic cases built here; nothing calls a real model or Telegram, and
nothing is sent as the owner (the target's sender refuses business calls).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import pytest
from evals.budget import BudgetedLLMClient, CallLedger
from evals.owner_drafting import (
    JUDGE_PURPOSE,
    RUBRIC_ID,
    OwnerDraftingCase,
    OwnerDraftingTarget,
)
from evals.targets import RunContext, Status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.actions.models import Action
from assistant.agent import prompt_registry
from assistant.conversations.models import Contact, Conversation
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import SystemState
from assistant.llm.port import LLMRequest, LLMResponse
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.scenarios.p2_drive import owner_draft_result
from tests.unit.test_owner_drafting_dataset import card_expectation, case_record

pytestmark = pytest.mark.integration

LUNA = "gpt-6-luna"
LIVE_TEXT = "SYNTH Ertaga kelaman."


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    sessions = build_session_factory(engine)
    async with sessions() as session:
        before = {a for (a,) in (await session.execute(select(Account.id))).all()}
        barrier = await session.get(SystemState, "ingestion_barrier")
        saved = None if barrier is None else dict(barrier.value)
    try:
        yield sessions
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(Account).where(Account.id.not_in(before or {0})))
            if saved is None:
                await session.execute(
                    delete(SystemState).where(SystemState.key == "ingestion_barrier")
                )
            else:
                row = await session.get(SystemState, "ingestion_barrier")
                assert row is not None
                row.value = saved
        await engine.dispose()


def cases() -> list[OwnerDraftingCase]:
    identity = case_record(id="od01", category="identity", turn="Isming nima?")
    identity["expect"].update(
        tools_called=[],
        tools_not_called=["propose_reply", "propose_message", "revise_draft"],
        card=None,
        reply_includes=["Alviro"],
        reply_excludes=["ChatGPT"],
    )
    dictate = case_record(id="od02")  # "Dilnozaga yoz: SYNTH ertaga keldim"
    compose = case_record(
        id="od03",
        category="compose",
        turn="Dilnozaga seminar haqida yangi xabar yozib ber, SYNTH payshanba",
    )
    compose["expect"].update(
        tools_called=["propose_reply"],
        card=card_expectation(origin="agent_proposal", mode="new_message", includes=["payshanba"]),
        judge="SYNTH the message mentions Thursday's seminar",
    )
    append = case_record(
        id="od04",
        category="append",
        turn="Qoralamaga qo'sh: SYNTH oldindan rahmat",
        live_draft={"chat": "a", "origin": "agent_proposal", "text": LIVE_TEXT},
        history=[
            {"role": "owner", "text": "SYNTH javob yoz"},
            {"role": "assistant", "text": "SYNTH karta tayyor"},
        ],
    )
    append["expect"].update(
        tools_called=["revise_draft"],
        card=card_expectation(same_card=True, keeps_prefix=True, includes=["oldindan rahmat"]),
    )
    advice = case_record(id="od05", category="advice", turn="Dilnozaga nima yozsam bo'ladi?")
    advice["expect"].update(
        tools_called=[],
        tools_not_called=["propose_reply", "propose_message", "revise_draft"],
        card=None,
    )
    return [
        OwnerDraftingCase.model_validate_json(json.dumps(r))
        for r in (identity, dictate, compose, append, advice)
    ]


def calling(name: str, **args: Any) -> Callable[[LLMRequest], Any]:
    """A tool call on the case's chat: its id is looked up for the run's account."""

    async def step(request: LLMRequest) -> LLMResponse:
        async with SESSIONS[0]() as session:
            conversation_id = await session.scalar(
                select(Conversation.id)
                .join(Contact, Contact.id == Conversation.contact_id)
                .where(Conversation.account_id == request.account_id)
            )
        payload = {"conversation_id": conversation_id, **args}
        return tool_response(("call_1", name, json.dumps(payload)))

    return step


SESSIONS: list[async_sessionmaker[AsyncSession]] = []


def by_purpose(request: LLMRequest) -> dict[str, Any]:
    if request.purpose == JUDGE_PURPOSE:
        return {"meets": True, "reason": "SYNTH mentions Thursday"}
    assert request.purpose == "draft_reply"
    return owner_draft_result(language="uz-Latn", text="SYNTH Payshanba kuni seminar bor.")


async def test_the_target_runs_the_owner_runtime_and_scores_each_case(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    SESSIONS[:] = [factory]
    llm = FakeLLM(
        # od01 identity: a plain answer.
        text_response("Men Alviro, sizning AI yordamchingizman."),
        # od02 dictate: the owner's words, word for word.
        calling("propose_message", contact_id=None, text="SYNTH ertaga keldim"),
        text_response("Karta tasdiqlashingizni kutmoqda."),
        # od03 compose: a new message through draft_owner.
        calling(
            "propose_reply",
            contact_id=None,
            mode="new_message",
            answered_message_id=None,
            guidance=None,
            min_words=None,
            max_words=None,
        ),
        text_response("Karta tayyor."),
        # od04 append: the owner's own words added to the live card, no model draft.
        calling(
            "revise_draft",
            action_id=None,
            append_text="SYNTH oldindan rahmat",
            min_words=None,
            max_words=None,
        ),
        text_response("Qoralama yangilandi."),
        # od05 advice, but the model stages a card anyway: a routing failure.
        calling(
            "propose_reply",
            contact_id=None,
            mode="reply",
            answered_message_id=None,
            guidance=None,
            min_words=None,
            max_words=None,
        ),
        text_response("Karta tayyor."),
    )
    llm.parsed.extend([by_purpose] * 3)
    budgeted = BudgetedLLMClient(llm, cap_usd=Decimal(1000), ledger=CallLedger())
    target = OwnerDraftingTarget(cases(), draft_effort="medium", session_factory=factory)

    result = await target.run(RunContext(llm=budgeted, model=LUNA, effort="medium"))

    details = {d["id"]: d for d in result.details}
    assert (result.items, result.items_planned, result.items_total) == (5, 5, 5)
    assert {k: d["problems"] for k, d in details.items()} == {
        "od01": [],
        "od02": [],
        "od03": [],
        "od04": [],
        "od05": ["routing:forbidden_tool:propose_reply", "routing:unexpected_card"],
    }
    assert details["od02"]["new_cards"][0]["origin"] == "owner_dictated"
    assert details["od03"]["new_cards"][0]["answered"] is None
    assert details["od03"]["judge"] == {
        "screened": True,
        "meets": True,
        "reason": "SYNTH mentions Thursday",
        "error": None,
    }
    live = details["od04"]
    assert live["new_cards"] == []
    assert live["live_after"]["action_id"] == live["live_before"]["action_id"]
    assert live["live_after"]["version"] == 2
    assert live["live_after"]["text"] == f"{LIVE_TEXT} SYNTH oldindan rahmat"
    assert [t["tool"] for t in live["tools"]] == ["revise_draft"]
    # Both efforts reach the model: owner_request and draft_owner alike.
    efforts = {(r.purpose, r.effort) for r in llm.requests}
    assert ("owner_request", "medium") in efforts
    assert ("draft_reply", "medium") in efforts
    assert result.prompts == (
        prompt_registry.active("owner_request").label,
        prompt_registry.active("draft_owner").label,
        RUBRIC_ID,
    )
    for metric in result.metrics:
        assert metric.gate_status() is Status.INFO, metric.key
    passed = next(m for m in result.metrics if m.key == "case_passed")
    assert (passed.all_items.numerator, passed.all_items.denominator) == (4, 5)
    assert passed.failed_items == ("od05",)
    # Each case ran under its own account; nothing was approved or sent.
    async with factory() as session:
        states = (await session.scalars(select(Action.state))).all()
    assert "approved" not in states
    assert "succeeded" not in states


def test_the_estimate_plans_the_agent_loop_drafts_and_the_judge() -> None:
    target = OwnerDraftingTarget(cases())
    plan = target.plan(RunContext(model=LUNA))
    purposes = [call.purpose for call in plan]
    assert purposes.count(JUDGE_PURPOSE) == 1  # only od03 is judged
    assert purposes.count("owner_request") == 3 * 5
    assert purposes.count("draft_reply") == 4 * 5
    assert len(target.plan(RunContext(model=LUNA, limit=1))) < len(plan)
    assert [c.id for c in target.named(["od03", "od01"])] == ["od01", "od03"]
    with pytest.raises(ValueError, match="whole set"):
        target.named([c.id for c in cases()])
    with pytest.raises(ValueError, match="unknown"):
        target.named(["od99"])
    assert target.label("low") == "effort-low"
    assert OwnerDraftingTarget(cases(), draft_effort="low").label("medium") == (
        "effort-medium-draft-low"
    )
