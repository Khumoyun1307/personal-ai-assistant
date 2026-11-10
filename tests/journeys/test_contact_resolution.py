"""I03/J02: strong identity wins; all variants go through the real tool/runtime."""

import pytest

from assistant.accounts.models import ControlTurn
from assistant.actions.requests import open_request
from tests.fakes.llm import text_response
from tests.integration.test_owner_requests import source
from tests.journeys.harness import Journey, call
from tests.journeys.locales import QUERIES, TEXT
from tests.journeys.models import LOCALES, CardCheck, CountCheck, Locale
from tests.scenarios.p2_drive import run_queued

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("verdict", ["unique_strong", "ambiguous_strong", "weak_only", "none"])
async def test_h5_i03_j02_j26_classified_request_binds_across_restart(
    journey: Journey, locale: Locale, verdict: str
) -> None:
    """H5 runtime seam; natural-language request classification remains H6's gate."""
    j = journey
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    await j.directory("n", "Kamola Safo" if verdict == "ambiguous_strong" else "Kamariddin Nur")
    query = {"weak_only": "Kamloa", "none": "Zebiniso Olimova"}.get(verdict, QUERIES[locale])
    text = TEXT[locale].unique
    turn_id = await j.owner(
        text,
        call("resolve_contact", name=query, limit=None),
        text_response(TEXT[locale].choice),
        run=False,
    )
    async with j.s.env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        request = await open_request(
            session,
            account_id=j.s.account_id,
            turn_id=turn_id,
            operation="compose",
            sources=(source(turn_id, text, purpose="instruction"),),
            now=j.s.env.clock.now(),
        )
    assert await run_queued(j.s, "agent.owner_request", limit=1) == 1
    output = j.outputs()[-1]
    assert output["verdict"] == verdict
    bound = await j.request(request.request_id)
    if verdict == "unique_strong":
        assert bound.conversation_id == j.chats["k"]
        assert bound.selection_basis == "unique_strong"
    else:
        assert bound.conversation_id is None
    await j.restart()
    if verdict in {"ambiguous_strong", "weak_only"}:
        index = 2 if verdict == "ambiguous_strong" else 1
        expected = output["candidates"][index - 1]["conversation_id"]
        await j.owner(str(index), text_response(TEXT[locale].choice))
        bound = await j.request(request.request_id)
        assert bound.conversation_id == expected
        assert bound.selection_basis == "owner_choice"
    elif verdict == "unique_strong":
        await j.owner(TEXT[locale].who, text_response(TEXT[locale].choice))
        assert (await j.request(request.request_id)).conversation_id == j.chats["k"]
    await j.check(CountCheck(artifact="actions", count=0))
    await j.check(CountCheck(artifact="business_attempts", count=0))


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


# H3 requires a classified C1 request; resolve_contact does not open/bind one.
@pytest.mark.xfail(strict=True, reason="needs H6", raises=AssertionError)
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
