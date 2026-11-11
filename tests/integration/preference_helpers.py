"""Synthetic exact-owner capture fixtures; no identity keys are assigned."""

from __future__ import annotations

from typing import Any

from assistant.accounts.models import ControlTurn
from assistant.agent.models import AgentRun
from assistant.agent.tools.memory import PREFERENCE_SOURCES_FLAG, RememberArgs
from assistant.agent.tools.registry import ToolContext, ToolServices
from assistant.memory.preference_proof import content_hash
from tests.integration.business_env import Env


def capture_args(turn_id: int, text: str, **changes: Any) -> RememberArgs:
    capture: dict[str, Any] = {
        "request_id": None,
        "request_version": None,
        "sources": [
            {
                "turn_id": turn_id,
                "field": "text",
                "start": 0,
                "end": len(text),
                "field_sha256": content_hash(text),
            }
        ],
        "kind": "style",
        "scope": "owner",
        "contact_id": None,
        "conversation_id": None,
        "applies_to_replies": False,
        "replaces_memory_id": None,
        **changes,
    }
    return RememberArgs.model_validate(
        {
            "content": text,
            "kind": capture["kind"],
            "contact_id": capture["contact_id"],
            "conversation_id": capture["conversation_id"],
            "applies_to_replies": capture["applies_to_replies"],
            "replaces_memory_id": capture["replaces_memory_id"],
            "capture": capture,
        }
    )


async def owner_capture(
    env: Env, account_id: int, text: str, **changes: Any
) -> tuple[ToolContext, RememberArgs]:
    async with env.factory() as session, session.begin():
        turn = ControlTurn(
            account_id=account_id,
            role="owner",
            text=text,
            owner_text_verified=True,
            created_at=env.clock.now(),
        )
        session.add(turn)
        await session.flush()
        run = AgentRun(
            account_id=account_id,
            run_type="owner_request",
            model="gpt-6-luna",
            trigger_ref=f"turn:{turn.id}",
            started_at=env.clock.now(),
        )
        session.add(run)
        await session.flush()
        ctx = ToolContext(
            account_id=account_id,
            run_id=run.id,
            run_type="owner_request",
            tainted=False,
            turn_id=turn.id,
            call_id="capture-preference",
            services=ToolServices(
                session_factory=env.factory,
                clock=env.clock,
                monthly_budget_usd=None,
                enabled_flags=frozenset({PREFERENCE_SOURCES_FLAG}),
            ),
        )
        return ctx, capture_args(turn.id, text, **changes)
