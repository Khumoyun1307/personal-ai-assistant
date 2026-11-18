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
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import NoReturn

import pytest
from sqlalchemy import select, text
from structlog.testing import capture_logs

from assistant.accounts.models import Account, ControlTurn
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest
from assistant.agent import incident_trace
from assistant.agent.incident_trace import (
    MAX_CHILD_RUNS_PER_READ,
    MAX_OUTCOMES_TRACED,
    REGULAR_SLOTS,
    event,
    write_events,
)
from assistant.agent.models import (
    MAX_TRACE_EVENTS_PER_RUN,
    TRACE_CODES,
    TRACE_REASONS,
    TRACE_STAGES,
    AgentRun,
    IncidentTraceEvent,
)
from assistant.llm.port import LLMRequest, LLMResponse
from assistant.observability.incident_report import (
    IncidentReport,
    build_incident_report,
    format_incident_report,
)
from assistant.privacy import deletion
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
ERROR_CANARY = "SYNTHI25ERRORCANARY"
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

    # Error and reason paths first, each carrying canaries in what the model sent.
    errors = tool_response(
        (
            "i25-unknown-request",
            "propose_owner_message",
            args(
                request_id=999_999,
                request_version=1,
                conversation_id=None,
                mode="new_message",
                answered_message_id=None,
                text=f"{ERROR_CANARY} {SECRET_CANARY}",
                context_fence_id=1,
            ),
        ),
        ("i25-bad-json", "propose_owner_message", "{" + ERROR_CANARY),
        ("i25-bad-args", "propose_owner_message", args(request_id=ERROR_CANARY)),
        ("i25-unknown-tool", f"{ERROR_CANARY}_tool", args(note=ERROR_CANARY)),
    )
    denv.fake.queue(errors, propose, text_response(f"SYNTH awaiting approval {SECRET_CANARY}"))
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
    [proposal] = [t for t in trace if t.stage == "proposal" and t.action_id is not None]
    assert (proposal.code, proposal.action_id) == ("staged", card.id)
    # The failed calls are traced by fixed code only (the unknown tool is not traced).
    failed = sorted((t.code, t.reason) for t in trace if t.stage == "proposal" and not t.action_id)
    assert len(failed) == 3
    assert {code for code, _ in failed} <= {"not_available", "invalid_arguments", "error"}
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
        repr(report),
        repr(by_request),
        format_incident_report(by_request),
        await trace_text(denv, account_id),
        json.dumps(logs, default=str),
        canaries=(BODY_CANARY, ASK_CANARY, SECRET_CANARY, ERROR_CANARY, *NAME_CANARIES),
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
        repr(report),
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
    """S2: tool overflow is marked once, and the finish batch still lands,
    ending with the run status."""
    account_id, run_id = await _account_and_run(denv, 92501)
    factory = denv.env.factory
    many = [event("proposal", "staged")] * (MAX_TRACE_EVENTS_PER_RUN + 6)
    written = await write_events(factory, account_id=account_id, run_id=run_id, events=many)
    assert written == REGULAR_SLOTS + 1
    more = [event("proposal", "staged")]
    assert await write_events(factory, account_id=account_id, run_id=run_id, events=more) == 0
    finish = [
        *[event("outcome", "awaiting_approval")] * MAX_OUTCOMES_TRACED,
        *[event("child_run", "succeeded")] * MAX_CHILD_RUNS_PER_READ,
        event("run", "succeeded"),
    ]
    assert await write_events(
        factory, account_id=account_id, run_id=run_id, events=finish, final=True
    ) == len(finish)
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(IncidentTraceEvent)
                .where(IncidentTraceEvent.run_id == run_id)
                .order_by(IncidentTraceEvent.seq)
            )
        )
    assert len(rows) == MAX_TRACE_EVENTS_PER_RUN
    assert [(r.stage, r.code) for r in rows].count(("trace", "truncated")) == 1
    assert (rows[REGULAR_SLOTS].stage, rows[REGULAR_SLOTS].code) == ("trace", "truncated")
    assert (rows[-1].stage, rows[-1].code) == ("run", "succeeded")


async def test_a_trace_exactly_at_the_regular_limit_still_records_the_finish(
    denv: DraftEnv,
) -> None:
    """S2 edge: regular events that exactly fill their slots, then one more."""
    account_id, run_id = await _account_and_run(denv, 92530)
    factory = denv.env.factory
    exact = [event("proposal", "staged")] * REGULAR_SLOTS
    assert await write_events(factory, account_id=account_id, run_id=run_id, events=exact) == (
        REGULAR_SLOTS
    )
    one = [event("proposal", "staged")]
    assert await write_events(factory, account_id=account_id, run_id=run_id, events=one) == 1
    final = [event("outcome", "rejected", reason="owner_rejected"), event("run", "succeeded")]
    assert (
        await write_events(factory, account_id=account_id, run_id=run_id, events=final, final=True)
        == 2
    )
    async with factory() as session:
        tail = list(
            await session.execute(
                select(IncidentTraceEvent.stage, IncidentTraceEvent.code)
                .where(IncidentTraceEvent.run_id == run_id)
                .order_by(IncidentTraceEvent.seq.desc())
                .limit(3)
            )
        )
    assert [tuple(row) for row in tail] == [
        ("run", "succeeded"),
        ("outcome", "rejected"),
        ("trace", "truncated"),
    ]


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


# ------------------------------------------------------------ review round (S1, S5, M3)
async def main_flow(denv: DraftEnv) -> tuple[int, int, int]:
    """Flag-on request + staged card. Returns (account_id, conversation_id, action_id)."""
    account_id, conversation_id = await named_chat(denv)
    first = await ask(denv.env, "Write to Kamola Ziyo.")
    denv.fake.queue(text_response("SYNTH what should I write?"))
    await main_runtime(denv).run(first)
    second = await ask(denv.env, f"{ASK_CANARY} ask about Friday's book delivery.")

    def propose(request: LLMRequest) -> LLMResponse:
        facts = request_facts(request)
        return tool_response(
            (
                "i25-flow",
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

    denv.fake.queue(propose, text_response("SYNTH awaiting approval"))
    await main_runtime(denv).run(second)
    [card] = await actions_of(denv.env, conversation_id)
    return account_id, conversation_id, card.id


async def test_live_check_constraints_match_the_model_code_lists(denv: DraftEnv) -> None:
    """M3: the model's lists equal the migrated database's CHECKs (the latest
    revision), not a copy pinned to one migration file."""

    def literals(definition: str) -> set[str]:
        return set(re.findall(r"'([^']*)'::text", definition))

    async with denv.env.factory() as session:
        result = await session.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'app.incident_trace_events'::regclass AND contype = 'c'"
            )
        )
        rows = {str(row[0]): str(row[1]) for row in result.all()}
    assert literals(rows["ck_incident_trace_events_stage"]) == set(TRACE_STAGES)
    assert literals(rows["ck_incident_trace_events_reason"]) == set(TRACE_REASONS)
    all_codes = {code for values in TRACE_CODES.values() for code in values}
    assert literals(rows["ck_incident_trace_events_code"]) == set(TRACE_STAGES) | all_codes
    assert f"<= {MAX_TRACE_EVENTS_PER_RUN}" in rows["ck_incident_trace_events_seq"]
    # Every (stage, code) pair of the model is accepted by the database.
    pairs = [(stage, code) for stage, values in TRACE_CODES.items() for code in values]
    for index, start in enumerate(range(0, len(pairs), MAX_TRACE_EVENTS_PER_RUN)):
        account_id, run_id = await _account_and_run(denv, 92510 + index)
        chunk = pairs[start : start + MAX_TRACE_EVENTS_PER_RUN]
        async with denv.env.factory() as session, session.begin():
            for seq, (stage, code) in enumerate(chunk, start=1):
                session.add(
                    IncidentTraceEvent(
                        account_id=account_id, run_id=run_id, seq=seq, stage=stage, code=code
                    )
                )


async def test_trace_failures_never_change_the_run(
    denv: DraftEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: a non-database error in each hook is contained: the card is staged,
    the reply delivered, the run succeeded; the log names the type only."""

    def explode(*_: object, **__: object) -> NoReturn:
        raise TypeError(f"{ERROR_CANARY} unhashable")

    async def explode_async(*_: object, **__: object) -> NoReturn:
        raise AttributeError(f"{ERROR_CANARY} missing")

    monkeypatch.setattr(incident_trace, "start_events", explode)
    monkeypatch.setattr(incident_trace, "tool_events", explode)
    monkeypatch.setattr(incident_trace, "finish_trace", explode_async)
    account_id, conversation = await seed_chat(denv, mode="observe")
    turn = await ask(denv.env, "SYNTH reply to them please")
    denv.fake.queue(
        tool_response(("c1", "propose_reply", json.dumps(reply_args(conversation)))),
        text_response("A card is waiting."),
    )
    denv.script(owner_output())
    with capture_logs() as logs:
        outcome = await owner_runtime(denv).run(turn)
    assert outcome.status.value == "succeeded"
    assert outcome.reply_message_id is not None  # the reply was delivered
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    failures = [entry for entry in logs if entry.get("event") == "incident_trace_failed"]
    assert sorted(entry["where"] for entry in failures) == ["finish", "start", "tool"]
    assert {entry["error_type"] for entry in failures} == {"TypeError", "AttributeError"}
    assert ERROR_CANARY not in json.dumps(logs, default=str)
    async with denv.env.factory() as session:
        assert list(await session.scalars(select(IncidentTraceEvent.id))) == []
    assert account_id


async def test_foreign_action_request_turn_and_conversation_ids_become_null(
    denv: DraftEnv,
) -> None:
    """S5: every reference column is tenant-scoped on write."""
    _, conversation_id, action_id = await main_flow(denv)
    async with denv.env.factory() as session:
        request_id = await session.scalar(select(OwnerRequest.id))
        turn_id = await session.scalar(select(ControlTurn.id))
    assert request_id is not None
    assert turn_id is not None
    other_account, other_run = await _account_and_run(denv, 92520)
    await write_events(
        denv.env.factory,
        account_id=other_account,
        run_id=other_run,
        events=[
            event(
                "proposal",
                "staged",
                turn_id=turn_id,
                request_id=request_id,
                request_version=1,
                action_id=action_id,
                action_version=1,
                conversation_id=conversation_id,
            )
        ],
    )
    async with denv.env.factory() as session:
        [row] = list(
            await session.scalars(
                select(IncidentTraceEvent).where(IncidentTraceEvent.account_id == other_account)
            )
        )
    assert (
        row.turn_id,
        row.request_id,
        row.request_version,
        row.action_id,
        row.action_version,
        row.conversation_id,
    ) == (None, None, None, None, None, None)
    assert (row.stage, row.code) == ("proposal", "staged")


@pytest.mark.parametrize("scope", ["conversation", "everything"])
async def test_erasure_keeps_only_ids_pointing_at_content_free_rows(
    denv: DraftEnv, scope: str
) -> None:
    """S5/M4: erasure clears content in place and keeps the referenced rows,
    so trace ids survive; what they point at holds no content any more."""
    account_id, conversation_id, action_id = await main_flow(denv)
    before = json.loads(await trace_text(denv, account_id))
    async with denv.env.factory() as session, session.begin():
        if scope == "conversation":
            await deletion.delete_conversation_data(
                session,
                account_id=account_id,
                conversation_id=conversation_id,
                actor="owner",
                clock=denv.env.clock,
            )
        else:
            await deletion.delete_everything(
                session, account_id=account_id, actor="owner", clock=denv.env.clock
            )
    after_text = await trace_text(denv, account_id)
    assert len(json.loads(after_text)) == len(before)  # rows survive (the run is kept)
    async with denv.env.factory() as session:
        rows = list(
            await session.scalars(
                select(IncidentTraceEvent).where(IncidentTraceEvent.account_id == account_id)
            )
        )
        for row in rows:
            assert row.code in TRACE_CODES[row.stage]
            assert row.reason is None or row.reason in TRACE_REASONS
        traced_actions = {row.action_id for row in rows if row.action_id is not None}
        assert action_id in traced_actions
        texts = list(
            await session.scalars(select(Action.text).where(Action.id.in_(traced_actions)))
        )
        assert texts
        assert all(value is None for value in texts)
        if scope == "everything":
            turn_ids = {row.turn_id for row in rows if row.turn_id is not None}
            turns = (
                await session.execute(
                    select(ControlTurn.text, ControlTurn.quoted_text).where(
                        ControlTurn.id.in_(turn_ids)
                    )
                )
            ).all()
            assert all(value is None for pair in turns for value in pair)
    assert_content_free(after_text, canaries=(BODY_CANARY, ASK_CANARY, *NAME_CANARIES))
