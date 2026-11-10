"""J07–J10/J13/J14: ordered events inside actual running model calls."""

import asyncio
from typing import Any

import pytest

from assistant.conversations.modes import set_mode
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest
from tests.fakes.llm import text_response
from tests.journeys.harness import Journey, ModelBarrier
from tests.journeys.locales import TEXT
from tests.journeys.matrix import owner_input
from tests.journeys.models import (
    LOCALES,
    Contact,
    CountCheck,
    DeleteMessage,
    EditMessage,
    Locale,
    Restart,
)
from tests.scenarios.p2_drive import draft_result, run_queued

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.xfail(strict=True, reason="needs H10", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("event", ["arrival", "edit-base", "edit-other", "delete", "manual"])
async def test_j08_j09_source_changes_during_main_model_composition(
    journey: Journey, locale: Locale, event: str
) -> None:
    j = journey
    await j.language(locale)
    await j.event(Contact(alias="base", text="CANARY-SYNTH book question?"))
    await j.event(Contact(alias="other", text="CANARY-SYNTH related deadline."))
    base = await j.message_id("base")
    barrier = ModelBarrier()

    async def held(request: LLMRequest) -> Any:
        barrier.entered.set()
        await barrier.release.wait()
        proposal = j.generated_proposal(TEXT[locale].generated, target=base)
        assert callable(proposal)
        return await proposal(request)

    task = asyncio.create_task(
        j.owner(
            owner_input("J09" if event == "manual" else "J08", locale)[0],
            j.read(),
            held,
            text_response(TEXT[locale].outcome),
        )
    )
    try:
        await j.hold(task, barrier)
        if event in {"arrival", "manual"}:
            await j.event(
                Contact(
                    alias="interruption",
                    text="CANARY-SYNTH superseding answer.",
                    owner_manual=event == "manual",
                )
            )
        elif event == "delete":
            await j.event(DeleteMessage(alias="base"))
        else:
            await j.event(
                EditMessage(
                    alias="base" if event == "edit-base" else "other",
                    text="CANARY-SYNTH changed source.",
                )
            )
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
    await task
    assert not [row for row in await j.rows() if row.state == "awaiting_approval"]
    assert j.outputs()[-1]["status"] in {"stale", "blocked"}
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs H11", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("reject_owner_card", [False, True])
async def test_j10_late_assist_cannot_replace_or_revive_owner_intent(
    journey: Journey, locale: Locale, *, reject_owner_card: bool
) -> None:
    j = journey
    await j.language(locale)
    async with transaction(j.s.env.factory) as uow:
        await set_mode(
            uow.session,
            account_id=j.s.account_id,
            conversation_id=j.chats["k"],
            mode="assist",
            actor="owner",
            clock=j.s.env.clock,
        )
    await j.event(Contact(alias="burst", text="CANARY-SYNTH Assist question?"))
    j.s.env.clock.advance(seconds=30)
    barrier = ModelBarrier()

    async def held(_request: LLMRequest) -> dict[str, Any]:
        barrier.entered.set()
        await barrier.release.wait()
        return draft_result(text="CANARY-SYNTH obsolete Assist reply.")

    j.s.fake.parsed.append(held)
    task = asyncio.create_task(run_queued(j.s, "replies.draft"))
    try:
        await j.hold(task, barrier)
        body = owner_input("J10", locale)[0].split(": ", 1)[1]
        owner_card = await j.dictate(body)
        if reject_owner_card:
            await j.tap("k", 1, "reject")
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
    await task
    rows = await j.rows()
    assert [row.id for row in rows] == [owner_card.id]
    assert rows[0].text == body
    assert rows[0].state == ("rejected" if reject_owner_card else "awaiting_approval")
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs H10", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("deleted", [False, True])
async def test_j07_retained_older_target_is_loaded_without_latest_fallback(
    journey: Journey, locale: Locale, *, deleted: bool
) -> None:
    j = journey
    await j.language(locale)
    await j.event(Contact(alias="older", text="CANARY-SYNTH older book question?"))
    older_id = await j.message_id("older")
    for index in range(35):
        await j.event(Contact(alias=f"later-{index}", text="CANARY-SYNTH newer invoice."))
    if deleted:
        await j.event(DeleteMessage(alias="older"))
    await j.owner(
        owner_input("J07", locale)[0],
        j.read(limit=50),
        j.generated_proposal(TEXT[locale].generated, target=older_id),
        text_response(TEXT[locale].outcome),
    )
    if deleted:
        await j.check(CountCheck(artifact="actions", count=0))
        assert j.outputs()[-1]["status"] in {"not_available", "invalid_arguments", "stale"}
    else:
        row = await j.bind_card("k")
        assert row.base_message_id == older_id
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs I13", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
async def test_j13_queued_selection_sees_the_completed_first_answer(
    journey: Journey, locale: Locale
) -> None:
    from tests.integration.agent_env import request_texts

    j = journey
    await j.language(locale)
    first, second = owner_input("J13", locale)
    await j.owner(f"{TEXT[locale].choice} {TEXT[locale].brief}", text_response(TEXT[locale].choice))
    options = f"1. CANARY-SYNTH-OPTION-A\n2. {TEXT[locale].generated}"
    barrier = ModelBarrier()
    task = asyncio.create_task(j.owner(first, barrier.response(text_response(options))))
    try:
        await j.hold(task, barrier)
        second_turn = await j.owner(second, run=False)
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
    await task
    before = len(j.s.fake.requests)
    j.s.fake.queue(j.selected_suggestion(), text_response(TEXT[locale].generated))
    assert await run_queued(j.s, "agent.owner_request") == 1
    assert options in request_texts(j.s.fake.requests[before])
    request = await j.request()
    assert second_turn in {source.span.turn_id for source in request.sources}
    row = await j.bind_card("k")
    assert row.text == TEXT[locale].generated
    proof = getattr(row, "authorship", None)
    assert proof is not None
    assert proof["kind"] == "agent_proposal_selected"
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs I14", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
async def test_j14_two_interruptions_do_not_suppress_unanswered_request(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)

    async def interrupt(_request: LLMRequest) -> Any:
        raise asyncio.CancelledError

    turn = await j.owner(owner_input("J14", locale)[0], run=False)
    for _attempt in range(2):
        j.s.fake.queue(interrupt)
        with pytest.raises(asyncio.CancelledError):
            await j.s.handlers.owner_request(turn)
        await j.event(Restart())
    j.s.fake.queue(j.read(), j.generated_proposal(TEXT[locale].generated), text_response("Ready."))
    await j.s.handlers.owner_request(turn)
    await j.bind_card("k")
    assert len(await j.rows()) == 1
    runs = await j.runs()
    assert len(runs) >= 3
    assert runs[-1].status == "succeeded"
    assert not [run for run in runs[:-1] if run.status == "running"]
    await j.check(CountCheck(artifact="business_attempts", count=0))
