"""I25 structural incident trace and report (finding R4-10).

Synthetic wrong-draft cases through the real owner runtime (production LLM
wiring over a scripted FakeLLM): the trace and the content-free report must
reveal the instruction source, the recipient selection, any dropped channel,
the observed action version and the outcome — with ids and fixed codes only.
Redaction: no synthetic text, name or secret reaches the trace rows, the
report or the logs. Bounds, tenant scoping, retention and cascade are checked
on the table directly. Synthetic text and names only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from structlog.testing import capture_logs

from assistant.accounts.models import Account
from assistant.agent.incident_trace import event, write_events
from assistant.agent.models import (
    MAX_TRACE_EVENTS_PER_RUN,
    AgentRun,
    IncidentTraceEvent,
)
from assistant.llm.port import LLMRequest, LLMResponse
from assistant.observability.incident_report import (
    IncidentReport,
    build_incident_report,
    format_incident_report,
)
from assistant.privacy.retention import sweep_all
from tests.fakes.llm import text_response, tool_response
from tests.integration.agent_env import args, ask
from tests.integration.drafting_env import (
    DraftEnv,
    actions_of,
    drafting_environment,
    seed_chat,
)
from tests.integration.test_owner_drafting import (
    main_runtime,
    named_chat,
    owner_runtime,
    request_facts,
    runs,
)
from tests.integration.test_reply_tools import reply_args
from tests.unit.test_draft_owner_units import owner_output

pytestmark = pytest.mark.integration

BODY_CANARY = "SYNTH-I25-BODY-CANARY the wrong words"
ASK_CANARY = "SYNTH-I25-ASK-CANARY"
FORWARD_CANARY = "SYNTH-I25-FORWARDED-CANARY"
SECRET_CANARY = "sk-proj-I25SyntheticSecretValue"
#: Contact name given by `named_chat`.
NAME_CANARIES = ("Kamola", "Ziyo")


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url) as built:
        yield built


async def report_for(denv: DraftEnv, account_id: int, **target: int) -> IncidentReport:
    async with denv.env.factory() as session:
        return await build_incident_report(session, account_id=account_id, **target)


async def trace_text(denv: DraftEnv, account_id: int) -> str:
    """Every stored trace value of the account, as one string."""
    async with denv.env.factory() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM app.incident_trace_events WHERE account_id = :a"),
                {"a": account_id},
            )
        ).mappings()
        return json.dumps([dict(row) for row in rows], default=str)


def assert_content_free(*surfaces: str, canaries: tuple[str, ...]) -> None:
    for surface in surfaces:
        for canary in canaries:
            assert canary not in surface, canary


def codes(report: IncidentReport) -> set[tuple[str, str]]:
    return {(item.stage, item.code) for item in report.trace}


# ------------------------------------------------------------ the wrong-draft cases
async def test_main_writer_wrong_draft_reveals_source_recipient_version_and_outcome(
    denv: DraftEnv,
) -> None:
    """Flag on (C1/C2): a clarified request, then a proposal whose words are
    wrong. The report reconstructs it from ids and codes alone."""
    account_id, conversation_id = await named_chat(denv)
    first = await ask(denv.env, "Write to Kamola Ziyo.")
    denv.fake.queue(text_response("SYNTH what should I write?"))
    await main_runtime(denv).run(first)
    initial = request_facts(denv.fake.requests[-1])
    second = await ask(denv.env, f"{ASK_CANARY} ask about Friday's book delivery.")

    def propose(request: LLMRequest) -> LLMResponse:
        facts = request_facts(request)
        return tool_response(
            (
                "i25-body",
                "propose_owner_message",
                args(
                    request_id=facts["request_id"],
                    request_version=facts["request_version"],
                    conversation_id=None,
                    mode="new_message",
                    answered_message_id=None,
                    text=BODY_CANARY,
                    context_fence_id=1,
                ),
            )
        )

    denv.fake.queue(propose, text_response(f"SYNTH awaiting approval {SECRET_CANARY}"))
    with capture_logs() as logs:
        await main_runtime(denv).run(second)
    [card] = await actions_of(denv.env, conversation_id)
    assert card.text == BODY_CANARY  # the stored body is the (synthetic) wrong draft

    report = await report_for(denv, account_id, action_id=card.id)
    [owner_run] = [run for run in report.runs if run.run_type == "owner_request"]
    assert owner_run.status == "succeeded"
    trace = report.trace
    # Instruction source: the request's owner sources, by turn and purpose.
    instruction_turns = {t.turn_id for t in trace if t.stage == "instruction"}
    assert {first, second} <= instruction_turns
    assert all(
        t.code in {"instruction", "literal_body", "constraint"}
        for t in trace
        if t.stage == "instruction"
    )
    # The request this run continued.
    [request_row] = [t for t in trace if t.stage == "request"]
    assert request_row.request_id == initial["request_id"]
    assert request_row.code in {"pending_question", "focused_request", "explicit_reply"}
    # Recipient selection: the request binding and the proposal's target.
    [binding] = [t for t in trace if t.stage == "recipient" and t.code != "proposal_target"]
    assert binding.code in {"unique_strong", "owner_choice", "explicit_reply"}
    assert binding.conversation_id == conversation_id
    [target] = [t for t in trace if t.stage == "recipient" and t.code == "proposal_target"]
    assert (target.conversation_id, target.action_id) == (conversation_id, card.id)
    # The proposal result and the version it showed the model.
    [proposal] = [t for t in trace if t.stage == "proposal"]
    assert (proposal.code, proposal.action_id) == ("staged", card.id)
    [observed] = [t for t in trace if t.stage == "observed"]
    assert (observed.action_id, observed.action_version) == (card.id, card.version)
    # Outcome at the end of the run, then the run status.
    [outcome] = [t for t in trace if t.stage == "outcome"]
    assert (outcome.code, outcome.action_id) == ("awaiting_approval", card.id)
    assert ("run", "succeeded") in codes(report)
    # No channel was dropped in this case.
    assert not [t for t in trace if t.stage == "dropped" and t.code != "managed_access_off"]
    [linked] = report.actions
    assert (linked.action_id, linked.owner_request_id) == (card.id, initial["request_id"])
    [request] = report.requests
    assert request.request_id == initial["request_id"]
    assert request.selection_basis == binding.code

    # The request view spans both runs.
    by_request = await report_for(denv, account_id, request_id=initial["request_id"])
    assert len([r for r in by_request.runs if r.run_type == "owner_request"]) == 2

    formatted = format_incident_report(report)
    assert f"action={card.id} v{card.version}" in formatted
    assert_content_free(
        formatted,
        format_incident_report(by_request),
        await trace_text(denv, account_id),
        json.dumps(logs, default=str),
        canaries=(BODY_CANARY, ASK_CANARY, SECRET_CANARY, *NAME_CANARIES),
    )


async def test_legacy_writer_forwarded_turn_reveals_the_dropped_channel_and_child_run(
    denv: DraftEnv,
) -> None:
    """Flag off (historical writer): a forwarded turn's words never reach the
    draft; the trace says so and links the draft child run."""
    account_id, conversation = await seed_chat(denv, mode="observe")
    turn = await ask(
        denv.env,
        f"{FORWARD_CANARY} someone else's words",
        forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "Synth Other"},
    )
    denv.fake.queue(
        tool_response(("c1", "propose_reply", json.dumps(reply_args(conversation)))),
        text_response("A card is waiting."),
    )
    denv.script(owner_output())
    with capture_logs() as logs:
        await owner_runtime(denv).run(turn)
    [owner_run] = await runs(denv, "owner_request")
    [draft_run] = await runs(denv, "draft_reply")
    [card] = await actions_of(denv.env, conversation)

    report = await report_for(denv, account_id, run_id=owner_run.id)
    found = codes(report)
    assert ("dropped", "forwarded_turn") in found
    assert ("dropped", "dropped_quoted") in found
    assert ("taint", "started_tainted") in found
    assert [t.stage for t in report.trace].count("request") == 1
    [child] = [t for t in report.trace if t.stage == "child_run"]
    assert (child.child_run_id, child.code) == (draft_run.id, "succeeded")
    assert {run.run_id: run.parent_run_id for run in report.runs}[draft_run.id] == owner_run.id
    [target] = [t for t in report.trace if t.stage == "recipient" and t.code == "proposal_target"]
    assert target.conversation_id == conversation
    [observed] = [t for t in report.trace if t.stage == "observed"]
    assert (observed.action_id, observed.action_version, observed.code) == (
        card.id,
        card.version,
        "unknown",
    )
    [outcome] = [t for t in report.trace if t.stage == "outcome"]
    assert (outcome.code, outcome.action_id) == ("awaiting_approval", card.id)
    assert [s.tool_name for s in report.steps] == ["propose_reply"]
    summary = report.summary
    assert any("dropped_quoted" in value for value in summary.dropped_channels)
    assert any(f"action={card.id}" in value for value in summary.observed_versions)

    assert_content_free(
        format_incident_report(report),
        await trace_text(denv, account_id),
        json.dumps(logs, default=str),
        canaries=(FORWARD_CANARY, "Synth Other"),
    )


# ------------------------------------------------------------ bounds, tenancy, retention
async def _account_and_run(denv: DraftEnv, telegram_user_id: int) -> tuple[int, int]:
    async with denv.env.factory() as session, session.begin():
        account = Account(telegram_user_id=telegram_user_id)
        session.add(account)
        await session.flush()
        run = AgentRun(account_id=account.id, run_type="owner_request", model="synthetic")
        session.add(run)
        await session.flush()
        return account.id, run.id


async def test_trace_is_bounded_with_one_truncation_row(denv: DraftEnv) -> None:
    account_id, run_id = await _account_and_run(denv, 92501)
    many = [event("run", "running")] * (MAX_TRACE_EVENTS_PER_RUN + 6)
    assert (
        await write_events(denv.env.factory, account_id=account_id, run_id=run_id, events=many)
        == MAX_TRACE_EVENTS_PER_RUN
    )
    assert (
        await write_events(
            denv.env.factory, account_id=account_id, run_id=run_id, events=[event("run", "failed")]
        )
        == 0
    )
    async with denv.env.factory() as session:
        rows = list(
            await session.scalars(
                select(IncidentTraceEvent)
                .where(IncidentTraceEvent.run_id == run_id)
                .order_by(IncidentTraceEvent.seq)
            )
        )
    assert len(rows) == MAX_TRACE_EVENTS_PER_RUN
    assert (rows[-1].stage, rows[-1].code) == ("trace", "truncated")


async def test_foreign_ids_are_never_stored_or_reported(denv: DraftEnv) -> None:
    account_id, run_id = await _account_and_run(denv, 92502)
    other_account, other_run = await _account_and_run(denv, 92503)
    # Another account's run as "child", and another account's run id as target.
    await write_events(
        denv.env.factory,
        account_id=account_id,
        run_id=run_id,
        events=[event("child_run", "succeeded", child_run_id=other_run)],
    )
    assert (
        await write_events(
            denv.env.factory,
            account_id=account_id,
            run_id=other_run,
            events=[event("run", "failed")],
        )
        == 0
    )
    async with denv.env.factory() as session:
        [row] = list(
            await session.scalars(
                select(IncidentTraceEvent).where(IncidentTraceEvent.account_id == account_id)
            )
        )
    assert row.child_run_id is None
    foreign = await report_for(denv, other_account, run_id=run_id)
    assert foreign.runs == ()
    assert foreign.trace == ()
    assert format_incident_report(foreign).endswith("nothing found")


async def test_trace_goes_with_the_run_sweep_and_with_the_account(denv: DraftEnv) -> None:
    account_id, run_id = await _account_and_run(denv, 92504)
    _, kept_run = await _account_and_run(denv, 92505)
    for target in (run_id, kept_run):
        async with denv.env.factory() as session:
            owner = await session.scalar(select(AgentRun.account_id).where(AgentRun.id == target))
        assert owner is not None
        await write_events(
            denv.env.factory,
            account_id=owner,
            run_id=target,
            events=[event("run", "succeeded")],
        )
    # One year of accounting retention: the old run and its trace are swept.
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.agent_runs SET started_at = :at WHERE id = :id"),
            {"at": denv.env.clock.now() - timedelta(days=400), "id": run_id},
        )
    await sweep_all(denv.env.factory, denv.env.clock)
    async with denv.env.factory() as session:
        remaining = set(await session.scalars(select(IncidentTraceEvent.run_id)))
    assert run_id not in remaining
    assert kept_run in remaining
    # Account deletion cascades.
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            text(
                "DELETE FROM app.accounts WHERE id = "
                "(SELECT account_id FROM app.agent_runs WHERE id = :id)"
            ),
            {"id": kept_run},
        )
    async with denv.env.factory() as session:
        assert list(await session.scalars(select(IncidentTraceEvent.id))) == []
    assert account_id
