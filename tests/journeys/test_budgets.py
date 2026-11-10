"""J24: parent and nested provider calls share request admission."""

from dataclasses import replace

import pytest

from assistant.agent.runtime import OwnerRequestRuntime
from tests.fakes.llm import text_response
from tests.journeys.harness import Journey, call
from tests.journeys.locales import TEXT
from tests.journeys.matrix import owner_input
from tests.journeys.models import LOCALES, CountCheck, Locale

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.xfail(strict=True, reason="needs I15", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
async def test_j24_nested_summary_calls_share_parent_token_budget_and_report_partial(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    await j.directory("n", "Nora Vale")
    runtime = j.s.handlers.owner_runtime
    assert isinstance(runtime, OwnerRequestRuntime)
    # The main tool response uses 40 tokens; one fake child uses 15.
    # At 55, a second child must not be dispatched. Configuration is the
    # existing runtime seam, not a replacement budget implementation.
    runtime._config = replace(runtime._config, run_token_budget=55)
    result = {
        "language": locale,
        "overview": TEXT[locale].generated,
        "open_questions_to_owner": [],
        "commitments": [],
        "decisions_requested": [],
        "key_facts": [],
        "needs_reply": False,
        "needs_reply_reason": None,
    }
    j.s.fake.parsed.extend([result, result])
    await j.owner(
        owner_input("J24", locale)[0],
        call(
            "summarize_period",
            since="2026-10-18 00:00",
            until=None,
            conversation_id=None,
            contact_id=None,
        ),
        text_response(TEXT[locale].outcome),
    )
    children = [request for request in j.s.fake.requests if request.purpose == "summarize"]
    assert len(children) <= 1, "nested calls escaped the request token budget"
    output = j.outputs()[-1]
    assert output["conversation_count"] == 1
    assert len(output["not_summarized"]) == 1
    assert "budget" in output["not_summarized"][0]["reason"]
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs I16", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
async def test_j24_unpriced_route_is_denied_before_fake_provider_dispatch(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    runtime = j.s.handlers.owner_runtime
    assert isinstance(runtime, OwnerRequestRuntime)
    runtime._config = replace(runtime._config, model="gpt-synthetic-unpriced-route")
    await j.owner(owner_input("J24", locale)[0], text_response(TEXT[locale].outcome))
    assert j.s.fake.requests == []
    await j.check(CountCheck(artifact="business_attempts", count=0))
