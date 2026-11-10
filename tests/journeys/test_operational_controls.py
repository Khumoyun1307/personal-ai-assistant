"""I28: language changes reach controls beside an ordinary owner conversation."""

import pytest

from assistant.core.control_texts import text
from tests.fakes.llm import text_response
from tests.integration.business_env import OWNER, deliver, private_message
from tests.journeys.harness import Journey
from tests.journeys.locales import TEXT
from tests.journeys.models import LOCALES, Locale

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.parametrize("locale", LOCALES)
async def test_i28_language_controls_and_owner_conversation(
    journey: Journey, locale: Locale
) -> None:
    env = journey.s.env
    await deliver(env, private_message(OWNER, f"/settings language {locale}"))
    await deliver(env, private_message(OWNER, "/pause"))
    assert env.sender.texts[-1] == text("paused", locale)
    await journey.owner("SYNTHETIC I28", text_response(TEXT[locale].choice))
    await deliver(env, private_message(OWNER, "/help"))
    assert env.sender.texts[-1] == text("help", locale)
    await deliver(env, private_message(OWNER, "/resume"))
    assert env.sender.texts[-1] == text("resumed", locale)
    await deliver(env, private_message(OWNER, "/settings language en"))
    await deliver(env, private_message(OWNER, "/help"))
    assert env.sender.texts[-1] == text("help", "en")
    assert journey.s.business_sends() == []
