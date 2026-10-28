"""J10 / R1 #6: activity exposes every outcome and its authorization."""

from datetime import timedelta
from typing import Any

import pytest

from assistant.accounts.models import AuditLogEntry
from assistant.db.uow import transaction
from tests.integration.business_env import OWNER, deliver, private_message
from tests.scenarios.p2_drive import Scenario, advance, tool_outputs

pytestmark = pytest.mark.integration


async def activity_mix(p2: Scenario) -> dict[str, int]:
    ids = {}
    for state in (
        "succeeded",
        "awaiting_approval",
        "rejected",
        "expired",
        "blocked",
        "delivery_uncertain",
    ):
        row = await p2.seed(state=state, text=f"P2 outcome {state}.")
        ids[state] = row.id
        if state == "succeeded":
            async with transaction(p2.env.factory) as uow:
                uow.session.add(
                    AuditLogEntry(
                        account_id=p2.account_id,
                        actor="owner",
                        event="action_approved",
                        subject_type="action",
                        subject_id=row.id,
                        details={"version": 1},
                        at=p2.env.clock.now(),
                    )
                )
    return ids


async def activity_tool(p2: Scenario) -> list[dict[str, Any]]:
    await p2.owner(
        "get_activity",
        since=None,
        until=None,
        conversation_id=None,
        contact_id=None,
        show=None,
        limit=None,
    )
    [result] = [o for o in tool_outputs(p2) if "entries" in o]
    assert result["status"] == "ok"
    return result["entries"]


async def test_activity_lists_every_send_with_its_authorization(p2: Scenario) -> None:
    ids = await activity_mix(p2)
    await deliver(p2.env, private_message(OWNER, "/activity today"))
    rendered = p2.control_text()
    for state in ids:
        assert f"P2 outcome {state}." in rendered
    entries = await activity_tool(p2)
    assert {e["action_id"] for e in entries} == set(ids.values())
    sent = next(e for e in entries if e["action_id"] == ids["succeeded"])
    assert sent["authorized"]["version"] == 1
    assert sent["authorized"]["approved_at"] is not None
    assert "approved by you" in rendered
    assert p2.business_sends() == []


async def test_drafts_and_sends_are_never_shown_alike(p2: Scenario) -> None:
    ids = await activity_mix(p2)
    entries = {e["action_id"]: e for e in await activity_tool(p2)}
    assert entries[ids["succeeded"]]["group"] == "sent"
    assert entries[ids["awaiting_approval"]]["group"] == "awaiting"
    assert entries[ids["delivery_uncertain"]]["group"] == "uncertain"
    assert entries[ids["blocked"]]["group"] == "blocked"
    assert entries[ids["rejected"]]["group"] == "not_sent"
    await deliver(p2.env, private_message(OWNER, "/activity today"))
    rendered = p2.control_text().lower()
    assert "uncertain" in rendered
    assert "awaiting" in rendered
    assert "not sendable" in rendered or "blocked" in rendered
    assert p2.business_sends() == []


async def test_catch_up_mentions_what_the_assistant_sent_and_drafted(p2: Scenario) -> None:
    await activity_mix(p2)
    start = (p2.env.clock.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    await advance(p2, 1)  # catch-up periods are [start, end)
    # Exercise the public tool with its metadata-only fallback, independently
    # of a model's choice of final wording (the product app supplies the model).
    from assistant.agent.tools.registry import ToolContext, ToolServices
    from assistant.agent.tools.summaries import SummarizePeriodArgs, summarize_period_tool

    result = await summarize_period_tool(
        ToolContext(
            account_id=p2.account_id,
            run_id=1,
            run_type="owner_request",
            tainted=False,
            services=ToolServices(
                session_factory=p2.env.factory, clock=p2.env.clock, monthly_budget_usd=None
            ),
        ),
        SummarizePeriodArgs(
            since=start, until=None, conversation_id=p2.conversation_id, contact_id=None
        ),
    )
    assert result.status == "ok"
    [entry] = result.data["assistant_activity"]
    assert entry["conversation_id"] == p2.conversation_id
    assert entry["sent"] == entry["awaiting"] == 1
    assert entry["not_sent"] == 2
    assert entry["problems"] == 2
    assert p2.business_sends() == []
