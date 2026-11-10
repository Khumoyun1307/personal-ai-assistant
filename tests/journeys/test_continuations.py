"""Additional matrix trajectories: topic switching, bare answers and advice."""

import pytest

from tests.fakes.llm import text_response
from tests.journeys.harness import Journey, call
from tests.journeys.locales import BOOK_QUESTION, CONSTRAINT_QUESTION, QUERIES, TEXT
from tests.journeys.matrix import owner_input
from tests.journeys.models import LOCALES, CountCheck, Locale, RequestCheck, Restart

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param(
            "J01", marks=pytest.mark.xfail(strict=True, reason="needs H3", raises=AssertionError)
        ),
        pytest.param(
            "J03", marks=pytest.mark.xfail(strict=True, reason="needs H4", raises=AssertionError)
        ),
        pytest.param(
            "J06", marks=pytest.mark.xfail(strict=True, reason="needs H4", raises=AssertionError)
        ),
        pytest.param(
            "J19", marks=pytest.mark.xfail(strict=True, reason="needs H4", raises=AssertionError)
        ),
    ],
)
async def test_matrix_pending_answer_precedes_search_and_new_topic_does_not_reuse_body(
    journey: Journey, locale: Locale, case_id: str
) -> None:
    j = journey
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    await j.directory("n", "Nora Vale")
    first, second = owner_input(case_id, locale)
    if case_id == "J06":
        await j.dictate(TEXT[locale].generated)
        await j.dictate("CANARY-SYNTH Nora body.", alias="n", chat="n")
    if case_id == "J01":
        await j.directory("other-kamola", "Kamola Safo")
        await j.owner(
            first,
            call("resolve_contact", name=QUERIES[locale], limit=None),
            text_response(TEXT[locale].recipient_question),
        )
        assert TEXT[locale].recipient_question in j.last_reply
        await j.check(CountCheck(artifact="actions", count=0))
    elif case_id == "J19":
        await j.owner(first, j.source_proposal(), text_response(TEXT[locale].choice))
        await j.bind_card("k")
    else:
        question = (
            CONSTRAINT_QUESTION[locale] if case_id == "J06" else TEXT[locale].content_question
        )
        await j.owner(first, text_response(question), reply_to="k" if case_id == "J06" else None)
        assert question in j.last_reply
    request = await j.request()
    if case_id == "J06":
        assert request.missing_slot == "constraints"
    await j.event(Restart())
    if case_id == "J01":
        await j.check(RequestCheck(state="collecting", slot="recipient"))
        await j.owner(second, j.source_proposal(), text_response(second))
        await j.bind_card("k")
        assert (await j.action("k")).text == first.split(": ", 1)[1]
        assert (await j.request()).request_id == request.request_id
    elif case_id == "J19":
        await j.owner(
            second,
            j.read("n"),
            j.generated_proposal(BOOK_QUESTION[locale], chat="n"),
            text_response(BOOK_QUESTION[locale]),
        )
        current = await j.request()
        assert current.request_id != request.request_id
        assert current.conversation_id == j.chats["n"]
        assert (await j.request(request.request_id)).state in {"cancelled", "superseded"}
        await j.bind_card("n", "n")
        assert "X1" not in ((await j.action("n")).text or "")
    else:
        proposal = (
            j.revision("k", TEXT[locale].revised)
            if case_id == "J06"
            else j.generated_proposal(BOOK_QUESTION[locale])
        )
        body = TEXT[locale].revised if case_id == "J06" else BOOK_QUESTION[locale]
        await j.owner(second, j.read(), proposal, text_response(body))
        current = await j.request()
        assert current.request_id == request.request_id
        if case_id == "J03":
            assert current.missing_slot != "content"
            await j.bind_card("k")
            assert (await j.action("k")).text == BOOK_QUESTION[locale]
        else:
            revised = await j.bind_card("k")
            assert revised.version == 2
            assert revised.text == TEXT[locale].revised
            assert (await j.action("n")).text == "CANARY-SYNTH Nora body."
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
async def test_j25_advice_and_list_edit_do_not_create_message_artifacts(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    await j.owner(TEXT[locale].brief, text_response("1. X1\n2. X2\n3. X3"))
    await j.owner(owner_input("J25", locale)[0], text_response(TEXT[locale].list_answer))
    assert TEXT[locale].list_answer in j.last_reply
    await j.check(CountCheck(artifact="actions", count=0))
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs H6", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
async def test_h6_chat_cutover_offers_main_writer_and_retires_nested_writer(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    await j.owner(owner_input("J04", locale)[0], text_response(TEXT[locale].content_question))
    [request] = j.s.fake.requests
    offered = {tool.name for tool in request.tools}
    assert "propose_owner_message" in offered
    assert "propose_reply" not in offered
    await j.check(CountCheck(artifact="business_attempts", count=0))
