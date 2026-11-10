"""I03/J02: strong identity wins; all variants go through the real tool/runtime."""

import pytest

from tests.fakes.llm import text_response
from tests.journeys.harness import Journey, call
from tests.journeys.locales import QUERIES, TEXT
from tests.journeys.models import LOCALES, CardCheck, CountCheck, Locale

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    ("other", "query", "verdict", "count"),
    [
        ("Kamariddin Nur", None, "unique_strong", 1),
        ("Kamola Safo", None, "ambiguous_strong", 2),
        ("Nora Vale", "Kamloa", "weak_only", 1),
        ("Kamron Safi", "Kam", "ambiguous_strong", 1),
    ],
)
async def test_h5_real_match_verdict_precedes_page_cap(
    journey: Journey,
    locale: Locale,
    other: str,
    query: str | None,
    verdict: str,
    count: int,
) -> None:
    j = journey
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    await j.directory("n", other)
    await j.owner(
        TEXT[locale].unique,
        call("resolve_contact", name=query or QUERIES[locale], limit=1 if query == "Kam" else 5),
        text_response(
            TEXT[locale].recipient_question if verdict != "unique_strong" else TEXT[locale].choice
        ),
    )
    output = j.outputs()[-1]
    assert output["verdict"] == verdict
    assert output["count"] == count
    if verdict == "unique_strong":
        [candidate] = output["candidates"]
        assert candidate["conversation_id"] == j.chats["k"]
        assert candidate["confidence"] == "match"
        assert "?" not in j.last_reply
    await j.check(CountCheck(artifact="actions", count=0))
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs H2", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("trial", range(3), ids=lambda trial: f"trial-{trial + 1}")
async def test_i03_unique_contact_bound_across_next_owner_turn(
    journey: Journey, locale: Locale, trial: int
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    await j.directory("n", "Kamariddin Nur")
    await j.owner(
        t.unique,
        call("resolve_contact", name=QUERIES[locale], limit=None),
        j.source_proposal(),
        text_response(t.choice),
    )
    await j.bind_card("k")
    await j.check(CardCheck(alias="k", text=t.unique.split(": ", 1)[1], origin="owner_dictated"))
    request = await j.request()
    await j.owner(
        t.who,
        call("get_draft", conversation_id=None, action_id=j.cards["k"]),
        text_response(t.choice),
    )
    assert (await j.request(request.request_id)).conversation_id == j.chats["k"]
    assert j.outputs()[-1]["conversation_id"] == j.chats["k"]
    await j.check(CountCheck(artifact="actions", count=1))
    await j.check(CountCheck(artifact="business_attempts", count=0))
