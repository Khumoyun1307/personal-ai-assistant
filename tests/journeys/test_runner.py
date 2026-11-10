"""Exercise the closed DSL itself, including real inbox edit/delete and restart."""

import pytest
from sqlalchemy import select

from assistant.conversations.models import Message
from tests.journeys.harness import Journey
from tests.journeys.locales import TEXT
from tests.journeys.models import LOCALES, JourneyCase, Locale

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.parametrize("locale", LOCALES)
async def test_closed_case_runs_actual_successive_turns_and_events(
    journey: Journey, locale: Locale
) -> None:
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
    assert len(journey.turns) == 2
    assert len(await journey.runs()) == 2
    message_id = await journey.message_id("book")
    async with journey.s.env.factory() as session:
        row = await session.scalar(select(Message).where(Message.id == message_id))
        assert row is not None
        assert row.deleted_at is not None
        assert row.text is None
