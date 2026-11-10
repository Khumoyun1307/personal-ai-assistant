"""Deliberately bad transport evidence must fail the journey safety gate."""

import pytest

from tests.integration.business_env import OWNER
from tests.journeys.harness import Journey
from tests.journeys.models import CardCheck
from tests.scenarios.p2_drive import run_queued

pytestmark = [pytest.mark.integration, pytest.mark.journey]

BODY = "CANARY-SYNTH exact approved body."


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
