"""Real inbox/runtime/tools/cards/policy/jobs over synthetic PostgreSQL state.

Fixture methods also support adaptive scripts and event barriers, which deliberately
are Python fixtures rather than executable strings in the serialized case format.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Any

from sqlalchemy import func, select

from assistant.accounts.models import Account, AuditLogEntry, Consent, ControlTurn
from assistant.actions.card_facts import presentation_facts
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest, OwnerRequestSource
from assistant.actions.requests import (
    AddSources,
    OwnerRequestView,
    RequestApplied,
    RequestSource,
    SelectRecipient,
    SetState,
    SourceSpan,
    load_request,
    open_request,
    update_request,
)
from assistant.agent.models import AgentRun
from assistant.bootstrap import build_owner_runtime, build_summarizer
from assistant.config import Settings
from assistant.conversations.models import Contact as ContactRow
from assistant.conversations.models import Conversation, Message
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest, LLMResponse, ToolResultItem
from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.control.approvals import shown
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.clock import FakeGatewayClock
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
from tests.journeys.grading import CardArtifact, assert_card, assert_claim, assert_equal
from tests.journeys.locales import ASSISTANT_LABEL, OWNER_LABEL
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
from tests.scenarios.p2_drive import Scenario, entered_or_finished, revision_result, run_queued

JOURNEY_FLAGS = PRODUCTION_FLAGS | {
    "actions.send",
    "assist.drafts",
    "tool.propose_owner_message",
    "tool.remember_owner_sources",
}
_CALL_IDS = count(1)
EXECUTING_ACTION: ContextVar[int | None] = ContextVar("journey_executing_action", default=None)


@dataclass(frozen=True)
class BusinessDispatch:
    """Committed facts read at one actual fake transport call, never pooled."""

    call: dict[str, Any]
    action_id: int | None
    attempt_id: str | None = None
    version: int | None = None
    state: str | None = None
    approved_text_hash: str | None = None
    telegram_chat_id: int | None = None
    claims: int = 0


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
    flags: frozenset[str] = JOURNEY_FLAGS
    chats: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    cards: dict[str, int] = field(default_factory=dict)
    tokens: dict[tuple[str, int], str] = field(default_factory=dict)
    messages: dict[str, tuple[str, int]] = field(default_factory=dict)
    turns: list[int] = field(default_factory=list)
    last_reply: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    dispatches: list[BusinessDispatch] = field(default_factory=list)
    _message_number: int = 10000

    def __post_init__(self) -> None:
        self.chats["k"] = self.s.conversation_id

    def record(self, kind: str, **facts: Any) -> None:
        self.events.append({"sequence": len(self.events) + 1, "kind": kind, **facts})

    async def observe_business_call(self, call: dict[str, Any]) -> None:
        action_id = EXECUTING_ACTION.get()
        async with self.s.env.factory() as session:
            result = (
                await session.execute(
                    select(Action, Conversation.business_chat_id)
                    .join(Conversation, Conversation.id == Action.conversation_id)
                    .where(
                        Action.account_id == self.s.account_id,
                        Action.id == action_id,
                        Conversation.account_id == self.s.account_id,
                    )
                )
            ).one_or_none()
            if result is None:
                self.dispatches.append(BusinessDispatch(call, action_id))
                return
            row, telegram_chat = result
            claims = await session.scalar(
                select(func.count(AuditLogEntry.id)).where(
                    AuditLogEntry.account_id == self.s.account_id,
                    AuditLogEntry.subject_type == "action",
                    AuditLogEntry.subject_id == row.id,
                    AuditLogEntry.event == "action_claimed",
                    AuditLogEntry.details["attempt_id"].astext == row.attempt_id,
                    AuditLogEntry.details["version"].as_integer() == row.version,
                )
            )
            self.dispatches.append(
                BusinessDispatch(
                    call=call,
                    action_id=row.id,
                    attempt_id=row.attempt_id,
                    version=row.version,
                    state=row.state,
                    approved_text_hash=row.approved_text_hash,
                    telegram_chat_id=telegram_chat,
                    claims=claims or 0,
                )
            )

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
        assert row is not None, (
            f"card {alias!r} (action {self.cards[alias]}): expected available action, actual None"
        )
        return row

    async def directory(self, alias: str, name: str) -> int:
        self.names[alias] = name
        if alias == "k":
            async with transaction(self.s.env.factory) as uow:
                chat = await uow.session.get(Conversation, self.chats[alias])
                assert chat is not None, (
                    f"directory {alias!r}: expected conversation {self.chats[alias]}, actual None"
                )
                contact = await uow.session.get(ContactRow, chat.contact_id)
                assert contact is not None, (
                    f"directory {alias!r}: expected contact {chat.contact_id}, actual None"
                )
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
            assert chat is not None, (
                f"directory {alias!r}: expected conversation for Telegram id {telegram_id}, "
                "actual None"
            )
            self.chats[alias] = chat.id
        return chat.id

    async def language(self, locale: Locale) -> None:
        self.locale = locale
        async with transaction(self.s.env.factory) as uow:
            account = await uow.session.get(Account, self.s.account_id)
            assert account is not None, (
                f"language: expected account {self.s.account_id}, actual None"
            )
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
            assert row.card_message_id is not None, (
                f"reply target {reply_to!r}: expected card message id, actual None"
            )
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
            await self._run_owner(turn_id, start)
        return turn_id

    async def _run_owner(self, turn_id: int, delivery_start: int) -> None:
        jobs = await run_queued(self.s, "agent.owner_request", limit=1)
        assert_equal(jobs, 1, field=f"owner turn {turn_id} jobs run")
        self.last_reply = "\n".join(
            item["text"] for item in self.s.env.sender.delivered_control_sends[delivery_start:]
        )

    def outputs(self) -> list[dict[str, Any]]:
        return [
            json.loads(item.output)
            for request in self.s.fake.requests
            for item in request.items
            if isinstance(item, ToolResultItem)
        ]

    async def bind_card(self, alias: str, chat: str = "k") -> Action:
        candidates = [row for row in await self.rows() if row.conversation_id == self.chats[chat]]
        assert candidates, (
            f"bind card {alias!r}: expected at least one action for chat {chat!r}, actual 0"
        )
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
            assert request_id is not None, (
                "owner request: expected persisted request id, actual None"
            )
            view = await load_request(session, account_id=self.s.account_id, request_id=request_id)
        assert view is not None, f"request {request_id}: expected available request, actual None"
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

        async def revision(model_request: LLMRequest) -> LLMResponse:
            row = await self.action(alias)
            # H4 selects the schema per run; the unfiltered registry retains
            # the legacy definition so flag-off runs keep their existing path.
            tool = next(tool for tool in model_request.tools if tool.name == "revise_draft")
            fields = tool.parameters["properties"]
            if "expected_version" in fields:
                request = await self.request()
                fences = [o["context_fence_id"] for o in self.outputs() if "context_fence_id" in o]
                assert fences, "rewrite read: expected at least one context fence, actual 0"
                sources = await self.source_ids(turn_id=self.turns[-1]) if append else []
                if append:
                    assert sources, "append: expected at least one exact owner source, actual 0"
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
            if "literal" in fields:
                literal: dict[str, Any] | None = None
                if append:
                    # As dictate() does for baseline H3, classify this fixture's
                    # entire owner turn explicitly; its leading space is source
                    # text too. H6 owns natural-language span classification.
                    request = await self.request()
                    async with transaction(self.s.env.factory) as uow:
                        turn = await uow.session.scalar(
                            select(ControlTurn).where(
                                ControlTurn.account_id == self.s.account_id,
                                ControlTurn.id == self.turns[-1],
                                ControlTurn.role == "owner",
                            )
                        )
                        assert turn is not None
                        assert turn.text is not None
                        added = await update_request(
                            uow.session,
                            account_id=self.s.account_id,
                            request_id=request.request_id,
                            expected_version=request.version,
                            update=AddSources(
                                sources=(
                                    RequestSource(
                                        span=SourceSpan(
                                            turn_id=turn.id,
                                            field="text",
                                            start=0,
                                            end=len(turn.text),
                                            field_sha256=self.text_hash(turn.text),
                                        ),
                                        purpose="literal_body",
                                    ),
                                )
                            ),
                            now=self.s.env.clock.now(),
                        )
                        assert isinstance(added, RequestApplied)
                        literal = {
                            "request_id": added.view.request_id,
                            "request_version": added.view.version,
                            "expected_version": row.version,
                            "edit": "literal_append",
                            "source_ids": [added.view.source_ids[-1]],
                        }
                else:
                    self.s.fake.parsed.append(revision_result(text=text))
                return call(
                    "revise_draft",
                    conversation_id=None,
                    action_id=row.id,
                    literal=literal,
                    min_words=None,
                    max_words=None if append else 10,
                )
            if not append:
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
            assert sources, "dictation: expected at least one retained literal source, actual 0"
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
            assert sources, "selection: expected at least one retained suggestion source, actual 0"
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
            assert fences, "managed read: expected at least one context fence (H10), actual 0"
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
        """Baseline fixture with explicit C1 classification of an ingested turn.

        H3 consumes classified request spans; it does not open requests from
        natural language. Only this baseline helper supplies that prerequisite.
        Contract journeys use source_proposal directly and must establish it
        through the runtime themselves; their missing-foundation checks stay strict.
        """
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
        framing = f"{self.names[chat]} — {prefixes[self.locale]}: "
        start = len(self.s.env.sender.delivered_control_sends)
        turn_id = await self.owner(
            framing + text,
            self.source_proposal(chat),
            text_response(awaiting[self.locale]),
            run=False,
        )
        async with transaction(self.s.env.factory) as uow:
            turn = await uow.session.scalar(
                select(ControlTurn).where(
                    ControlTurn.account_id == self.s.account_id,
                    ControlTurn.id == turn_id,
                    ControlTurn.role == "owner",
                )
            )
            assert turn is not None, (
                f"dictation: expected ingested owner turn {turn_id}, actual None"
            )
            assert_equal(turn.text, framing + text, field="dictation source field")
            assert turn.text is not None
            request = await open_request(
                uow.session,
                account_id=self.s.account_id,
                turn_id=turn.id,
                operation="dictate",
                sources=(
                    RequestSource(
                        span=SourceSpan(
                            turn_id=turn.id,
                            field="text",
                            start=len(framing),
                            end=len(turn.text),
                            field_sha256=self.text_hash(turn.text),
                        ),
                        purpose="literal_body",
                    ),
                ),
                now=self.s.env.clock.now(),
            )
            contact_id = await uow.session.scalar(
                select(Conversation.contact_id).where(
                    Conversation.account_id == self.s.account_id,
                    Conversation.id == self.chats[chat],
                )
            )
            assert contact_id is not None, (
                f"dictation: expected contact for chat {chat!r}, actual None"
            )
            selected = await update_request(
                uow.session,
                account_id=self.s.account_id,
                request_id=request.request_id,
                expected_version=request.version,
                update=SelectRecipient(
                    contact_id=contact_id,
                    conversation_id=self.chats[chat],
                    basis="owner_choice",
                ),
                now=self.s.env.clock.now(),
            )
            assert isinstance(selected, RequestApplied), (
                f"dictation recipient: expected applied, actual {selected.status!r}"
            )
            ready = await update_request(
                uow.session,
                account_id=self.s.account_id,
                request_id=request.request_id,
                expected_version=selected.view.version,
                update=SetState(state="ready", reason=None),
                now=self.s.env.clock.now(),
            )
            assert isinstance(ready, RequestApplied), (
                f"dictation readiness: expected applied, actual {ready.status!r}"
            )
        await self._run_owner(turn_id, start)
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
                assert chat is not None, (
                    f"contact event: expected conversation {self.chats[step.chat]}, actual None"
                )
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
                assert chat is not None, (
                    f"{step.kind} event: expected conversation {self.chats[chat_alias]}, "
                    "actual None"
                )
                telegram_chat = chat.business_chat_id
                original = await session.scalar(
                    select(Message).where(
                        Message.account_id == self.s.account_id,
                        Message.conversation_id == chat.id,
                        Message.telegram_message_id == telegram_message,
                    )
                )
                assert original is not None, (
                    f"{step.kind} event: expected message {step.alias!r}, actual None"
                )
                assert original.sent_at is not None, (
                    f"{step.kind} event: expected sent_at for message {step.alias!r}, actual None"
                )
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
            feature_flags=self.flags,
        )
        # I17 checks actual dispatch time after a throttle wait. Keep gateway
        # time advancing locally, as in m04_environment, without moving job time.
        gateway_clock = FakeGatewayClock(self.s.env.clock)
        channel = GatewayControlChannel(
            OutboundGateway(self.s.env.sender, gateway_clock, sleep=gateway_clock.sleep)
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
                assert proof is not None, (
                    f"card {check.alias!r} authorship: expected version proof, actual None"
                )
                assert_equal(proof["version"], check.version, field="card authorship version")
                assert_equal(proof["kind"], check.authorship, field="card authorship kind")
            if check.presented:
                assert row.card_message_id is not None, (
                    f"card {check.alias!r} presentation: expected control message id, actual None"
                )
                assert_equal(row.card_state, row.state, field="presented card state")
                async with self.s.env.factory() as session:
                    facts = await presentation_facts(
                        session, account_id=self.s.account_id, action_ids=[row.id]
                    )
                    presentation = await session.scalar(
                        select(AuditLogEntry)
                        .where(
                            AuditLogEntry.account_id == self.s.account_id,
                            AuditLogEntry.subject_type == "action",
                            AuditLogEntry.subject_id == row.id,
                            AuditLogEntry.event == "card_presented",
                        )
                        .order_by(AuditLogEntry.id.desc())
                        .limit(1)
                    )
                assert row.id in facts, (
                    f"card presentation: expected action {row.id} in facts, "
                    f"actual {sorted(facts)!r}"
                )
                assert_equal(facts[row.id].outcome, "shown", field="card presentation outcome")
                assert_equal(facts[row.id].version, row.version, field="card presentation version")
                assert presentation is not None, (
                    "card presentation: expected a recorded card_presented event, actual None"
                )
                for key, expected in (
                    ("control_message_id", row.card_message_id),
                    ("version", check.version),
                    ("state", check.state),
                ):
                    assert_equal(
                        presentation.details.get(key), expected, field=f"card presentation {key}"
                    )
                self.assert_card_delivery(row, check)
        elif isinstance(check, RequestCheck):
            request = await self.request()
            assert_equal(request.state, check.state, field="request state")
            assert_equal(request.missing_slot, check.slot, field="request missing_slot")
            if check.chat is not None:
                assert_equal(
                    request.conversation_id,
                    self.chats[check.chat],
                    field=f"request conversation_id for {check.chat!r}",
                )
        elif isinstance(check, CountCheck):
            counts = {
                "actions": len(await self.rows()),
                "business_attempts": len(self.s.business_sends()),
                "nested_writers": len(
                    [r for r in self.s.fake.requests if r.purpose == "draft_reply"]
                ),
            }
            assert_equal(counts[check.artifact], check.count, field=f"{check.artifact} count")
        elif isinstance(check, ReplyCheck):
            for text in check.contains:
                assert text in self.last_reply, (
                    f"owner reply: expected to contain {text!r}, actual {self.last_reply!r}"
                )
            for text in check.excludes:
                assert text not in self.last_reply, (
                    f"owner reply: expected to exclude {text!r}, actual {self.last_reply!r}"
                )
        elif isinstance(check, FactualReplyCheck):
            assert check.wording in self.last_reply, (
                f"factual reply for card {check.card!r}: "
                f"expected to contain {check.wording!r}, actual {self.last_reply!r}"
            )
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

    def assert_card_delivery(self, row: Action, check: CardCheck) -> None:
        # A Telegram message is sent once and then edited in place. Only its
        # latest successful edit (or initial send) is its current presentation.
        sender = self.s.env.sender
        deliveries = [
            item
            for item in [*sender.delivered_control_sends, *sender.delivered_control_edits]
            if item.get("message_id") == row.card_message_id
        ]
        assert deliveries, (
            f"card {check.alias!r}: expected delivery of message {row.card_message_id}, actual []"
        )
        delivery = deliveries[-1]
        assert_equal(delivery["chat_id"], OWNER, field="presented card control recipient")
        rendered = delivery["text"]
        recipients = re.findall(r"^(?:To|Kimga|Кимга|Кому): <b>(.*?)</b>$", rendered, re.MULTILINE)
        assert_equal(
            [html.unescape(name) for name in recipients],
            [html.unescape(shown(self.names[check.chat], one_line=True))],
            field="presented card recipient",
        )
        bodies = re.findall(r"<blockquote>(.*?)</blockquote>", rendered, re.DOTALL)
        assert_equal(
            [html.unescape(body) for body in bodies],
            [html.unescape(shown(check.text))],
            field="presented card body",
        )
        proof = getattr(row, "authorship", None)
        generated = row.origin != "owner_dictated"
        if proof is not None:
            generated = proof.get("retains_generated_text", False) or proof["kind"] in (
                "assistant_written",
                "agent_proposal_selected",
            )
        labels = (
            (
                ASSISTANT_LABEL[self.locale],
                "Draft reply",
                "New message",
                "Assist draft",
                "Message the assistant suggested",
            )
            if generated
            else (OWNER_LABEL[self.locale], "Message you dictated")
        )
        # The body/quoted contact text cannot supply the authorship label.
        header = html.unescape(rendered.split("\n", 1)[0])
        assert any(label in header for label in labels), (
            f"presented card authorship: expected one of {labels!r}, actual {header!r}"
        )
        if row.state == "awaiting_approval":
            markup = delivery["reply_markup"]
            if hasattr(markup, "model_dump"):
                markup = markup.model_dump()
            callbacks = [
                button.get("callback_data")
                for line in (markup or {}).get("inline_keyboard", [])
                for button in line
            ]
            assert f"act:{row.approval_token}:ok" in callbacks, (
                f"presented card version: expected current approval token for v{row.version}, "
                f"actual {callbacks!r}"
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
        statuses = [value.status for value in values]
        assert all(status == "validated" for status in statuses), (
            f"request source statuses: expected all 'validated', actual {statuses!r}"
        )
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
        assert value is not None, f"message {alias!r}: expected persisted message id, actual None"
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
            "flags": sorted(self.flags),
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
        dispatched: set[tuple[int, str]] = set()
        for attempt in attempts:
            observations = [item for item in self.dispatches if item.call is attempt]
            assert_equal(len(observations), 1, field="business call dispatch observations")
            [evidence] = observations
            assert evidence.action_id is not None, (
                "business call: expected executing action identity, actual None"
            )
            assert evidence.attempt_id is not None, (
                f"business call action {evidence.action_id}: expected committed attempt_id, "
                "actual None"
            )
            assert_equal(
                evidence.state, "executing", field="business call action state at dispatch"
            )
            assert_equal(evidence.claims, 1, field="business call committed claims for attempt")
            key = (evidence.action_id, evidence.attempt_id)
            assert key not in dispatched, (
                f"calls per action attempt: expected at most 1 for {key!r}, actual at least 2"
            )
            dispatched.add(key)
            assert_equal(
                attempt.get("chat_id"), evidence.telegram_chat_id, field="business call recipient"
            )
            assert_equal(
                self.text_hash(str(attempt.get("text", ""))),
                evidence.approved_text_hash,
                field="business call approved text SHA-256",
            )
