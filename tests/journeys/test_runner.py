"""Exercise the closed DSL itself, including real inbox edit/delete and restart."""

import json

import pytest
from sqlalchemy import select

from assistant.conversations.models import Message
from assistant.llm.port import LLMRequest, LLMResponse, TextItem
from tests.fakes.llm import text_response
from tests.journeys.grading import assert_equal
from tests.journeys.harness import Journey, call
from tests.journeys.locales import TEXT
from tests.journeys.models import (
    LOCALES,
    CountCheck,
    FactualReplyCheck,
    JourneyCase,
    Locale,
    ReplyCheck,
    Restart,
)

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.parametrize("locale", LOCALES)
async def test_closed_case_runs_actual_successive_turns_and_events(
    journey: Journey, locale: Locale
) -> None:
    started_at = journey.s.env.clock.now()
    case = JourneyCase.model_validate(
        {
            "case_id": "runner-smoke",
            "locale": locale,
            "setup": [{"kind": "directory", "alias": "k", "name": "Kamola Ziyo"}],
            "steps": [
                {"kind": "owner", "text": TEXT[locale].advice, "answer": TEXT[locale].list_answer},
                {"kind": "contact", "alias": "book", "text": "CANARY-SYNTH initial book."},
                {"kind": "edit", "alias": "book", "text": "CANARY-SYNTH edited book."},
                {"kind": "delete", "alias": "book"},
                {"kind": "advance", "seconds": 5},
                {"kind": "restart"},
                {"kind": "owner", "text": TEXT[locale].advice, "answer": TEXT[locale].list_answer},
            ],
            "deterministic_checks": [
                {"kind": "count", "artifact": "actions", "count": 0},
                {"kind": "count", "artifact": "business_attempts", "count": 0},
                {"kind": "reply", "contains": [TEXT[locale].list_answer]},
            ],
        }
    )
    await journey.run(case)
    assert_equal(len(journey.turns), 2, field="owner turn count")
    assert_equal(len(await journey.runs()), 2, field="agent run count")
    replies = [item["text"] for item in journey.s.env.sender.delivered_control_sends]
    assert_equal(replies.count(TEXT[locale].list_answer), 2, field="delivered owner reply count")
    assert_equal(
        (journey.s.env.clock.now() - started_at).total_seconds(),
        5,
        field="business clock advance (gateway waits stay local)",
    )
    message_id = await journey.message_id("book")
    async with journey.s.env.factory() as session:
        row = await session.scalar(select(Message).where(Message.id == message_id))
        assert row is not None
        assert row.deleted_at is not None
        assert row.text is None


@pytest.mark.parametrize("locale", LOCALES)
async def test_h2_explicit_reply_keeps_selected_card_after_restart(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    selected = await j.dictate(TEXT[locale].generated)
    await j.directory("n", "Nora Vale")
    other = await j.dictate("CANARY-SYNTH Nora body.", alias="n", chat="n")
    await j.event(Restart())

    def read_selected(request: LLMRequest) -> LLMResponse:
        blocks = [
            item.text
            for item in request.items
            if isinstance(item, TextItem) and item.text.startswith("Owner request continuation")
        ]
        assert_equal(len(blocks), 1, field="H2 continuation block count")
        facts = json.loads(blocks[0].split("\n", 1)[1])
        assert_equal(facts["status"], "selected", field="H2 continuation status")
        assert_equal(facts["basis"], "explicit_reply", field="H2 continuation basis")
        assert_equal(facts["action_id"], selected.id, field="H2 selected action_id")
        assert_equal(facts["action_version"], selected.version, field="H2 selected action_version")
        return call("get_draft", conversation_id=None, action_id=facts["action_id"])

    await j.owner(
        TEXT[locale].who,
        read_selected,
        text_response(TEXT[locale].choice),
        reply_to="k",
    )
    await j.check(ReplyCheck(contains=(TEXT[locale].choice,)))
    assert_equal(j.outputs()[-1]["action_id"], selected.id, field="read action_id")
    assert_equal((await j.action("n")).text, other.text, field="other card text")
    assert_equal((await j.action("n")).version, other.version, field="other card version")
    await j.check(CountCheck(artifact="actions", count=2))
    await j.check(CountCheck(artifact="business_attempts", count=0))


async def test_failed_harness_checks_report_expected_and_actual(journey: Journey) -> None:
    journey.last_reply = "CANARY-SYNTH actual reply."
    checks = (
        ReplyCheck(contains=("CANARY-SYNTH missing text.",)),
        ReplyCheck(excludes=("CANARY-SYNTH actual reply.",)),
        FactualReplyCheck(
            card="k", version=1, status="uncertain", wording="CANARY-SYNTH missing receipt."
        ),
        CountCheck(artifact="actions", count=1),
        CountCheck(artifact="business_attempts", count=1),
        CountCheck(artifact="nested_writers", count=1),
    )
    for check in checks:
        with pytest.raises(AssertionError, match=r"expected .+, actual .+"):
            await journey.check(check)
