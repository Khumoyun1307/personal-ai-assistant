"""Deliberately bad transport evidence must fail the journey safety gate."""

import pytest
from sqlalchemy import select

from assistant.accounts.models import ControlTurn
from assistant.agent import prompt_registry
from assistant.llm.port import TextItem
from tests.fakes.llm import text_response
from tests.integration.business_env import OWNER
from tests.journeys.harness import JOURNEY_FLAGS, Journey
from tests.journeys.models import CardCheck
from tests.scenarios.p2_drive import run_queued

pytestmark = [pytest.mark.integration, pytest.mark.journey]

BODY = "CANARY-SYNTH exact approved body."


async def test_baseline_dictation_selects_source_from_actual_ingested_owner_turn(
    journey: Journey,
) -> None:
    body = "X1 Max — Oʻzbek!\nA'B / A’B 🙂  "
    row = await journey.dictate(body)
    request = await journey.request()
    assert len(journey.turns) == 1
    [source] = request.sources
    assert source.purpose == "literal_body"
    assert source.span.turn_id == journey.turns[0]
    assert source.span.field == "text"
    async with journey.s.env.factory() as session:
        turns = list(
            await session.scalars(
                select(ControlTurn).where(
                    ControlTurn.account_id == journey.s.account_id,
                    ControlTurn.role == "owner",
                )
            )
        )
    [turn] = turns
    assert turn.id == journey.turns[0]
    assert turn.text is not None
    assert source.span.start > 0
    assert source.span.end == len(turn.text)
    assert turn.text[source.span.start : source.span.end] == body
    assert source.span.field_sha256 == journey.text_hash(turn.text)
    assert row.text == body
    assert request.action_id == row.id
    assert request.conversation_id == journey.chats["k"]
    assert journey.outputs()[-1]["proposal_status"] == "staged"
    # Selection precedes runtime context loading: the fake model uses actual
    # visible C1 references, rather than creating a request during its call.
    context = "\n".join(
        item.text for item in journey.s.fake.requests[0].items if isinstance(item, TextItem)
    )
    assert f'"source_id": {request.source_ids[0]}' in context
    await journey.check(CardCheck(alias="k", text=body, origin="owner_dictated"))


async def test_request_lookup_does_not_fabricate_missing_foundation(journey: Journey) -> None:
    await journey.owner("CANARY-SYNTH advice only.", text_response("CANARY-SYNTH advice."))
    proposal = journey.source_proposal()
    assert callable(proposal)
    with pytest.raises(AssertionError, match="expected persisted request id, actual None"):
        await proposal(journey.s.fake.requests[-1])
    assert await journey.rows() == []


async def test_main_revision_adapter_does_not_fall_back_to_legacy_writer(
    journey: Journey,
) -> None:
    j = journey
    row = await j.dictate(BODY)
    request = j.s.fake.requests[-1]
    revision = next(tool for tool in request.tools if tool.name == "revise_draft")
    assert "expected_version" in revision.parameters["properties"]
    script = j.revision("k", "CANARY-SYNTH main-model rewrite.")
    assert callable(script)
    # H10 is absent here. A flat-schema call must require its real read fence,
    # rather than enqueue a legacy writer and emit arguments the run rejects.
    with pytest.raises(AssertionError, match="expected at least one context fence"):
        await script(request)
    assert j.s.fake.parsed == []
    unchanged = await j.action("k")
    assert unchanged.version == row.version
    assert unchanged.text == BODY


@pytest.mark.parametrize(
    "journey_flags",
    [JOURNEY_FLAGS, JOURNEY_FLAGS - {"tool.propose_owner_message"}],
    ids=["main-writer", "legacy-writer"],
)
async def test_restart_and_manifest_keep_selected_writer_flags(
    journey: Journey, journey_flags: frozenset[str]
) -> None:
    j = journey
    await j.dictate(BODY)
    await j.restart()
    await j.owner("CANARY-SYNTH advice only.", text_response("CANARY-SYNTH advice."))
    request = j.s.fake.requests[-1]
    offered = {tool.name for tool in request.tools}
    revision = next(tool for tool in request.tools if tool.name == "revise_draft")
    if "tool.propose_owner_message" in journey_flags:
        selected = prompt_registry.load("owner_request", "v11")
        assert "propose_owner_message" in offered
        assert "propose_reply" not in offered
        assert "expected_version" in revision.parameters["properties"]
    else:
        selected = prompt_registry.load("owner_request", "v10")
        assert "propose_owner_message" not in offered
        assert "propose_reply" in offered
        assert "literal" in revision.parameters["properties"]
    assert request.instructions.startswith(selected.text)
    assert request.prompt_cache_key == f"owner_request:{selected.label}"
    assert (await j.runs())[-1].prompt_version == selected.label
    assert (await j.manifest())["flags"] == sorted(journey_flags)


async def test_card_check_rejects_expected_body_only_in_unrelated_reply(journey: Journey) -> None:
    row = await journey.dictate(BODY)
    await journey.check(CardCheck(alias="k", text=BODY, origin="owner_dictated"))
    sender = journey.s.env.sender
    [card] = [
        item for item in sender.delivered_control_sends if item["message_id"] == row.card_message_id
    ]
    card["text"] = card["text"].replace(BODY, "CANARY-SYNTH wrong displayed body.")
    await sender.send_message(chat_id=OWNER, text=BODY)
    with pytest.raises(AssertionError, match="presented card body: expected"):
        await journey.check(CardCheck(alias="k", text=BODY, origin="owner_dictated"))


async def test_card_check_rejects_wrong_latest_edit_despite_correct_old_card(
    journey: Journey,
) -> None:
    row = await journey.dictate(BODY)
    await journey.check(CardCheck(alias="k", text=BODY, origin="owner_dictated"))
    sender = journey.s.env.sender
    [card] = [
        item for item in sender.delivered_control_sends if item["message_id"] == row.card_message_id
    ]
    assert row.card_message_id is not None
    await sender.edit_message_text(
        chat_id=OWNER,
        message_id=row.card_message_id,
        text=card["text"].replace(BODY, f"{BODY} CANARY-SYNTH unwanted suffix."),
        reply_markup=card["reply_markup"],
    )
    with pytest.raises(AssertionError, match="presented card body: expected"):
        await journey.check(CardCheck(alias="k", text=BODY, origin="owner_dictated"))


@pytest.mark.parametrize("field", ["recipient", "authorship"])
async def test_card_check_rejects_wrong_displayed_recipient_or_authorship(
    journey: Journey, field: str
) -> None:
    row = await journey.dictate(BODY)
    await journey.check(CardCheck(alias="k", text=BODY, origin="owner_dictated"))
    [card] = [
        item
        for item in journey.s.env.sender.delivered_control_sends
        if item["message_id"] == row.card_message_id
    ]
    if field == "recipient":
        card["text"] = card["text"].replace("Kamola Ziyo", "Nora Vale")
    else:
        _, rest = card["text"].split("\n", 1)
        card["text"] = f"📝 <b>Assist draft</b>\n{rest}"
    with pytest.raises(AssertionError, match=f"presented card {field}: expected"):
        await journey.check(CardCheck(alias="k", text=BODY, origin="owner_dictated"))


async def test_at_most_once_rejects_two_sends_for_one_of_two_claimed_actions(
    journey: Journey, monkeypatch: pytest.MonkeyPatch
) -> None:
    j = journey
    first = await j.dictate(BODY)
    await j.directory("n", "Nora Vale")
    await j.dictate("CANARY-SYNTH second approved body.", alias="n", chat="n")
    await j.tap("k", 1, "approve")
    await j.tap("n", 1, "approve")
    sender = j.s.env.sender
    send = sender.send_plain_message

    async def duplicate_first_skip_second(
        *, chat_id: int, text: str, business_connection_id: str
    ) -> dict[str, object]:
        if text != BODY:
            return {"message_id": 9000, "chat": {"id": chat_id}}
        await send(chat_id=chat_id, text=text, business_connection_id=business_connection_id)
        return await send(chat_id=chat_id, text=text, business_connection_id=business_connection_id)

    with monkeypatch.context() as patch:
        patch.setattr(sender, "send_plain_message", duplicate_first_skip_second)
        await run_queued(j.s, "actions.execute")
    assert await j.audit("k", "action_claimed") == 1
    assert await j.audit("n", "action_claimed") == 1
    assert len(j.s.business_sends()) == 2
    approved = await j.action("k")
    assert approved.id == first.id
    assert all(
        j.text_hash(call["text"]) == approved.approved_text_hash for call in sender.plain_sends
    )
    try:
        with pytest.raises(AssertionError, match="calls per action attempt: expected at most 1"):
            await j.assert_approved_attempts()
    finally:
        # Remove only the deliberately injected duplicate; teardown still
        # checks the first real call and its committed approval/attempt.
        del sender.plain_sends[1:]


async def test_at_most_once_accepts_identical_bodies_on_distinct_action_attempts(
    journey: Journey,
) -> None:
    j = journey
    await j.dictate(BODY)
    await j.directory("n", "Nora Vale")
    await j.dictate(BODY, alias="n", chat="n")
    await j.tap("k", 1, "approve")
    await j.tap("n", 1, "approve")
    await run_queued(j.s, "actions.execute")
    assert len(j.s.business_sends()) == 2
    await j.assert_approved_attempts()
