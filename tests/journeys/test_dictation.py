"""I01/J01: actual ambiguity, restart, name-only continuation, exact source."""

import pytest

from tests.fakes.llm import text_response
from tests.journeys.harness import Journey, call
from tests.journeys.locales import QUERIES, TEXT
from tests.journeys.models import LOCALES, CardCheck, CountCheck, Locale, RequestCheck, Restart
from tests.scenarios.p2_drive import run_queued

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.xfail(strict=True, reason="needs H3", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("trial", range(3), ids=lambda trial: f"trial-{trial + 1}")
async def test_i01_j01_dictation_survives_recipient_answer_and_restart(
    journey: Journey, locale: Locale, trial: int
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    await j.directory("other-kamola", "Kamola Safo")
    first_turn = await j.owner(
        t.dictate,
        call("resolve_contact", name=QUERIES[locale], limit=None),
        text_response(t.recipient_question),
    )
    assert t.recipient_question in j.last_reply
    await j.check(CountCheck(artifact="actions", count=0))
    await j.check(RequestCheck(state="collecting", slot="recipient"))
    before = await j.request()
    original_sources = await j.source_ids()
    assert original_sources
    assert first_turn in {source.span.turn_id for source in before.sources}
    await j.event(Restart())
    await j.owner(t.choice, j.source_proposal(), text_response(t.choice))
    after = await j.request()
    assert after.request_id == before.request_id
    assert after.requested_at == before.requested_at
    assert first_turn in {source.span.turn_id for source in after.sources}
    assert set(original_sources) <= set(await j.source_ids())
    body = t.dictate.split(": ", 1)[1]
    await j.bind_card("k")
    await j.check(
        CardCheck(alias="k", text=body, origin="owner_dictated", authorship="owner_dictated")
    )
    card = await j.action("k")
    proof = getattr(card, "authorship", None)
    assert proof is not None
    assert proof["source_ids"] == original_sources
    await j.check(CountCheck(artifact="nested_writers", count=0))
    await j.check(CountCheck(artifact="business_attempts", count=0))
    await j.tap("k", 1, "approve")
    await j.tap("k", 1, "approve")
    await run_queued(j.s, "actions.execute")
    await j.s.execute(j.cards["k"])
    [sent] = j.s.business_sends()
    assert sent["text"] == body
    assert await j.audit("k", "action_claimed") == 1
    assert (await j.action("k")).sent_text_hash == j.text_hash(body)


@pytest.mark.xfail(strict=True, reason="needs H3", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("mutation", ["x1", "Mah", "folded-punctuation"])
async def test_source_copy_cannot_be_replaced_by_model_normalization(
    journey: Journey, locale: Locale, mutation: str
) -> None:
    j = journey
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    body = "X1 Max — Oʻzbek!\nA'B / A’B 🙂"
    # The model supplies source IDs, never an alternative body. Deliberately
    # misleading assistant prose must not become a source or authorize a fold.
    await j.owner(
        f"{TEXT[locale].unique.split(': ', 1)[0]}: {body}",
        text_response(mutation),
    )
    await j.owner(TEXT[locale].choice, j.source_proposal(), text_response(TEXT[locale].choice))
    await j.bind_card("k")
    await j.check(
        CardCheck(alias="k", text=body, origin="owner_dictated", authorship="owner_dictated")
    )
    assert body in await j.source_texts()
    await j.check(CountCheck(artifact="business_attempts", count=0))
