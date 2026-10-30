"""The `owner_drafting` target (PF-16b): owner-requested drafting and routing,
through the real owner runtime, on hand-written synthetic cases.

`python -m evals run owner_drafting` measures what the 2026-10-30 owner
playtest showed the product getting wrong (ISSUE-001 to ISSUE-009; plan
PF-10 to PF-15), **exactly as the product runs it**: per case, the owner's
message is stored as an owner control turn and the real `OwnerRequestRuntime`
answers it, with the shipped `owner_request` prompt, `default_registry()` with
the production release flags (`agent_mode.PRODUCTION_FLAGS`, which include
`tool.revise_draft` and `tool.get_draft`), the product's `Drafter`
(`draft_owner`), approval cards and `RevisionJob` (the `revise_draft` path),
a real `Summarizer` and a fixed clock. Nothing leaves the process: cards go
to an eval-side recording sender, the gateway has no business target (a
business send is refused locally), and the only way to send stays the
owner's Approve on a card, which nobody taps here.

The cases (`datasets/owner_drafting/cases.jsonl`, `OwnerDraftingCase`) are
**hand-written and synthetic** — never derived from the playtest chats
(AGENTS §8). Each case brings its own chats (a contact, messages minutes
before the fixed instant), the control-chat history before the owner's
message (an assistant suggestion list, a "draft is on its card" reply), an
optional live card (staged through the product's own `propose_draft`), the
owner's message, and what the result must show. Each case runs under its
**own synthetic account** in the throwaway database, so no case sees
another's chats, history or cards.

Deterministic checks (`check_case`, pure): which tools the run called or must
not have called (recorded by an instrumentation wrapper around every tool
handler: name, arguments, status); whether a card was staged or revised, on
which chat, its origin, mode (`new_message` has no base message) and the
answered message; the exact text of a dictated or picked text; words that
must or must not be in the card (amounts and clock times compared as values,
`agent_mode.value_key`); word bounds and "fewer words" counted by the
product's own `word_count`; an append that keeps the old text as its prefix;
the **same** card revised (version + 1, never a supersede); and in the reply,
names that must or must not appear and the live draft's exact word count. A
judge (rubric `owner_drafting.v1`) pre-screens only what a string cannot
decide: tone and carried substance. A judge failure leaves the case
unscreened (listed).

**Status: informational.** No metric has a threshold or a gate: the run
measures the remainder after the structural fixes honestly and compares the
`low` and `medium` efforts (owner decision D10), set through the existing
settings (`LLM_OWNER_REQUEST_EFFORT`, `LLM_DRAFT_REPLY_EFFORT`); each effort's
report has its own name.

Cost: the agent loop's worst case is far above a typical run (as for
retrieval's agent mode), so a full run starts on the expected cost of a
`--limit 1` trial passed with `--calibrate-from` (E-ab), always under the hard
in-flight cap of `BudgetedLLMClient`.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, BusinessConnection
from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    grant_consent,
    record_control_turn,
)
from assistant.actions.config import ActionsConfig
from assistant.actions.models import Action
from assistant.actions.states import ActionOrigin
from assistant.agent import prompt_registry
from assistant.agent.draft_owner_schemas import word_count
from assistant.agent.drafting import (
    DraftConfig,
    Drafter,
    DraftOutcome,
    DraftStatus,
    Stale,
    propose_draft,
)
from assistant.agent.models import AgentRun
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime, RunStatus
from assistant.agent.summarize import SummarizeConfig, Summarizer
from assistant.agent.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRegistry,
    default_registry,
)
from assistant.agent.tools.replies import normalise_for_match
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.channel import Button, Delivery
from assistant.core.model_routes import processor_for_model
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import SystemState
from assistant.db.uow import transaction
from assistant.llm.port import LLMClient, LLMError, LLMRequest, Provenance, StopReason, TextItem
from assistant.memory.search import detect_script, guess_lang, normalize
from assistant.telegram.control.approvals import ApprovalCards, RevisionJob
from assistant.telegram.gateway import OutboundGateway
from evals.agent_mode import (
    EVAL_POLICY_VERSION,
    MAX_ATTEMPTS,
    OUTAGE_ERRORS,
    PRODUCTION_FLAGS,
    RETRY_DELAYS,
    SUMMARY_CALLS_PER_QUERY,
    value_key,
)
from evals.assets import load_asset
from evals.budget import BudgetExceededError
from evals.corpus import OWNER_TIMEZONE, scratch_database
from evals.errors import RefusedError
from evals.metrics import contains_words, ratio_metric
from evals.review import ReviewLedger
from evals.schema import fold
from evals.targets import (
    DatasetName,
    Measure,
    MetricResult,
    PlannedCall,
    RunContext,
    TargetResult,
)

ROUTE = "owner_request"
SET_NAME = "owner-drafting"
DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "owner_drafting" / "cases.jsonl"
JUDGE_PURPOSE = "eval_owner_drafting_judge"
RUBRIC_ID = "owner_drafting.v1"
JUDGE_MAX_OUTPUT_TOKENS = 800
JUDGE_TIMEOUT_SECONDS = 120.0
#: The fixed instant every case runs at; messages are dated minutes before it.
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
#: The barrier the proposal policy reads (`policy_loader.ingestion_state`):
#: set by "this process run", so a card is staged as in production.
PROCESS_RUN_ID = "eval-owner-drafting"
_SYNTHETIC_OWNER_ID = 7_000_000_095
_CONTROL_CHAT_ID = 7_000_000_094

Variant = Literal["uz-Latn", "ru", "en"]
Category = Literal[
    "identity",
    "suggest",
    "advice",
    "dictate",
    "command_words",
    "compose",
    "reply_earlier",
    "substance",
    "append",
    "shorter",
    "word_bounds",
    "tone",
    "word_count",
    "pick_suggestion",
]
CATEGORIES: tuple[str, ...] = get_args(Category)
#: Tools whose call stages or changes a card.
CARD_TOOLS = ("propose_reply", "propose_message", "revise_draft")
_KNOWN_TOOLS = frozenset(
    {*CARD_TOOLS, "get_draft", "resolve_contact", "get_conversation", "list_conversations"}
)


# ------------------------------------------------------------------ the cases
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ChatMessage(_Strict):
    #: Unique within the case (`m1`), for `answered`.
    key: str = Field(pattern=r"^m\d+$")
    author: Literal["contact", "owner"]
    #: Minutes before the case's instant (`NOW`): inside the reply window.
    minutes_ago: int = Field(ge=1, le=1380)
    text: str = Field(min_length=1)


class Chat(_Strict):
    key: str = Field(pattern=r"^[a-z]$")
    #: A synthetic display name.
    contact: str = Field(min_length=1)
    messages: tuple[ChatMessage, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _oldest_first(self) -> Self:
        ages = [m.minutes_ago for m in self.messages]
        if ages != sorted(ages, reverse=True) or len(set(ages)) != len(ages):
            raise ValueError(f"chat {self.key}: messages must be oldest first, distinct times")
        return self


class HistoryTurn(_Strict):
    role: Literal["owner", "assistant"]
    text: str = Field(min_length=1)


class LiveDraft(_Strict):
    """A card awaiting approval before the owner's message."""

    chat: str
    origin: Literal["owner_dictated", "agent_proposal"]
    text: str = Field(min_length=1)


class CardExpectation(_Strict):
    #: The chat the staged or revised card must be on.
    chat: str
    origin: Literal["owner_dictated", "agent_proposal"] | None
    #: `new_message`: no base message; `reply`: a base message.
    mode: Literal["reply", "new_message"] | None
    #: The message key the card answers (its base message).
    answered: str | None
    #: The exact text (a dictated or picked text), compared normalised.
    exact_text: str | None
    #: Each must be in the card (as a value, or as a folded substring).
    includes: tuple[str, ...]
    #: None may be in the card (folded substring).
    excludes: tuple[str, ...]
    min_words: int | None = Field(ge=1)
    max_words: int | None = Field(ge=1)
    #: The live card itself was revised (the same action, a higher version).
    same_card: bool
    #: The live card's old text is the new text's prefix (append).
    keeps_prefix: bool
    #: Fewer words than the live card had.
    fewer_words: bool


#: One required value of the reply: a string, or (v1.1) a list of
#: alternatives of which one must appear — the same value in another script
#: (the name "Alviro" written in Cyrillic is just as right).
ReplyValue = str | Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]


def value_alternatives(value: ReplyValue) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else value


def value_label(value: ReplyValue) -> str:
    """The dataset's own expected value(s), for a problem code."""
    return "|".join(value_alternatives(value))


class Expectation(_Strict):
    tools_called: tuple[str, ...]
    tools_not_called: tuple[str, ...]
    #: None: no card may be staged and the live card (if any) stays unchanged.
    card: CardExpectation | None
    reply_includes: tuple[ReplyValue, ...]
    reply_excludes: tuple[str, ...]
    #: The reply states the live draft's exact word count.
    reply_states_word_count: bool
    #: What the judge checks (tone, carried substance), or None for no judge.
    judge: str | None


class OwnerDraftingCase(_Strict):
    id: str = Field(pattern=r"^od\d{2}$")
    variant: Variant
    category: Category
    chats: tuple[Chat, ...] = Field(min_length=1)
    history: tuple[HistoryTurn, ...]
    live_draft: LiveDraft | None
    turn: str = Field(min_length=1)
    expect: Expectation

    def chat(self, key: str) -> Chat:
        return next(c for c in self.chats if c.key == key)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        chats = {c.key for c in self.chats}
        if len(chats) != len(self.chats):
            raise ValueError("chat keys repeat")
        keys = [m.key for c in self.chats for m in c.messages]
        if len(set(keys)) != len(keys):
            raise ValueError("message keys repeat")
        names = {fold(c.contact) for c in self.chats}
        if len(names) != len(self.chats):
            raise ValueError("two chats with one contact name")
        for name in (*self.expect.tools_called, *self.expect.tools_not_called):
            if name not in _KNOWN_TOOLS:
                raise ValueError(f"unknown tool {name}")
        if set(self.expect.tools_called) & set(self.expect.tools_not_called):
            raise ValueError("a tool both required and forbidden")
        live = self.live_draft
        if live is not None and live.chat not in chats:
            raise ValueError("live_draft names an unknown chat")
        if (
            live is not None
            and live.origin == "agent_proposal"
            and not any(t.role == "owner" for t in self.history)
        ):
            # The product stages an assistant's card only for an owner turn.
            raise ValueError("an agent_proposal live draft needs an owner turn in history")
        if self.expect.reply_states_word_count and live is None:
            raise ValueError("a word count needs a live draft")
        card = self.expect.card
        if card is None:
            return self
        if card.chat not in chats:
            raise ValueError("the card names an unknown chat")
        if card.answered is not None:
            answered = next(
                (m for m in self.chat(card.chat).messages if m.key == card.answered), None
            )
            if answered is None or answered.author != "contact":
                raise ValueError("answered must be a contact message of the card's chat")
            if card.mode == "new_message":
                raise ValueError("a new message answers nothing")
        if (
            card.min_words is not None
            and card.max_words is not None
            and card.min_words > card.max_words
        ):
            raise ValueError("min_words above max_words")
        needs_live = card.same_card or card.keeps_prefix or card.fewer_words
        if needs_live and (live is None or live.chat != card.chat):
            raise ValueError("same_card, keeps_prefix and fewer_words need a live draft there")
        if card.exact_text is not None:
            if card.origin == "owner_dictated" and not _occurs(card.exact_text, self.turn):
                raise ValueError("a dictated exact_text must be in the owner's message")
            if card.origin == "agent_proposal" and not any(
                _occurs(card.exact_text, t.text) for t in self.history if t.role == "assistant"
            ):
                raise ValueError("a picked exact_text must be in an assistant turn")
        return self


def _occurs(needle: str, haystack: str) -> bool:
    return normalise_for_match(needle).strip(_EDGE) in normalise_for_match(haystack)


_EDGE = " '\".,!?;:…"


def load_cases(path: Path = DATASET_PATH) -> list[OwnerDraftingCase]:
    """The cases, validated; ids consecutive from `od01`. A malformed line is a
    `ValueError` naming its line, never its text."""
    cases: list[OwnerDraftingCase] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = OwnerDraftingCase.model_validate_json(line)
        except ValueError:
            raise ValueError(f"{path.name} line {number} is not a valid case") from None
        expected = f"od{len(cases) + 1:02d}"
        if case.id != expected:
            raise ValueError(f"{path.name} line {number}: id should be {expected}")
        cases.append(case)
    return cases


# ------------------------------------------------------------------ observing
@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    name: str
    #: The validated arguments (synthetic data), as the model passed them.
    args: Mapping[str, Any]
    status: str

    def as_json(self) -> dict[str, Any]:
        return {"tool": self.name, "args": dict(self.args), "status": self.status}


@dataclass(frozen=True, slots=True)
class CardRecord:
    action_id: int
    chat: str
    origin: str
    state: str
    version: int
    text: str
    #: The base message's case key, or None (a new message).
    answered: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "chat": self.chat,
            "origin": self.origin,
            "state": self.state,
            "version": self.version,
            "answered": self.answered,
            "words": word_count(self.text),
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class Observed:
    """What one case's run left (the checks' input)."""

    tools: tuple[ToolCallRecord, ...]
    reply: str
    #: Cards staged by this run (the seeded live card excluded).
    new_cards: tuple[CardRecord, ...]
    #: The seeded live card after the run, or None when the case has none.
    live_after: CardRecord | None
    #: The seeded live card's id, version and text before the run.
    live_before: CardRecord | None = None


# ------------------------------------------------------------------ the checks
def _includes(text: str, value: str) -> bool:
    return fold(value) in fold(text) or contains_words(value_key(text), value_key(value))


def _card_problems(card: CardRecord, want: CardExpectation, live: CardRecord | None) -> list[str]:
    problems: list[str] = []
    if want.origin is not None and card.origin != want.origin:
        problems.append(f"card:origin:{card.origin}")
    if want.mode == "new_message" and card.answered is not None:
        problems.append("card:answers_a_message")
    if want.mode == "reply" and card.answered is None:
        problems.append("card:answers_nothing")
    if want.answered is not None and card.answered != want.answered:
        problems.append("card:wrong_answered_message")
    if want.exact_text is not None and normalise_for_match(card.text).strip(
        _EDGE
    ) != normalise_for_match(want.exact_text).strip(_EDGE):
        problems.append("card:not_exact_text")
    problems += [f"card:missing:{v}" for v in want.includes if not _includes(card.text, v)]
    problems += [f"card:contains:{v}" for v in want.excludes if fold(v) in fold(card.text)]
    words = word_count(card.text)
    if want.min_words is not None and words < want.min_words:
        problems.append("card:too_few_words")
    if want.max_words is not None and words > want.max_words:
        problems.append("card:too_many_words")
    if live is not None:
        if want.keeps_prefix and not normalise_for_match(card.text).startswith(
            normalise_for_match(live.text).rstrip(".!?…")
        ):
            problems.append("card:prefix_lost")
        if want.fewer_words and words >= word_count(live.text):
            problems.append("card:not_shorter")
    return problems


def check_case(case: OwnerDraftingCase, seen: Observed) -> tuple[str, ...]:
    """Every deterministic problem with one case's result (codes and the
    dataset's own expected strings; empty means it passed). Pure."""
    want = case.expect
    called = {t.name for t in seen.tools}
    problems = [f"routing:missing_tool:{n}" for n in want.tools_called if n not in called]
    problems += [f"routing:forbidden_tool:{n}" for n in want.tools_not_called if n in called]
    before, after = seen.live_before, seen.live_after
    revised = (
        before is not None
        and after is not None
        and (after.version != before.version or after.text != before.text)
    )
    card_want = want.card
    if card_want is None:
        if seen.new_cards:
            problems.append("routing:unexpected_card")
        if revised:
            problems.append("routing:unexpected_revision")
    elif card_want.same_card:
        if seen.new_cards:
            problems.append("routing:new_card_instead_of_revision")
        if not revised or after is None or after.version <= (before.version if before else 1):
            problems.append("routing:live_card_not_revised")
        else:
            problems += _card_problems(after, card_want, before)
    else:
        mine = [c for c in seen.new_cards if c.chat == card_want.chat]
        if any(c.chat != card_want.chat for c in seen.new_cards):
            problems.append("routing:card_on_another_chat")
        if not mine:
            problems.append("routing:no_card")
        else:
            problems += _card_problems(mine[-1], card_want, before)
    reply = fold(seen.reply)
    problems += [
        f"reply:missing:{value_label(v)}"
        for v in want.reply_includes
        if not any(fold(alt) in reply for alt in value_alternatives(v))
    ]
    problems += [f"reply:contains:{v}" for v in want.reply_excludes if fold(v) in reply]
    if want.reply_states_word_count and before is not None:
        count = word_count(before.text)
        if not re.search(rf"(?<!\d){count}(?!\d)", seen.reply):
            problems.append("reply:word_count_not_stated")
    return tuple(dict.fromkeys(problems))


def judged_text(case: OwnerDraftingCase, seen: Observed) -> str | None:
    """The card text the judge reads, or None when there is none to judge."""
    want = case.expect.card
    if want is None:
        return None
    if want.same_card:
        revised = seen.live_after
        before = seen.live_before
        if revised is None or before is None or revised.version <= before.version:
            return None
        return revised.text
    mine = [c for c in seen.new_cards if c.chat == want.chat]
    return mine[-1].text if mine else None


# ------------------------------------------------------------------ the judge
class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meets: bool
    reason: str


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    screened: bool
    meets: bool | None = None
    reason: str | None = None
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "screened": self.screened,
            "meets": self.meets,
            "reason": self.reason,
            "error": self.error,
        }


def render_case_chat(case: OwnerDraftingCase, chat_key: str) -> str:
    chat = case.chat(chat_key)
    lines = [f"Chat with {chat.contact} (oldest first):"]
    lines += [
        f"[{m.key}] {m.minutes_ago} min ago, "
        f"{'the owner' if m.author == 'owner' else chat.contact}: {m.text}"
        for m in chat.messages
    ]
    return "\n".join(lines)


def judge_input(case: OwnerDraftingCase, text: str) -> str:
    want = case.expect.card
    assert want is not None
    assert case.expect.judge is not None
    parts = [render_case_chat(case, want.chat), ""]
    if case.live_draft is not None and want.same_card:
        parts += ["The draft before the owner's request:", case.live_draft.text, ""]
    return "\n".join(
        [
            *parts,
            "The owner's request to the assistant:",
            case.turn,
            "",
            "Criterion:",
            case.expect.judge,
            "",
            "The draft the assistant staged (written as the owner):",
            text,
        ]
    )


class CaseJudge:
    """Pre-screens tone and carried substance through the `LLMClient` port."""

    def __init__(self, llm: LLMClient, *, model: str, effort: str | None) -> None:
        self._llm = llm
        self._model = model
        self._effort = effort
        self._rubric = load_asset("rubrics", RUBRIC_ID)

    async def screen(self, case: OwnerDraftingCase, text: str, account_id: int) -> JudgeOutcome:
        request = LLMRequest(
            account_id=account_id,
            purpose=JUDGE_PURPOSE,
            model=self._model,
            instructions=self._rubric.text,
            items=(TextItem("user", judge_input(case, text), Provenance.MANAGED),),
            max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS,
            timeout_seconds=JUDGE_TIMEOUT_SECONDS,
            effort=self._effort,
        )
        try:
            response = await self._llm.parse(request, JudgeVerdict)
        except LLMError as error:
            return JudgeOutcome(screened=False, error=error.kind.value)
        if response.stop_reason is not StopReason.COMPLETED or response.parsed is None:
            return JudgeOutcome(screened=False, error=response.stop_reason.value)
        return JudgeOutcome(True, response.parsed.meets, response.parsed.reason)


# ------------------------------------------------------------------ the record
@dataclass(frozen=True, slots=True)
class CaseRecord:
    case_id: str
    category: str
    variant: str
    problems: tuple[str, ...]
    run_status: str
    error: str | None
    seen: Observed
    judge: JudgeOutcome | None = None
    outage: bool = False
    attempts: int = 1
    not_evaluated: bool = False

    @property
    def passed(self) -> bool:
        return self.error is None and not self.problems

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "category": self.category,
            "variant": self.variant,
            "passed": self.passed,
            "not_evaluated": self.not_evaluated,
            "problems": list(self.problems),
            "run_status": self.run_status,
            "error": self.error,
            "attempts": self.attempts,
            "tools": [t.as_json() for t in self.seen.tools],
            "new_cards": [c.as_json() for c in self.seen.new_cards],
            "live_before": None
            if self.seen.live_before is None
            else self.seen.live_before.as_json(),
            "live_after": None if self.seen.live_after is None else self.seen.live_after.as_json(),
            "reply": self.seen.reply,
            "judge": None if self.judge is None else self.judge.as_json(),
        }


# ------------------------------------------------------------------ eval-side fakes
class _Capture:
    """The control channel: keeps the runtime's replies."""

    def __init__(self) -> None:
        self.sends: list[str] = []

    async def send(
        self, *, account_id: int, chat_id: int, text: str, buttons: Sequence[Button] = ()
    ) -> Delivery:
        self.sends.append(text)
        return Delivery(ok=True, message_id=len(self.sends))

    async def typing(self, *, account_id: int, chat_id: int) -> None:
        return None


class _RecordingSender:
    """The Bot API as the approval cards see it: records, never calls Telegram.
    Business (owner-voice) methods raise: nothing may send as the owner here."""

    def __init__(self) -> None:
        self.calls = 0
        self._next = 5000

    async def send_message(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        self._next += 1
        return {"message_id": self._next}

    async def edit_message_text(self, *, message_id: int, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return {"message_id": message_id}

    async def edit_message_reply_markup(self, *, message_id: int, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return {"message_id": message_id}

    async def send_chat_action(self, **_: Any) -> None:
        return None

    async def answer_callback_query(self, **_: Any) -> None:
        return None

    async def send_plain_message(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("the evaluation never sends as the owner")

    async def edit_plain_message_text(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("the evaluation never sends as the owner")

    async def delete_business_messages(self, **_: Any) -> bool:
        raise AssertionError("the evaluation never sends as the owner")


class _NoJobs:
    """`JobScheduler`: a revision handed over to a job stays undone here."""

    def __init__(self) -> None:
        self.deferred: list[str] = []

    async def defer(self, task: str, **_: Any) -> int | None:
        self.deferred.append(task)
        return None


class _FixedClock:
    def now(self) -> datetime:
        return NOW


async def _no_sleep(_: float) -> None:
    return None


# ------------------------------------------------------------------ loading
@dataclass(frozen=True, slots=True)
class _Loaded:
    account_id: int
    #: Chat key -> conversation id; message key -> message id (and back).
    conversations: Mapping[str, int]
    messages: Mapping[str, int]
    live_action_id: int | None


def _actions_config(model: str) -> ActionsConfig:
    return ActionsConfig(
        approval_max_age=timedelta(hours=12),
        draft_debounce=timedelta(seconds=20),
        mark_uncertain_after=timedelta(seconds=300),
        connection_stale_alert_after=timedelta(minutes=30),
        sending_enabled=False,
        process_run_id=PROCESS_RUN_ID,
        assist_draft_trigger="off",
        draft_route_model=model,
    )


# ------------------------------------------------------------------ estimate
_TOOL_RESULT_CHARS = 20_000
#: Draft calls per case at worst: two attempts (a word range retried once),
#: each with its one truncation retry.
DRAFT_CALLS_PER_CASE = 4
_DRAFT_OVERHEAD_CHARS = 8_000


def _planned_calls(case: OwnerDraftingCase, *, judged: bool) -> list[PlannedCall]:
    agent = AgentConfig(model="")
    draft = DraftConfig(model="")
    summary = SummarizeConfig(model="")
    history = sum(len(t.text) for t in case.history) + len(case.turn)
    last_input = agent.run_token_budget + _TOOL_RESULT_CHARS + history
    retry = min(agent.max_output_tokens * 2, agent.max_output_tokens_ceiling)
    chats = sum(len(m.text) for c in case.chats for m in c.messages)
    calls = [
        PlannedCall(ROUTE, agent.run_token_budget, 0),
        PlannedCall(ROUTE, last_input, agent.max_output_tokens),
        PlannedCall(ROUTE, last_input, retry),
    ]
    calls += [
        PlannedCall(
            "draft_reply", chats + history + _DRAFT_OVERHEAD_CHARS, draft.max_output_tokens_ceiling
        )
    ] * DRAFT_CALLS_PER_CASE
    calls += [PlannedCall("summarize", 40_000, summary.max_output_tokens)] * SUMMARY_CALLS_PER_QUERY
    if judged:
        rubric = len(load_asset("rubrics", RUBRIC_ID).text)
        calls.append(PlannedCall(JUDGE_PURPOSE, rubric + chats + 6_000, JUDGE_MAX_OUTPUT_TOKENS))
    return calls


# ------------------------------------------------------------------ the target
class OwnerDraftingTarget:
    name = "owner_drafting"
    dataset: DatasetName = "owner_drafting"
    route = ROUTE
    uses_model = True

    def __init__(
        self,
        cases: Sequence[OwnerDraftingCase],
        *,
        draft_model: str | None = None,
        draft_effort: str | None = "low",
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cases = list(cases)
        self.draft_model = draft_model
        self.draft_effort = draft_effort
        self._factory = session_factory
        self._sleep = sleep

    def label(self, effort: str | None) -> str:
        """The report's mode: both efforts, so low and medium runs never share a name
        or calibrate each other."""
        owner = effort or "none"
        draft = self.draft_effort or "none"
        return f"effort-{owner}" if owner == draft else f"effort-{owner}-draft-{draft}"

    def bind(self, session_factory: async_sessionmaker[AsyncSession]) -> OwnerDraftingTarget:
        return OwnerDraftingTarget(
            self._cases,
            draft_model=self.draft_model,
            draft_effort=self.draft_effort,
            session_factory=session_factory,
            sleep=self._sleep,
        )

    @property
    def _database(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            raise RuntimeError("bind() a database before running the owner_drafting target")
        return self._factory

    def selected(self, context: RunContext) -> list[OwnerDraftingCase]:
        if context.only is not None:
            return self.named(context.only, limit=context.limit)
        if context.limit is None:
            return self._cases
        if context.limit < 1:
            raise ValueError("limit must be at least 1")
        return self._cases[: context.limit]

    def named(self, ids: Sequence[str], *, limit: int | None = None) -> list[OwnerDraftingCase]:
        """The cases `--only` names, in set order: a trial (at least one, fewer
        than all, never with a limit)."""
        if limit is not None:
            raise ValueError("only and limit cannot be combined")
        wanted = set(ids)
        unknown = sorted(wanted - {c.id for c in self._cases})
        if unknown:
            raise ValueError(f"only names unknown cases: {', '.join(unknown)}")
        if not wanted or len(wanted) >= len(self._cases):
            raise ValueError("only must name at least one case and not the whole set")
        return [c for c in self._cases if c.id in wanted]

    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        return [
            call
            for case in self.selected(context)
            for call in _planned_calls(case, judged=case.expect.judge is not None)
        ]

    # ------------------------------------------------------------------ run
    async def run(self, context: RunContext) -> TargetResult:
        if context.llm is None or context.model is None:
            raise ValueError("the owner_drafting target needs a model client and a model id")
        cases = self.selected(context)
        records: list[CaseRecord] = []
        stopped: str | None = None
        for case in cases:
            try:
                records.append(await self._attempts(case, context))
            except BudgetExceededError as stop:
                stopped = str(stop)
                break
        return self._result(
            records, planned=len(cases), ledger=context.reviews, stopped=stopped, context=context
        )

    async def _attempts(self, case: OwnerDraftingCase, context: RunContext) -> CaseRecord:
        """A case again after a provider outage, each attempt under a fresh
        account; never scored when every attempt failed on the provider."""
        record: CaseRecord | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            record = await self._evaluate(case, context)
            if not record.outage:
                return replace(record, attempts=attempt)
            if attempt < MAX_ATTEMPTS:
                await self._sleep(RETRY_DELAYS[attempt - 1])
        assert record is not None
        return replace(record, attempts=MAX_ATTEMPTS, not_evaluated=True)

    async def _evaluate(self, case: OwnerDraftingCase, context: RunContext) -> CaseRecord:
        assert context.llm is not None
        assert context.model is not None
        factory = self._database
        clock = _FixedClock()
        draft_model = self.draft_model or context.model
        config = _actions_config(draft_model)
        gateway = OutboundGateway(_RecordingSender(), clock, sleep=_no_sleep)
        cards = ApprovalCards(session_factory=factory, gateway=gateway, clock=clock, config=config)
        drafter = Drafter(
            session_factory=factory,
            llm=context.llm,
            clock=clock,
            config=DraftConfig(model=draft_model, effort=self.draft_effort),
        )
        loaded = await self._load(case, context.model, config=config, cards=cards)
        before = await self._card(loaded, loaded.live_action_id)
        calls: list[ToolCallRecord] = []
        channel = _Capture()
        runtime = OwnerRequestRuntime(
            session_factory=factory,
            llm=context.llm,
            channel=channel,
            registry=_recording_registry(calls),
            clock=clock,
            config=AgentConfig(
                model=context.model, effort=context.effort, enabled_flags=PRODUCTION_FLAGS
            ),
            summarizer=Summarizer(
                session_factory=factory,
                llm=context.llm,
                clock=clock,
                config=SummarizeConfig(model=context.model, effort=context.effort),
            ),
            drafter=drafter,
            approval_cards=cards,
            actions_config=config,
            card_reviser=RevisionJob(
                session_factory=factory,
                cards=cards,
                drafter=drafter,
                clock=clock,
                scheduler=_NoJobs(),
            ),
        )
        turn_id = await self._turn(loaded.account_id, case.turn, at=NOW)
        outcome = await runtime.run(turn_id)
        error_class = await self._error_class(outcome.run_id)
        new_cards = await self._new_cards(loaded)
        after = await self._card(loaded, loaded.live_action_id)
        seen = Observed(
            tools=tuple(calls),
            reply="\n".join(channel.sends),
            new_cards=new_cards,
            live_after=after,
            live_before=before,
        )
        succeeded = outcome.status is RunStatus.SUCCEEDED
        error = (
            None
            if succeeded
            else f"run:{outcome.status.value}" + (f":{error_class}" if error_class else "")
        )
        judge: JudgeOutcome | None = None
        text = judged_text(case, seen)
        if case.expect.judge is not None and succeeded:
            judge = (
                JudgeOutcome(screened=False, error="no_card")
                if text is None
                else await CaseJudge(
                    context.llm, model=context.model, effort=context.effort
                ).screen(case, text, loaded.account_id)
            )
        return CaseRecord(
            case_id=case.id,
            category=case.category,
            variant=case.variant,
            problems=check_case(case, seen),
            run_status=outcome.status.value,
            error=error,
            seen=seen,
            judge=judge,
            outage=not succeeded and error_class in OUTAGE_ERRORS,
        )

    # ------------------------------------------------------------------ data
    async def _load(
        self, case: OwnerDraftingCase, model: str, *, config: ActionsConfig, cards: ApprovalCards
    ) -> _Loaded:
        """The case's own synthetic account: chats, consents, a connection, the
        ingestion barrier, the history and the live card."""
        processor = processor_for_model(model)
        if processor is None:
            raise RefusedError(f"no processor is known for model '{model}'")
        factory = self._database
        conversations: dict[str, int] = {}
        messages: dict[str, int] = {}
        async with factory() as session, session.begin():
            account = Account(
                telegram_user_id=_SYNTHETIC_OWNER_ID + secrets.randbelow(900_000_000),
                status="active",
                timezone=OWNER_TIMEZONE,
                control_chat_id=_CONTROL_CHAT_ID,
                locale_pref=case.variant,
            )
            session.add(account)
            await session.flush()
            for kind in (CONSENT_AI_CONTROL, CONSENT_AI_MANAGED):
                await grant_consent(
                    session,
                    account,
                    kind=kind,
                    policy_version=EVAL_POLICY_VERSION,
                    processors=[processor],
                    clock=_FixedClock(),
                )
            session.add(
                BusinessConnection(
                    account_id=account.id,
                    connection_id=f"eval-owner-drafting-{account.id}",
                    is_enabled=True,
                    rights=["can_reply", "can_read_messages"],
                    capability_tier="can_reply",
                    user_chat_id=_CONTROL_CHAT_ID,
                    status="active",
                    connected_at=NOW - timedelta(days=2),
                )
            )
            for number, chat in enumerate(case.chats, start=1):
                contact = Contact(
                    account_id=account.id,
                    telegram_user_id=2_000_000 + number,
                    display_name=chat.contact,
                )
                session.add(contact)
                await session.flush()
                row = Conversation(
                    account_id=account.id,
                    business_chat_id=2_000_000 + number,
                    contact_id=contact.id,
                    coverage_started_at=NOW - timedelta(days=2),
                )
                session.add(row)
                await session.flush()
                conversations[chat.key] = row.id
                for index, message in enumerate(chat.messages, start=1):
                    stored = Message(
                        account_id=account.id,
                        conversation_id=row.id,
                        telegram_message_id=index,
                        direction="contact" if message.author == "contact" else "owner_manual",
                        sent_at=NOW - timedelta(minutes=message.minutes_ago),
                        content_type="text",
                        text=message.text,
                        search_key=normalize(message.text),
                        script=detect_script(message.text),
                        lang_guess=guess_lang(message.text),
                    )
                    session.add(stored)
                    await session.flush()
                    messages[message.key] = stored.id
            await _barrier(session)
            account_id = account.id
        count = len(case.history)
        trigger: int | None = None
        for index, turn in enumerate(case.history):
            turn_id = await self._turn(
                account_id, turn.text, at=NOW - timedelta(minutes=count - index), role=turn.role
            )
            if turn.role == "owner":
                trigger = turn_id
        live_id: int | None = None
        live = case.live_draft
        if live is not None:
            async with transaction(factory) as uow:
                dictated = live.origin == "owner_dictated"
                proposal = await propose_draft(
                    uow.session,
                    account_id=account_id,
                    conversation_id=conversations[live.chat],
                    outcome=DraftOutcome(DraftStatus.DRAFTED),
                    origin=ActionOrigin.OWNER_DICTATED if dictated else ActionOrigin.AGENT_PROPOSAL,
                    run_id=None,
                    tool_call_id=None,
                    trigger_turn_id=trigger,
                    clock=_FixedClock(),
                    config=config,
                    presenter=cards,
                    dictated_text=live.text if dictated else None,
                    suggested_text=None if dictated else live.text,
                )
                assert not isinstance(proposal, Stale)  # a given text is never stale
                live_id = proposal.action_id
        return _Loaded(account_id, conversations, messages, live_id)

    async def _turn(self, account_id: int, text: str, *, at: datetime, role: str = "owner") -> int:
        async with self._database() as session, session.begin():
            turn = await record_control_turn(
                session, account_id=account_id, role=role, text=text, created_at=at
            )
            return turn.id

    async def _error_class(self, run_id: int | None) -> str | None:
        if run_id is None:
            return None
        async with self._database() as session:
            run = await session.get(AgentRun, run_id)
            return None if run is None else run.error_class

    def _record(self, loaded: _Loaded, row: Action) -> CardRecord:
        chats = {v: k for k, v in loaded.conversations.items()}
        keys = {v: k for k, v in loaded.messages.items()}
        return CardRecord(
            action_id=row.id,
            chat=chats.get(row.conversation_id, "?"),
            origin=row.origin,
            state=row.state,
            version=row.version,
            text=row.text or "",
            answered=None if row.base_message_id is None else keys.get(row.base_message_id, "?"),
        )

    async def _card(self, loaded: _Loaded, action_id: int | None) -> CardRecord | None:
        if action_id is None:
            return None
        async with self._database() as session:
            row = await session.get(Action, action_id, populate_existing=True)
            return None if row is None else self._record(loaded, row)

    async def _new_cards(self, loaded: _Loaded) -> tuple[CardRecord, ...]:
        async with self._database() as session:
            rows = (
                await session.scalars(
                    select(Action).where(Action.account_id == loaded.account_id).order_by(Action.id)
                )
            ).all()
        return tuple(self._record(loaded, row) for row in rows if row.id != loaded.live_action_id)

    # ------------------------------------------------------------------ metrics
    def _result(
        self,
        records: Sequence[CaseRecord],
        *,
        planned: int,
        ledger: ReviewLedger,
        stopped: str | None,
        context: RunContext,
    ) -> TargetResult:
        total = len(self._cases)
        measured = [r for r in records if not r.not_evaluated]
        skipped = tuple(r.case_id for r in records if r.not_evaluated)
        cases = {c.id: c for c in self._cases}

        def routing_ok(record: CaseRecord) -> bool:
            return record.error is None and not any(
                p.startswith("routing:") for p in record.problems
            )

        with_card = [r for r in measured if cases[r.case_id].expect.card is not None]
        with_reply = [
            r
            for r in measured
            if (want := cases[r.case_id].expect).reply_includes
            or want.reply_excludes
            or want.reply_states_word_count
        ]
        judged = [r for r in measured if r.judge is not None and r.judge.screened]
        metrics: list[MetricResult] = [
            ratio_metric(
                key="case_passed",
                label="Every deterministic check of the case passed",
                outcomes={r.case_id: r.passed for r in measured},
                threshold=None,
                ledger=ledger,
                set_size=total,
            ),
            ratio_metric(
                key="routing_correct",
                label="Routing: the right tools, and a card staged or revised only when asked",
                outcomes={r.case_id: routing_ok(r) for r in measured},
                threshold=None,
                ledger=ledger,
                set_size=total,
            ),
            ratio_metric(
                key="card_correct",
                label="Card: chat, origin, mode, answered message, text and length checks",
                outcomes={
                    r.case_id: r.error is None
                    and not any(p.startswith(("routing:", "card:")) for p in r.problems)
                    for r in with_card
                },
                threshold=None,
                ledger=ledger,
            ),
            ratio_metric(
                key="reply_correct",
                label="Reply: identity and the exact word count",
                outcomes={
                    r.case_id: r.error is None
                    and not any(p.startswith("reply:") for p in r.problems)
                    for r in with_reply
                },
                threshold=None,
                ledger=ledger,
            ),
            ratio_metric(
                key="judge_meets",
                label="Judge pre-screen: tone or substance as asked",
                outcomes={r.case_id: bool(r.judge and r.judge.meets) for r in judged},
                threshold=None,
                ledger=ledger,
                note="A pre-screen only; the owner reads the JSON report's drafts.",
            ),
        ]
        prompts = (
            prompt_registry.active(ROUTE).label,
            prompt_registry.active("draft_owner").label,
            RUBRIC_ID,
        )
        notes = [
            "Owner-requested drafting and routing through the real owner_request runtime, "
            f"the product's drafter and card machinery; owner_request effort "
            f"{context.effort or 'none'}, draft_owner effort {self.draft_effort or 'none'}. "
            "Informational: no threshold, no gate."
        ]
        failed = sorted(r.case_id for r in measured if not r.passed)
        if failed:
            notes.append(f"{len(failed)} case(s) failed a check: " + ", ".join(failed))
        unscreened = sorted(
            r.case_id for r in measured if r.judge is not None and not r.judge.screened
        )
        if unscreened:
            notes.append("judge could not screen: " + ", ".join(unscreened))
        if skipped:
            notes.append(
                f"INCOMPLETE: {len(skipped)} case(s) not evaluated (provider unavailable "
                f"through {MAX_ATTEMPTS} attempts): " + ", ".join(skipped)
            )
        if stopped:
            notes.append(f"STOPPED EARLY: {stopped}; {len(records)} of {planned} cases ran")
        return TargetResult(
            target=self.name,
            dataset=self.dataset,
            items=len(measured),
            items_planned=planned,
            items_total=total,
            metrics=tuple(metrics),
            details=tuple(r.as_json() for r in records),
            breakdowns={
                "case passed by category": _by(measured, lambda r: r.category),
                "case passed by variant": _by(measured, lambda r: r.variant),
            },
            notes=tuple(notes),
            prompts=prompts,
            not_evaluated=skipped,
        )


def _by(records: Sequence[CaseRecord], key: Callable[[CaseRecord], str]) -> Mapping[str, Measure]:
    grouped: dict[str, list[bool]] = {}
    for record in records:
        grouped.setdefault(key(record), []).append(record.passed)
    return {k: Measure(sum(v), len(v)) for k, v in sorted(grouped.items())}


def _recording_registry(calls: list[ToolCallRecord]) -> ToolRegistry:
    """The product's registry, each call's name, arguments and status recorded
    (instrumentation only: the handler and its result are unchanged)."""

    def recording(tool: ToolDefinition) -> ToolDefinition:
        real = tool.handler

        async def handler(ctx: ToolContext, args: Any) -> ToolOutcome:
            outcome = await real(ctx, args)
            dumped = args.model_dump(mode="json") if isinstance(args, BaseModel) else {}
            calls.append(ToolCallRecord(tool.name, json.loads(json.dumps(dumped)), outcome.status))
            return outcome

        return replace(tool, handler=handler)

    return ToolRegistry(recording(tool) for tool in default_registry().all())


async def _barrier(session: AsyncSession) -> None:
    """The ingestion barrier for `PROCESS_RUN_ID` (the policy fails closed without it)."""
    value = {"process_run_id": PROCESS_RUN_ID, "caught_up_at": NOW.isoformat()}
    existing = await session.get(SystemState, "ingestion_barrier")
    if existing is None:
        session.add(SystemState(key="ingestion_barrier", value=value))
    else:
        existing.value = value


@asynccontextmanager
async def isolated_owner_drafting(
    base_url: str, target: OwnerDraftingTarget
) -> AsyncIterator[OwnerDraftingTarget]:
    """The target over its own throwaway database (dropped on exit)."""
    async with scratch_database(base_url) as url:
        engine = build_engine(url)
        try:
            yield target.bind(build_session_factory(engine))
        finally:
            await engine.dispose()


__all__ = [
    "CATEGORIES",
    "DATASET_PATH",
    "JUDGE_PURPOSE",
    "NOW",
    "RUBRIC_ID",
    "SET_NAME",
    "CardRecord",
    "Observed",
    "OwnerDraftingCase",
    "OwnerDraftingTarget",
    "ToolCallRecord",
    "check_case",
    "isolated_owner_drafting",
    "load_cases",
]
