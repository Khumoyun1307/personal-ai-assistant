"""Real inbox/runtime/tools/cards/policy/jobs over synthetic PostgreSQL state.

Fixture methods also support adaptive scripts and event barriers, which deliberately
are Python fixtures rather than executable strings in the serialized case format.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Any

from sqlalchemy import select

from assistant.accounts.models import Account, AuditLogEntry, Consent
from assistant.actions.card_facts import presentation_facts
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest, OwnerRequestSource
from assistant.actions.requests import OwnerRequestView, load_request
from assistant.agent.models import AgentRun
from assistant.agent.tools.registry import default_registry
from assistant.bootstrap import build_owner_runtime, build_summarizer
from assistant.config import Settings
from assistant.conversations.models import Contact as ContactRow
from assistant.conversations.models import Conversation, Message
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest, LLMResponse, ToolResultItem
from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.clock import FakeSleep
from tests.fakes.llm import Script, text_response, tool_response
from tests.fakes.sender import network_error, telegram_error
from tests.integration.agent_env import PRODUCTION_FLAGS, args, ask, gated
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    business_message,
    callback,
    deleted_messages,
    deliver,
    private_message,
)
from tests.journeys.grading import CardArtifact, assert_card, assert_claim
from tests.journeys.models import (
    AdvanceClock,
    ArtifactCheck,
    CardCheck,
    Contact,
    CountCheck,
    DeleteMessage,
    DirectoryEntry,
    EditMessage,
    FactualReplyCheck,
    Fail,
    JourneyCase,
    JourneyStep,
    Locale,
    Owner,
    ReplyCheck,
    RequestCheck,
    Restart,
    Revoke,
    Tap,
)
from tests.scenarios.p2_drive import Scenario, entered_or_finished, run_queued

JOURNEY_FLAGS = PRODUCTION_FLAGS | {
    "actions.send",
    "assist.drafts",
    "tool.propose_owner_message",
}
_CALL_IDS = count(1)


def call(name: str, /, **values: Any) -> LLMResponse:
    """Keep the tool name positional so every keyword belongs to its arguments."""
    return tool_response((f"journey-{next(_CALL_IDS)}", name, args(**values)))


@dataclass
class ModelBarrier:
    """An explicit provider boundary, with no sleeps or wall-clock ordering."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def response(self, response: LLMResponse) -> Script:
        async def held(_request: LLMRequest) -> LLMResponse:
            self.entered.set()
            await self.release.wait()
            return response

        return held


@dataclass
class Journey:
    s: Scenario
    case_id: str = "fixture-journey"
    locale: Locale = "en"
    chats: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    cards: dict[str, int] = field(default_factory=dict)
    tokens: dict[tuple[str, int], str] = field(default_factory=dict)
    messages: dict[str, tuple[str, int]] = field(default_factory=dict)
    turns: list[int] = field(default_factory=list)
    last_reply: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    _message_number: int = 10000

    def __post_init__(self) -> None:
        self.chats["k"] = self.s.conversation_id

    def record(self, kind: str, **facts: Any) -> None:
        self.events.append({"sequence": len(self.events) + 1, "kind": kind, **facts})

    async def rows(self) -> list[Action]:
        async with self.s.env.factory() as session:
            return list(
                await session.scalars(
                    select(Action).where(Action.account_id == self.s.account_id).order_by(Action.id)
                )
            )

    async def action(self, alias: str) -> Action:
        async with self.s.env.factory() as session:
            row = await session.scalar(
                select(Action).where(
                    Action.account_id == self.s.account_id, Action.id == self.cards[alias]
                )
            )
        assert row is not None, "selected artifact is unavailable"
        return row

    async def directory(self, alias: str, name: str) -> int:
        self.names[alias] = name
        if alias == "k":
            async with transaction(self.s.env.factory) as uow:
                chat = await uow.session.get(Conversation, self.chats[alias])
                assert chat is not None
                contact = await uow.session.get(ContactRow, chat.contact_id)
                assert contact is not None
                contact.display_name = name
            return self.chats[alias]
        telegram_id = CONTACT + 100 + len(self.chats)
        await deliver(
            self.s.env,
            business_message(
                from_id=telegram_id,
                chat_id=telegram_id,
                date=self.s.date,
                first_name=name,
                message_text="CANARY-SYNTH unrelated observed chat.",
            ),
        )
        async with self.s.env.factory() as session:
            chat = await session.scalar(
                select(Conversation).where(
                    Conversation.account_id == self.s.account_id,
                    Conversation.business_chat_id == telegram_id,
                )
            )
            assert chat is not None
            self.chats[alias] = chat.id
        return chat.id

    async def language(self, locale: Locale) -> None:
        self.locale = locale
        async with transaction(self.s.env.factory) as uow:
            account = await uow.session.get(Account, self.s.account_id)
            assert account is not None
            account.locale_pref = locale

    async def owner(
        self,
        text: str,
        *script: Script,
        reply_to: str | None = None,
        quote: str | None = None,
        run: bool = True,
    ) -> int:
        extra: dict[str, Any] = {}
        if reply_to is not None:
            row = await self.action(reply_to)
            assert row.card_message_id is not None
            extra["reply_to_message"] = {
                "message_id": row.card_message_id,
                "date": self.s.date,
                "chat": {"id": OWNER, "type": "private"},
            }
        if quote is not None:
            extra["quote"] = {"text": quote, "position": 0, "is_manual": True}
        self.s.fake.queue(*script)
        start = len(self.s.env.sender.delivered_control_sends)
        turn_id = await ask(self.s.env, text, **extra)
        self.turns.append(turn_id)
        self.record("owner", turn_id=turn_id, text_sha256=self.text_hash(text))
        if run:
            assert await run_queued(self.s, "agent.owner_request", limit=1) == 1
            self.last_reply = "\n".join(
                item["text"] for item in self.s.env.sender.delivered_control_sends[start:]
            )
        return turn_id

    def outputs(self) -> list[dict[str, Any]]:
        return [
            json.loads(item.output)
            for request in self.s.fake.requests
            for item in request.items
            if isinstance(item, ToolResultItem)
        ]

    async def bind_card(self, alias: str, chat: str = "k") -> Action:
        candidates = [row for row in await self.rows() if row.conversation_id == self.chats[chat]]
        assert candidates, "proposal did not create an artifact"
        row = candidates[-1]
        self.cards[alias] = row.id
        if row.approval_token:
            self.tokens[(alias, row.version)] = row.approval_token
        return row

    async def request(self, request_id: int | None = None) -> OwnerRequestView:
        async with self.s.env.factory() as session:
            if request_id is None:
                request_id = await session.scalar(
                    select(OwnerRequest.id)
                    .where(OwnerRequest.account_id == self.s.account_id)
                    .order_by(OwnerRequest.focused.desc(), OwnerRequest.id.desc())
                    .limit(1)
                )
            assert request_id is not None, "runtime did not persist the owner request (H2)"
            view = await load_request(session, account_id=self.s.account_id, request_id=request_id)
        assert view is not None, "request is unavailable"
        return view

    async def source_ids(
        self, *, turn_id: int | None = None, suggestion: bool = False
    ) -> list[int]:
        request = await self.request()
        async with self.s.env.factory() as session:
            statement = select(OwnerRequestSource.id).where(
                OwnerRequestSource.account_id == self.s.account_id,
                OwnerRequestSource.request_id == request.request_id,
                OwnerRequestSource.purpose == ("suggestion" if suggestion else "literal_body"),
            )
            if turn_id is not None:
                statement = statement.where(OwnerRequestSource.turn_id == turn_id)
            return list(await session.scalars(statement.order_by(OwnerRequestSource.ordinal)))

    def revision(self, alias: str, text: str, *, append: bool = False) -> Script:
        """Baseline/C2 adapter; final text always has an explicit artifact oracle."""

        async def revision(_request: LLMRequest) -> LLMResponse:
            row = await self.action(alias)
            tool = next(tool for tool in default_registry().all() if tool.name == "revise_draft")
            if "expected_version" in tool.args_model.model_fields:
                request = await self.request()
                fences = [o["context_fence_id"] for o in self.outputs() if "context_fence_id" in o]
                assert fences, "rewrite read did not issue a context fence"
                sources = await self.source_ids(turn_id=self.turns[-1]) if append else []
                if append:
                    assert sources, "append has no exact owner source"
                return call(
                    "revise_draft",
                    request_id=request.request_id,
                    request_version=request.version,
                    action_id=row.id,
                    expected_version=row.version,
                    edit="literal_append" if append else "rewrite",
                    text=None if append else text,
                    source_ids=sources,
                    context_fence_id=None if append else fences[-1],
                )
            if not append:
                from tests.scenarios.p2_drive import revision_result

                self.s.fake.parsed.append(revision_result(text=text))
            return call(
                "revise_draft",
                conversation_id=None,
                action_id=row.id,
                append_text=text if append else None,
                min_words=None,
                max_words=None if append else 10,
            )

        return revision

    def source_proposal(self, chat: str = "k") -> Script:
        async def proposal(_request: LLMRequest) -> LLMResponse:
            request = await self.request()
            sources = await self.source_ids()
            assert sources, "dictation has no retained literal source"
            return call(
                "propose_message",
                request_id=request.request_id,
                request_version=request.version,
                conversation_id=self.chats[chat],
                answered_message_id=None,
                source_ids=sources,
            )

        return proposal

    def selected_suggestion(self, chat: str = "k") -> Script:
        async def proposal(_request: LLMRequest) -> LLMResponse:
            request = await self.request()
            sources = await self.source_ids(suggestion=True)
            assert sources, "selection has no exact retained assistant suggestion"
            return call(
                "propose_message",
                request_id=request.request_id,
                request_version=request.version,
                conversation_id=self.chats[chat],
                answered_message_id=None,
                source_ids=sources,
            )

        return proposal

    def generated_proposal(
        self, text: str, *, chat: str = "k", target: int | None = None
    ) -> Script:
        async def proposal(_request: LLMRequest) -> LLMResponse:
            request = await self.request()
            fences = [o["context_fence_id"] for o in self.outputs() if "context_fence_id" in o]
            assert fences, "managed read did not issue a context fence (H10)"
            return call(
                "propose_owner_message",
                request_id=request.request_id,
                request_version=request.version,
                conversation_id=self.chats[chat],
                mode="new_message" if target is None else "reply",
                answered_message_id=target,
                text=text,
                context_fence_id=fences[-1],
            )

        return proposal

    def read(self, chat: str = "k", *, limit: int = 30) -> LLMResponse:
        return call(
            "get_conversation",
            conversation_id=self.chats[chat],
            contact_id=None,
            cursor=None,
            limit=limit,
        )

    async def dictate(self, text: str, *, alias: str = "k", chat: str = "k") -> Action:
        """Current API for baseline cases; source API automatically used after H3.

        Future-contract cases use source_proposal explicitly and never fall back.
        """
        tool = next(tool for tool in default_registry().all() if tool.name == "propose_message")
        fields = tool.args_model.model_fields
        script = (
            self.source_proposal(chat)
            if "source_ids" in fields
            else call(
                "propose_message",
                conversation_id=self.chats[chat],
                contact_id=None,
                text=text,
                answered_message_id=None,
            )
        )
        prefixes = {
            "uz-Latn": "Aynan yoz",
            "uz-Cyrl": "Айнан ёз",
            "ru": "Напиши дословно",
            "en": "Write exactly",
        }
        awaiting = {
            "uz-Latn": "Tasdiqlashingiz kutilmoqda.",
            "uz-Cyrl": "Тасдиқлашингиз кутилмоқда.",
            "ru": "Ожидает вашего подтверждения.",
            "en": "Awaiting your approval.",
        }
        await self.owner(
            f"{self.names[chat]} — {prefixes[self.locale]}: {text}",
            script,
            text_response(awaiting[self.locale]),
        )
        return await self.bind_card(alias, chat)

    async def tap(self, card: str, version: int, op: str) -> None:
        row = await self.action(card)
        self.record("tap", action_id=row.id, version=version, op=op)
        token = self.tokens[(card, version)]
        operations = {"approve": "ok", "reject": "rj", "edit": "ed", "redraft": "rd"}
        await deliver(
            self.s.env,
            callback(f"act:{token}:{operations[op]}", message_id=row.card_message_id or 1),
        )

    async def event(self, step: JourneyStep) -> None:
        self.record(step.kind, event_sha256=self.text_hash(step.model_dump_json()))
        if isinstance(step, Owner):
            script = [call(c.name, **c.arguments) for c in step.calls]
            await self.owner(
                step.text,
                *script,
                text_response(step.answer),
                reply_to=step.reply_to,
                quote=step.quote,
            )
        elif isinstance(step, Contact):
            self._message_number += 1
            async with self.s.env.factory() as session:
                chat = await session.get(Conversation, self.chats[step.chat])
                assert chat is not None
                telegram_chat = chat.business_chat_id
            await deliver(
                self.s.env,
                business_message(
                    chat_id=telegram_chat,
                    from_id=OWNER if step.owner_manual else telegram_chat,
                    message_id=self._message_number,
                    message_text=step.text,
                    date=self.s.date,
                ),
            )
            self.messages[step.alias] = (step.chat, self._message_number)
        elif isinstance(step, (EditMessage, DeleteMessage)):
            chat_alias, telegram_message = self.messages[step.alias]
            async with self.s.env.factory() as session:
                chat = await session.get(Conversation, self.chats[chat_alias])
                assert chat is not None
                telegram_chat = chat.business_chat_id
                original = await session.scalar(
                    select(Message).where(
                        Message.account_id == self.s.account_id,
                        Message.conversation_id == chat.id,
                        Message.telegram_message_id == telegram_message,
                    )
                )
                assert original is not None
                assert original.sent_at is not None
                original_date = int(original.sent_at.timestamp())
            if isinstance(step, EditMessage):
                payload = business_message(
                    chat_id=telegram_chat,
                    from_id=telegram_chat,
                    message_id=telegram_message,
                    message_text=step.text,
                    edit=True,
                    date=original_date,
                    edit_date=self.s.date,
                )
            else:
                payload = deleted_messages(chat_id=telegram_chat, message_ids=[telegram_message])
            await deliver(self.s.env, payload)
        elif isinstance(step, Tap):
            await self.tap(step.card, step.version, step.op)
        elif isinstance(step, Restart):
            await self.restart()
        elif isinstance(step, AdvanceClock):
            self.s.env.clock.advance(seconds=step.seconds)
        elif isinstance(step, Revoke):
            # Production privacy command revokes managed consent. Control revoke
            # uses the same persisted consent ledger without contacting a service.
            if step.scope == "ai_managed":
                await deliver(self.s.env, private_message(OWNER, "/privacy revoke"))
            else:
                async with transaction(self.s.env.factory) as uow:
                    rows = await uow.session.scalars(
                        select(Consent).where(
                            Consent.account_id == self.s.account_id,
                            Consent.kind == step.scope,
                            Consent.revoked_at.is_(None),
                        )
                    )
                    for row in rows:
                        row.revoked_at = self.s.env.clock.now()
        elif isinstance(step, Fail):
            error = (
                network_error()
                if step.outcome == "uncertain"
                else telegram_error("Synthetic refusal", status_code=400)
            )
            if step.stage == "send":
                self.s.env.sender.plain_script.append(error)
            elif step.stage == "presentation":
                self.s.env.sender.control_script.append(error)
            else:
                from assistant.llm.port import LLMError, LLMErrorKind

                self.s.fake.queue(LLMError(LLMErrorKind.UNAVAILABLE))

    async def restart(self) -> None:
        """Replace runtime/adapters; retain committed rows, FakeClock and transport.

        No environment context is reentered: it would TRUNCATE the database.
        Registered jobs consult the Scenario's current frozen handler bundle.
        """
        self.record("runtime_restart")
        settings = Settings(
            _env_file=None,
            database_url="postgresql+psycopg://unused:unused@localhost/assistant_test",
            bot_token=None,
            openai_api_key=None,
            owner_telegram_ids=(OWNER,),
            feature_flags=JOURNEY_FLAGS,
        )
        channel = GatewayControlChannel(
            OutboundGateway(self.s.env.sender, self.s.env.clock, sleep=FakeSleep())
        )
        client = gated(self.s.env, self.s.fake)
        summaries = build_summarizer(settings, self.s.env.factory, self.s.env.clock, client)
        wiring = self.s.wiring
        runtime = build_owner_runtime(
            settings,
            self.s.env.factory,
            channel,
            self.s.env.clock,
            client,
            summaries,
            drafter=wiring.drafter,
            approval_cards=wiring.approval_cards,
            actions_config=wiring.actions_config,
            card_reviser=wiring.card_reviser,
            notifications_enabled=wiring.notifications_enabled,
        )
        self.s.handlers = replace(self.s.handlers, owner_runtime=runtime)

    async def check(self, check: ArtifactCheck) -> None:
        if isinstance(check, CardCheck):
            row = await self.action(check.alias)
            assert_card(
                CardArtifact(
                    row.account_id,
                    row.conversation_id,
                    row.text,
                    row.version,
                    row.state,
                    row.origin,
                    row.state_reason,
                ),
                CardArtifact(
                    self.s.account_id,
                    self.chats[check.chat],
                    check.text,
                    check.version,
                    check.state,
                    check.origin,
                    check.reason,
                ),
            )
            if check.authorship is not None:
                proof = getattr(row, "authorship", None)
                assert proof is not None, "version authorship is unavailable"
                assert proof["version"] == check.version
                assert proof["kind"] == check.authorship
            if check.presented:
                assert row.card_message_id is not None
                assert row.card_state == row.state
                async with self.s.env.factory() as session:
                    facts = await presentation_facts(
                        session, account_id=self.s.account_id, action_ids=[row.id]
                    )
                assert row.id in facts
                assert facts[row.id].outcome == "shown"
                assert facts[row.id].version == row.version
                rendered = "\n".join(
                    call["text"]
                    for call in [
                        *self.s.env.sender.delivered_control_sends,
                        *self.s.env.sender.delivered_control_edits,
                    ]
                )
                assert check.text in html.unescape(rendered)
        elif isinstance(check, RequestCheck):
            request = await self.request()
            assert request.state == check.state
            assert request.missing_slot == check.slot
            if check.chat is not None:
                assert request.conversation_id == self.chats[check.chat]
        elif isinstance(check, CountCheck):
            counts = {
                "actions": len(await self.rows()),
                "business_attempts": len(self.s.business_sends()),
                "nested_writers": len(
                    [r for r in self.s.fake.requests if r.purpose == "draft_reply"]
                ),
            }
            assert counts[check.artifact] == check.count
        elif isinstance(check, ReplyCheck):
            for text in check.contains:
                assert text in self.last_reply
            for text in check.excludes:
                assert text not in self.last_reply
        elif isinstance(check, FactualReplyCheck):
            assert check.wording in self.last_reply
            row = await self.action(check.card)
            assert_claim(
                CardArtifact(
                    row.account_id,
                    row.conversation_id,
                    row.text,
                    row.version,
                    row.state,
                    row.origin,
                    row.state_reason,
                ),
                claimed_version=check.version,
                claimed_status=check.status,
                claimed_text_hash=check.text_sha256,
            )

    async def run(self, case: JourneyCase) -> None:
        await self.language(case.locale)
        for setup in case.setup:
            if isinstance(setup, DirectoryEntry):
                await self.directory(setup.alias, setup.name)
            else:
                await self.language(setup.locale)
        for step in case.steps:
            await self.event(step)
        for check in case.deterministic_checks:
            await self.check(check)

    async def hold(self, task: asyncio.Task[Any], barrier: ModelBarrier) -> None:
        await entered_or_finished(task, barrier.entered)
        self.record("model_barrier_entered")

    async def source_texts(self) -> tuple[str, ...]:
        request = await self.request()
        from assistant.actions.requests import load_source

        async with self.s.env.factory() as session:
            values = [
                await load_source(session, account_id=self.s.account_id, source=source)
                for source in request.sources
            ]
        assert all(value.status == "validated" for value in values)
        return tuple(value.text for value in values if value.status == "validated")

    async def message_id(self, alias: str) -> int:
        chat, telegram_id = self.messages[alias]
        async with self.s.env.factory() as session:
            value = await session.scalar(
                select(Message.id).where(
                    Message.account_id == self.s.account_id,
                    Message.conversation_id == self.chats[chat],
                    Message.telegram_message_id == telegram_id,
                )
            )
        assert value is not None
        return value

    async def runs(self) -> list[AgentRun]:
        async with self.s.env.factory() as session:
            return list(
                await session.scalars(
                    select(AgentRun)
                    .where(AgentRun.account_id == self.s.account_id)
                    .order_by(AgentRun.id)
                )
            )

    async def audit(self, alias: str, event: str) -> int:
        async with self.s.env.factory() as session:
            return len(
                list(
                    await session.scalars(
                        select(AuditLogEntry.id).where(
                            AuditLogEntry.account_id == self.s.account_id,
                            AuditLogEntry.subject_id == self.cards[alias],
                            AuditLogEntry.subject_type == "action",
                            AuditLogEntry.event == event,
                        )
                    )
                )
            )

    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    async def manifest(self) -> dict[str, Any]:
        """Content-free linkage for JUnit; no claims about real quality/cost/latency."""
        from assistant.agent.models import LlmCall

        rows = await self.rows()
        async with self.s.env.factory() as session:
            calls = list(
                await session.scalars(
                    select(LlmCall).where(LlmCall.account_id == self.s.account_id)
                )
            )
            requests = list(
                await session.scalars(
                    select(OwnerRequest)
                    .where(OwnerRequest.account_id == self.s.account_id)
                    .order_by(OwnerRequest.id)
                )
            )
            sources = list(
                await session.scalars(
                    select(OwnerRequestSource)
                    .where(OwnerRequestSource.account_id == self.s.account_id)
                    .order_by(OwnerRequestSource.id)
                )
            )
        return {
            "synthetic": True,
            "case_id": self.case_id,
            "locale": self.locale,
            "case_sha256": self.text_hash(
                json.dumps(
                    [
                        {k: v for k, v in event.items() if not k.endswith("_id")}
                        for event in self.events
                    ],
                    sort_keys=True,
                )
            ),
            "schema_sha256": self.text_hash(
                json.dumps(JourneyCase.model_json_schema(), sort_keys=True)
            ),
            "prompt_sha256": sorted({self.text_hash(r.instructions) for r in self.s.fake.requests}),
            "routes": sorted({(r.purpose, r.model, r.effort or "") for r in self.s.fake.requests}),
            "flags": sorted(JOURNEY_FLAGS),
            "events": self.events,
            "turn_ids": self.turns,
            "run_ids": [run.id for run in await self.runs()],
            "requests": [
                {
                    "id": row.id,
                    "version": row.version,
                    "state": row.state,
                    "action_id": row.action_id,
                    "action_version": row.action_version,
                    "trigger_turn_id": row.trigger_turn_id,
                }
                for row in requests
            ],
            "sources": [
                {
                    "id": row.id,
                    "request_id": row.request_id,
                    "turn_id": row.turn_id,
                    "purpose": row.purpose,
                    "field_sha256": row.field_sha256,
                }
                for row in sources
            ],
            "actions": [
                {"id": row.id, "version": row.version, "state": row.state, "run_id": row.run_id}
                for row in rows
            ],
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "fake_cost_estimate_usd": str(sum(c.cost_estimate or 0 for c in calls)),
            "business_attempts": len(self.s.business_sends()),
            "quality_review": "not_performed",
            "latency_evidence": "fake_clock_only",
        }

    async def assert_approved_attempts(self) -> None:
        """Teardown safety guard: xfail must not excuse an unauthorized send."""
        attempts = self.s.business_sends()
        if not attempts:
            return
        async with self.s.env.factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(Action, Conversation.business_chat_id)
                        .join(Conversation, Conversation.id == Action.conversation_id)
                        .where(Action.account_id == self.s.account_id)
                    )
                ).all()
            )
        for attempt in attempts:
            matches = [
                row
                for row, telegram_chat in rows
                if telegram_chat == attempt.get("chat_id")
                and row.approved_text_hash == self.text_hash(str(attempt.get("text", "")))
            ]
            assert matches, "business attempt has no exact owner-approved artifact"
            assert any(row.attempt_id is not None for row in matches)
        claims = 0
        for alias in self.cards:
            claims += await self.audit(alias, "action_claimed")
        assert len(attempts) <= claims, "business attempts exceed committed claims"
