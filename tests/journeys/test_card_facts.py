"""I04/J06/J11/J12/J21/J22/J23: exact artifact, token and terminal facts."""

from typing import Any

import pytest
from sqlalchemy import select

from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest
from tests.fakes.llm import text_response
from tests.journeys.grading import assert_equal
from tests.journeys.harness import Journey, call
from tests.journeys.locales import OUTDATED, OWNER_LABEL, SENT, TEXT, UNAVAILABLE, UNCERTAIN
from tests.journeys.models import (
    LOCALES,
    Contact,
    CountCheck,
    FactualReplyCheck,
    Fail,
    Locale,
    Restart,
    Revoke,
)
from tests.scenarios.p2_drive import run_queued

pytestmark = [pytest.mark.integration, pytest.mark.journey]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("transition", ["reject", "approve", "outdate"])
async def test_q2_q3_j11_terminal_facts_do_not_depend_on_live_card(
    journey: Journey, locale: Locale, transition: str
) -> None:
    j = journey
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    await j.directory("n", "Nora Vale")
    body = TEXT[locale].unique.split(": ", 1)[1]
    row = await j.dictate(body)
    other = await j.dictate("CANARY-SYNTH Nora only.", alias="n", chat="n")
    if transition == "outdate":
        await j.event(Contact(alias="new", text="CANARY-SYNTH changed topic."))
        state = "outdated"
    else:
        await j.tap("k", 1, transition)
        if transition == "approve":
            await run_queued(j.s, "actions.execute")
        state = "succeeded" if transition == "approve" else "rejected"
    wording = (
        TEXT[locale].rejected
        if state == "rejected"
        else SENT[locale]
        if state == "succeeded"
        else OUTDATED[locale]
    )
    await j.owner(
        TEXT[locale].outcome,
        call("get_draft", action_id=row.id, conversation_id=None),
        text_response(wording),
    )
    result = j.outputs()[-1]
    assert result["action_id"] == row.id
    assert result["version"] == 1
    assert result["conversation_id"] == j.chats["k"]
    assert result["state"] == state
    assert result["live"] is False
    assert body in result["text"]
    assert result["word_count"] == len(body.split())
    assert result["char_count"] == len(body)
    assert (await j.action("n")).state == other.state
    assert (await j.action("n")).text == other.text
    await j.check(
        FactualReplyCheck(
            card="k",
            version=1,
            status="sent" if transition == "approve" else "not_sent",
            wording=wording,
            text_sha256=j.text_hash(body),
        )
    )
    await j.check(CountCheck(artifact="business_attempts", count=int(transition == "approve")))


@pytest.mark.xfail(strict=True, reason="needs H4", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("trial", range(3), ids=lambda trial: f"trial-{trial + 1}")
async def test_i04_j06_reply_selects_k_revision_and_rejected_v2_content(
    journey: Journey, locale: Locale, trial: int
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    await j.directory("k", "Kamola Ziyo")
    await j.directory("n", "Nora Vale")
    await j.owner(
        f"{t.choice} {t.brief}",
        j.read(),
        j.generated_proposal(t.generated),
        text_response(t.generated),
    )
    original = await j.bind_card("k")
    await j.owner(
        f"Nora Vale. {t.brief}",
        j.read("n"),
        j.generated_proposal(t.generated, chat="n"),
        text_response(t.generated),
    )
    n = await j.bind_card("n", "n")

    async def rewrite(_request: LLMRequest) -> Any:
        request = await j.request()
        fences = [o["context_fence_id"] for o in j.outputs() if "context_fence_id" in o]
        assert fences
        return call(
            "revise_draft",
            request_id=request.request_id,
            request_version=request.version,
            action_id=original.id,
            expected_version=1,
            edit="rewrite",
            text=t.revised,
            source_ids=[],
            context_fence_id=fences[-1],
        )

    await j.owner(t.revise, j.read(), rewrite, text_response(t.revised), reply_to="k")
    k = await j.bind_card("k")
    assert k.id == original.id
    assert k.version == 2
    assert k.text == t.revised
    assert k.approval_token != original.approval_token
    await j.tap("k", 1, "approve")
    assert (await j.action("k")).state == "awaiting_approval"
    await j.tap("k", 2, "reject")
    await j.owner(
        t.outcome,
        call("get_draft", conversation_id=None, action_id=k.id),
        text_response(f"{t.rejected} {t.revised}"),
    )
    assert j.outputs()[-1]["state"] == "rejected"
    assert j.outputs()[-1]["version"] == 2
    assert t.revised in j.outputs()[-1]["text"]
    assert t.rejected in j.last_reply
    assert (await j.action("n")).version == n.version
    assert (await j.action("n")).text == n.text
    await j.check(CountCheck(artifact="nested_writers", count=0))
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("literal_append", [False, True])
async def test_j12_rewrite_has_generated_authorship_and_revocation_cancels(
    journey: Journey, locale: Locale, *, literal_append: bool
) -> None:
    j = journey
    await j.language(locale)
    await j.dictate(TEXT[locale].unique.split(": ", 1)[1])
    await j.owner(
        TEXT[locale].revise,
        j.read(),
        j.revision("k", TEXT[locale].revised),
        text_response(TEXT[locale].revised),
        reply_to="k",
    )
    revised = await j.bind_card("k")
    assert revised.version == 2
    proof = getattr(revised, "authorship", None)
    assert proof is not None, "rewritten version has no authorship proof"
    assert proof["kind"] == "assistant_written"
    assert proof["retains_generated_text"] is True
    if literal_append:
        await j.owner(
            " X1",
            j.read(),
            j.revision("k", "X1", append=True),
            text_response("X1"),
            reply_to="k",
        )
        appended = await j.bind_card("k")
        assert appended.version == 3
        assert appended.text == f"{TEXT[locale].revised} X1"
        append_proof = getattr(appended, "authorship", None)
        assert append_proof is not None
        assert append_proof["retains_generated_text"] is True
    await j.event(Revoke(scope="ai_managed"))
    await j.tap("k", 1, "approve")
    await run_queued(j.s, "actions.execute")
    assert (await j.action("k")).state in {"cancelled", "blocked"}
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs H7", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("purge", [False, True])
async def test_j22_final_answer_refreshes_after_artifact_read(
    journey: Journey, locale: Locale, *, purge: bool
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    row = await j.dictate(t.generated)

    async def race(_request: LLMRequest) -> Any:
        # This is after get_draft and before final output, not seeded history.
        if purge:
            async with transaction(j.s.env.factory) as uow:
                current = await uow.session.get(type(row), row.id)
                assert current is not None
                current.text = None
        else:
            await j.event(Contact(alias="late", text="CANARY-SYNTH changed question."))
        return text_response(t.generated)

    requests_before = len(j.s.fake.requests)
    await j.owner(
        t.outcome,
        call("get_draft", action_id=row.id, conversation_id=None),
        race,
        text_response(UNAVAILABLE[locale] if purge else OUTDATED[locale]),
    )
    # H7 must discard the unsent stale final and perform a correction/read or
    # use deterministic receipt wording. Returning the scripted stale claim fails.
    assert j.last_reply != t.generated
    if purge:
        assert t.generated not in j.last_reply
        assert UNAVAILABLE[locale] in j.last_reply
    else:
        assert (await j.action("k")).state == "outdated"
        assert OUTDATED[locale] in j.last_reply
    assert len(j.s.fake.requests) >= requests_before + 2
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.xfail(strict=True, reason="needs H8", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
async def test_j21_redraft_retains_standalone_request_after_restart(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    await j.owner(t.brief, j.read(), j.generated_proposal(t.generated), text_response(t.generated))
    row = await j.bind_card("k")
    request = await j.request()
    await j.event(Contact(alias="new", text="CANARY-SYNTH newer question."))
    assert (await j.action("k")).state == "outdated"
    await j.event(Restart())
    j.s.fake.queue(j.read(), j.generated_proposal(t.revised), text_response(t.revised))
    await j.tap("k", row.version, "redraft")
    await run_queued(j.s, "agent.owner_request")
    assert (await j.request()).request_id == request.request_id
    current = await j.bind_card("k")
    assert current.base_message_id is None
    assert current.text == t.revised
    await j.check(CountCheck(artifact="nested_writers", count=0))


@pytest.mark.xfail(strict=True, reason="needs I27", raises=AssertionError)
@pytest.mark.parametrize("locale", LOCALES)
async def test_j23_explicit_card_recovery_never_retries_business_send(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    await j.event(Fail(stage="presentation", outcome="uncertain"))
    row = await j.dictate(TEXT[locale].generated)
    # Prove that the card send failed once, while the rest of this turn and
    # the recovery command have a healthy transport. Do not xfail a network
    # exception or count attempted delivery as a card the owner saw.
    assert row.card_message_id is None
    assert await j.audit("k", "approval_card_presentation_claimed") == 1
    assert j.s.env.sender.control_script == []
    assert j.s.env.sender.fail_with is None
    assert j.last_reply
    await j.event(Restart())
    before = len(j.s.env.sender.delivered_control_sends)
    # The recovery command is deterministic, including when AI is unavailable.
    await deliver_recovery(j)
    current = await j.action("k")
    assert current.id == row.id
    assert current.version == row.version
    recovered = j.s.env.sender.delivered_control_sends[before:]
    assert recovered, "I27 must explicitly recover the uncertain card"
    assert current.card_message_id is not None
    assert any(
        item["message_id"] == current.card_message_id and TEXT[locale].generated in item["text"]
        for item in recovered
    )
    await j.check(CountCheck(artifact="business_attempts", count=0))


async def deliver_recovery(j: Journey) -> None:
    from tests.integration.business_env import OWNER, deliver, private_message

    await deliver(j.s.env, private_message(OWNER, "/current"))


@pytest.mark.parametrize("locale", LOCALES)
async def test_policy_rechecks_rights_after_card_approval(journey: Journey, locale: Locale) -> None:
    from assistant.accounts.models import BusinessConnection

    j = journey
    await j.language(locale)
    await j.dictate(TEXT[locale].generated)
    await j.tap("k", 1, "approve")
    async with transaction(j.s.env.factory) as uow:
        connection = await uow.session.scalar(
            select(BusinessConnection).where(BusinessConnection.account_id == j.s.account_id)
        )
        assert connection is not None
        connection.is_enabled = False
    await run_queued(j.s, "actions.execute")
    assert (await j.action("k")).state == "blocked"
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
async def test_ambiguous_business_send_has_committed_claim_and_is_never_retried(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    body = TEXT[locale].generated
    await j.dictate(body)
    await j.tap("k", 1, "approve")
    observed = False

    async def observe_claim() -> None:
        nonlocal observed
        if observed:
            return
        observed = True
        # Independent session at the transport boundary: a flush in an
        # uncommitted execution transaction would not be visible here.
        claimed = await j.action("k")
        assert_equal(claimed.state, "executing", field="state at transport boundary")
        assert claimed.attempt_id is not None, "committed attempt_id: expected value, actual None"
        assert_equal(claimed.approved_text_hash, j.text_hash(body), field="approved text hash")
        assert_equal(await j.audit("k", "action_claimed"), 1, field="committed claim count")

    j.s.env.sender.on_call = observe_claim
    await j.event(Fail(stage="send", outcome="uncertain"))
    try:
        await run_queued(j.s, "actions.execute")
    finally:
        j.s.env.sender.on_call = None
    assert_equal(observed, True, field="transport boundary observed")
    uncertain = await j.action("k")
    assert_equal(uncertain.state, "delivery_uncertain", field="ambiguous send state")
    assert_equal(uncertain.state_reason, "ambiguous", field="ambiguous send reason")
    await j.event(Restart())
    await j.s.handlers.reconcile_pending()
    await j.s.execute(j.cards["k"])
    await j.check(CountCheck(artifact="business_attempts", count=1))
    [attempt] = j.s.business_sends()
    assert_equal(attempt["text"], body, field="business attempt text")
    assert attempt["business_connection_id"] is not None, (
        "business attempt connection_id: expected value, actual None"
    )
    assert_equal(await j.audit("k", "action_claimed"), 1, field="claim count after replay")
    replayed = await j.action("k")
    assert_equal(replayed.state, uncertain.state, field="state after replay")
    assert_equal(replayed.attempt_id, uncertain.attempt_id, field="attempt_id after replay")
    await j.owner(
        TEXT[locale].outcome,
        call("get_draft", conversation_id=None, action_id=j.cards["k"]),
        text_response(UNCERTAIN[locale]),
    )
    await j.check(
        FactualReplyCheck(card="k", version=1, status="uncertain", wording=UNCERTAIN[locale])
    )


@pytest.mark.parametrize(
    "locale",
    [
        pytest.param(
            locale,
            marks=pytest.mark.xfail(strict=True, reason="needs I27", raises=AssertionError)
            if locale != "en"
            else (),
        )
        for locale in LOCALES
    ],
)
async def test_card_authorship_label_is_localized_without_changing_exact_body(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    body = TEXT[locale].unique.split(": ", 1)[1]
    row = await j.dictate(body)
    assert row.text == body
    cards = [
        item["text"]
        for item in j.s.env.sender.delivered_control_sends
        if item.get("message_id") == row.card_message_id
    ]
    assert cards
    assert OWNER_LABEL[locale] in "\n".join(cards)
    await j.check(CountCheck(artifact="business_attempts", count=0))
