"""The owner agent over the S02 business environment (S03.1).

`business_env` gives the real inbox, cards and stored conversations; this
adds the production LLM wiring (consent gate → metering → FakeLLM), a
FakeChannel for the agent's replies and cards, and a test-only `configure`
tool so the confirmation gate can be exercised before M03's real configure
tools exist (S03.3).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from assistant.accounts.models import AuditLogEntry, ControlTurn
from assistant.agent.confirmations import ConfirmationApplier
from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.summarize import SummarizeConfig, Summarizer
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRegistry,
    ToolServices,
    default_registry,
)
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.port import LLMRequest, TextItem, ToolResultItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM
from tests.integration.business_env import OWNER, Env, deliver, private_message

READ_FLAGS = frozenset(
    {
        "tool.get_status",
        "tool.search_messages",
        "tool.resolve_contact",
        "tool.get_conversation",
        "tool.list_conversations",
        "tool.summarize_period",
        "tool.get_attention_list",
    }
)
NOTE_FLAG = "tool.test_set_note"
ALL_FLAGS = READ_FLAGS | {NOTE_FLAG}
#: The S03.3 memory, preference and mode tools (not in ALL_FLAGS, so the
#: S03.1/S03.2 tests keep the tool offer they were written against).
MEMORY_FLAGS = frozenset(
    {
        "tool.remember",
        "tool.forget",
        "tool.list_memories",
        "tool.update_preferences",
        "tool.set_conversation_mode",
        "tool.set_contact_priority",
    }
)

#: `update_preferences`' S03.5 fields, unchanged. Every field of a strict
#: tool schema is required (null = leave as is; D52), so S03.3's calls
#: name them too.
S035_PREFS_UNCHANGED: dict[str, None] = {
    "reply_language": None,
    "reply_tone": None,
    "reply_style": None,
    "message_retention_days": None,
    "control_chat_retention_days": None,
    "raw_update_retention_days": None,
}
#: The S03.5 deletion tools (always a confirmation card).
DELETION_FLAGS = frozenset(
    {
        "tool.delete_conversation_data",
        "tool.delete_contact_data",
        "tool.delete_everything",
    }
)
#: Verbatim from docs/milestones/M03.md, "The production flags line" (what
#: production runs with). A unit test keeps it equal to every registered
#: tool's flag plus the background refresh (M03 fix round C2).
PRODUCTION_LINE = (
    "tool.get_status,tool.search_messages,tool.resolve_contact,tool.get_conversation,"
    "tool.list_conversations,tool.summarize_period,tool.get_attention_list,summary.refresh,"
    "tool.remember,tool.forget,tool.list_memories,tool.set_conversation_mode,"
    "tool.update_preferences,tool.delete_conversation_data,tool.delete_contact_data,"
    "tool.delete_everything,tool.set_contact_priority,tool.create_monitor,tool.list_monitors,"
    "tool.stop_monitor"
)
PRODUCTION_FLAGS = frozenset(PRODUCTION_LINE.split(","))


class SetNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(description="A synthetic note.")


#: Notes that make the test tool misbehave, for the applier's failure paths.
NOTE_EXPLODE = "explode"
NOTE_ODD_STATUS = "odd-status"
NOTE_CANCEL = "cancel"
NOTE_NOT_IN_ACCOUNT = "not-in-account"  # an id that is not the account's
NOTE_KEY_ERROR = "key-error"  # a lookup bug, not "not found" (C2)


async def set_note(ctx: ToolContext, args: SetNoteArgs) -> ToolOutcome:
    """A state change: one content-free audit row per execution."""
    if args.note == NOTE_EXPLODE:
        raise RuntimeError("synthetic handler failure")
    if args.note == NOTE_NOT_IN_ACCOUNT:
        raise NotInAccountError("synthetic id not in this account")
    if args.note == NOTE_KEY_ERROR:
        raise KeyError("synthetic")
    if args.note == NOTE_CANCEL:
        raise asyncio.CancelledError  # a shutdown mid-way
    if args.note == NOTE_ODD_STATUS:
        return ToolOutcome("awaiting_approval")
    async with transaction(ctx.services.session_factory) as uow:
        uow.session.add(
            AuditLogEntry(
                account_id=ctx.account_id,
                at=ctx.services.clock.now(),
                actor="agent",
                event="test_note_set",
                details={"length": len(args.note)},
            )
        )
    return ToolOutcome("ok")


#: Test-only: a `configure` tool, registered only in `registry_with_note_tool()`.
SET_NOTE = ToolDefinition(
    name="test_set_note",
    description="Test-only configure tool.",
    args_model=SetNoteArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=set_note,
    release_flag=NOTE_FLAG,
)


def registry_with_note_tool() -> ToolRegistry:
    registry = default_registry()
    registry.register(SET_NOTE)
    return registry


def gated(env: Env, fake: FakeLLM) -> ConsentGatedLLMClient:
    """The production LLM wiring over FakeLLM: consent gate → metering → fake."""
    return ConsentGatedLLMClient(
        MeteredLLMClient(fake, DbCallRecorder(env.factory, env.clock)), DbConsentReader(env.factory)
    )


def summarizer(env: Env, fake: FakeLLM, **config: Any) -> Summarizer:
    """The `summarize` run type (S03.2) over the same wiring."""
    values: dict[str, Any] = {"model": "gpt-6-luna"}
    values.update(config)
    return Summarizer(
        session_factory=env.factory,
        llm=gated(env, fake),
        clock=env.clock,
        config=SummarizeConfig(**values),
    )


def runtime(
    env: Env,
    fake: FakeLLM,
    channel: FakeChannel,
    *,
    flags: frozenset[str] = ALL_FLAGS,
    registry: ToolRegistry | None = None,
    with_summaries: bool = False,
) -> OwnerRequestRuntime:
    return OwnerRequestRuntime(
        session_factory=env.factory,
        llm=gated(env, fake),
        channel=channel,
        registry=registry or registry_with_note_tool(),
        clock=env.clock,
        config=AgentConfig(model="gpt-6-luna", enabled_flags=flags),
        summarizer=summarizer(env, fake) if with_summaries else None,
    )


def applier(
    env: Env, channel: FakeChannel, *, flags: frozenset[str] = ALL_FLAGS
) -> ConfirmationApplier:
    return ConfirmationApplier(
        session_factory=env.factory,
        clock=env.clock,
        channel=channel,
        registry=registry_with_note_tool(),
        services=ToolServices(
            session_factory=env.factory, clock=env.clock, monthly_budget_usd=None, channel=channel
        ),
        enabled_flags=flags,
    )


async def ask(env: Env, question: str, **extra: Any) -> int:
    """The owner types `question` (plus any payload fields); returns the turn id."""
    update = private_message(OWNER, question)
    update["message"].update(extra)
    await deliver(env, update)
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    select(ControlTurn.id)
                    .where(ControlTurn.role == "owner")
                    .order_by(ControlTurn.id.desc())
                    .limit(1)
                )
            ).scalar_one()
        )


async def one_id(env: Env, sql: str, **params: Any) -> int:
    async with env.factory() as session:
        return int((await session.execute(text(sql), params)).scalar_one())


async def note_rows(env: Env) -> int:
    async with env.factory() as session:
        rows = await session.execute(
            select(AuditLogEntry).where(AuditLogEntry.event == "test_note_set")
        )
        return len(rows.scalars().all())


def request_texts(request: LLMRequest) -> str:
    """Everything a request would have sent, as one string, for assertions."""
    parts = [request.instructions]
    for item in request.items:
        if isinstance(item, TextItem):
            parts.append(item.text)
        elif isinstance(item, ToolResultItem):
            parts.append(item.output)
    return "\n".join(parts)


def args(**values: Any) -> str:
    return json.dumps(values)
