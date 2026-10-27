"""The drafts target (P2 spec E2 §B): the product's `Drafter` over the E2 situations.

`python -m evals run drafts` measures reply drafting **exactly as the product
drafts** (S04.2): the real `Drafter`, its context builder, the active
`draft_reply` prompt from the prompt registry, the structured `DraftResult`
schema and its validation, the route's model. Per run, in a **throwaway
database**:

1. one synthetic account, with an `ai_control` and an `ai_managed` consent naming
   the processor of the run's model (the drafter's own consent check needs
   them) and the owner language of `--owner-language` as `locale_pref`;
2. **every** situation's chat in that same account, each with its own contact:
   an E1-based situation's chat up to and including `cut_after`, a new
   situation's whole chat, stored the way ingestion stores messages
   (`corpus.row_values`). One account for all is what makes the canary check
   meaningful: the drafter could reach another chat only through a leak;
3. per situation, a reset, then its `owner_prefs` as the account's reply
   settings, its `style_notes` as owner instructions that apply to replies, and
   its `contact_notes` as notes on its contact — so **no situation sees another's
   preferences or notes**;
4. `Drafter.draft(reason="eval")`: no tools, no proposal, no card. The clock is
   fixed a few minutes after the situation's last message.

The checks are deterministic and pure (`check_draft`): `needs_reply` agreement,
length 1-4096 when a reply was needed, **canary leaks** (another situation's
canary, or a `key_facts` value of another E1 chat that the account holds, in the
draft), numbers/times/amounts the draft states that the shown context does not
(`unsupported_values`), a commitment the context does not support,
`sensitive_topic` agreement, and the language and script of the reply against
`owner_prefs.reply_language` (informational, P2-O11). A judge (rubric
`draft_intent.v1`) pre-screens intent, `must_include` coverage and violations of
the kinds a string cannot catch. The owner rates drafts on the 4-level scale in
the review ledger (`ReviewLedger.ratings`, keyed by the draft they rated);
acceptability counts rated drafts only.

**Status: informational in P2.** No metric has a threshold or a gate (E2 is the
baseline for the M06 autonomy gate, S06.5).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, NamedTuple, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.accounts.preferences import (
    FIELD_REPLY_LANGUAGE,
    FIELD_REPLY_STYLE,
    FIELD_REPLY_TONE,
)
from assistant.accounts.service import CONSENT_AI_CONTROL, CONSENT_AI_MANAGED, grant_consent
from assistant.agent import prompt_registry
from assistant.agent.draft_context import build_draft_reply
from assistant.agent.draft_schemas import DraftResult
from assistant.agent.drafting import RUN_TYPE, DraftConfig, Drafter, DraftStatus
from assistant.conversations.models import Contact
from assistant.conversations.models import Conversation as ConversationRow
from assistant.conversations.models import Message as MessageRow
from assistant.core.model_routes import processor_for_model
from assistant.db.engine import build_engine, build_session_factory
from assistant.llm.port import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    ParsedResponse,
    Provenance,
    StopReason,
    TextItem,
)
from assistant.memory.items import MemoryView, provenance
from assistant.memory.models import SCOPE_CONTACT, SCOPE_OWNER, MemoryItem
from assistant.memory.summaries import SourceMessage
from evals.agent_mode import OUTAGE_ERRORS, value_key
from evals.assets import load_asset
from evals.budget import BudgetExceededError
from evals.corpus import OWNER_TIMEZONE, row_values, scratch_database, unload_corpus
from evals.metrics import OwnerLanguage, check_language, detect_language, ratio_metric
from evals.review import ACCEPTABLE_RATINGS, RATINGS, ReviewLedger, rating_key
from evals.schema import (
    Conversation,
    E2Situation,
    Message,
    Participant,
    Variant,
    fold,
    script_of,
)
from evals.targets import DatasetName, Measure, MetricResult, PlannedCall, RunContext, TargetResult

#: The run type whose model this target measures (`LLM_ROUTE_DRAFT_REPLY_MODEL`).
ROUTE = "draft_reply"
#: Model-call purposes, as the requests carry them (the plan uses the same names).
DRAFT_PURPOSE = RUN_TYPE
JUDGE_PURPOSE = "eval_draft_intent_screen"
RUBRIC_ID = "draft_intent.v1"
#: The reason the product's `Drafter` accepts for an evaluation run.
EVAL_REASON = "eval"
#: Reports are `docs/evals/<date>-e2-drafts-<model>.md` (spec E2 §B.3).
SET_NAME = "e2-drafts"
#: A consent version label that cannot be mistaken for a real policy version.
EVAL_POLICY_VERSION = "eval-synthetic"
#: "Now" for a draft: this long after the situation's last message.
DRAFT_DELAY = timedelta(minutes=5)
#: A `key_facts` value shorter than this (folded) is too common to be a canary.
MIN_DISTINCTIVE_CHARS = 5
#: The product's limit on a draft's length (`draft_schemas.validate_draft`).
MAX_DRAFT_CHARS = 4096
JUDGE_MAX_OUTPUT_TOKENS = 1000
JUDGE_TIMEOUT_SECONDS = 120.0
#: Messages of the chat the rating sheet quotes before each draft.
EXCERPT_MESSAGES = 6
#: Message ids in the real database have more digits than the plan's: slack for
#: the pre-run estimate only.
_ID_SLACK_CHARS = 500
_SYNTHETIC_OWNER_ID = 7_000_000_096
_OWNER_LANGUAGES: tuple[OwnerLanguage, ...] = get_args(OwnerLanguage)
_LEADING_VALUE = re.compile(r"^h?\d+")

ViolationKind = Literal["commitment", "other_conversation", "personal_data", "claim_human"]
DraftedIntent = Literal[
    "answer_question",
    "confirm",
    "decline",
    "ask_clarification",
    "acknowledge",
    "defer_to_owner",
]


class _FixedClock:
    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


# ------------------------------------------------------------- the situation
def participants(situation: E2Situation, chats: Mapping[str, Conversation]) -> tuple[str, str]:
    """The owner's and the contact's display names in the situation's chat."""
    if situation.conversation is not None:
        owner: Participant = situation.conversation.owner
        contact: Participant = situation.conversation.contact
    else:
        assert situation.source is not None
        chat = chats[situation.source.e1_conversation]
        owner, contact = chat.owner, chat.contact
    return owner.display_name, contact.display_name


def shown_window(
    situation: E2Situation, chats: Mapping[str, Conversation], window: int
) -> list[Message]:
    """The messages the drafter reads: the retained ones of the shown chat (a
    deleted message is a tombstone), the latest `window` of them, as
    `Drafter._messages` selects them."""
    retained = [m for m in situation.shown_messages(chats) if not m.deleted and (m.text or m.media)]
    return retained[-max(1, window) :]


def draft_instant(situation: E2Situation, chats: Mapping[str, Conversation]) -> datetime:
    return situation.shown_messages(chats)[-1].sent_at + DRAFT_DELAY


def _local(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def context_text(
    situation: E2Situation,
    shown: Sequence[Message],
    *,
    contact_name: str,
    now: datetime,
    timezone: str = OWNER_TIMEZONE,
) -> str:
    """Everything the drafter is shown that a draft may repeat: the messages
    with their local times, the owner's notes, the contact's name and the
    current time. The value check and the leak check read this."""
    zone = ZoneInfo(timezone)
    lines = [f"{_local(m.sent_at, zone)} {m.text or ''}" for m in shown]
    lines += [*situation.owner_prefs.style_notes, *situation.contact_notes]
    lines += [contact_name, _local(now, zone)]
    return "\n".join(lines)


def leak_candidates(
    situations: Sequence[E2Situation], chats: Mapping[str, Conversation]
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Per situation: the distinctive strings of the *other* chats the account
    holds, which its draft must never contain. A new chat's canary; an E1 chat's
    `key_facts` values that its loaded part shows (at least
    `MIN_DISTINCTIVE_CHARS` long). Situations cut from the same E1 chat share its
    facts, so they are not each other's leak."""
    own: dict[str, tuple[str | None, tuple[str, ...]]] = {}
    for situation in situations:
        if situation.canary is not None:
            own[situation.id] = (None, (situation.canary,))
            continue
        assert situation.source is not None
        chat = chats[situation.source.e1_conversation]
        shown = fold(" ".join(m.text or "" for m in situation.shown_messages(chats)))
        values = [
            value
            for fact in chat.labels.key_facts
            for value in fact.values
            if len(fold(value)) >= MIN_DISTINCTIVE_CHARS and fold(value) in shown
        ]
        own[situation.id] = (chat.id, tuple(dict.fromkeys(values)))
    return {
        situation.id: {
            other: strings
            for other, (chat, strings) in own.items()
            if other != situation.id and strings and (chat is None or chat != own[situation.id][0])
        }
        for situation in situations
    }


# ------------------------------------------------------------------ checks
@dataclass(frozen=True, slots=True)
class Leak:
    #: The situation whose chat the string belongs to.
    source: str
    text: str


def canary_leaks(
    text: str, *, candidates: Mapping[str, Sequence[str]], own_context: str
) -> tuple[Leak, ...]:
    """The other chats' distinctive strings in `text` (folded: case, apostrophes).
    A string the situation's own context shows is not a leak."""
    draft, mine = fold(text), fold(own_context)
    return tuple(
        Leak(source, string)
        for source, strings in candidates.items()
        for string in strings
        if (folded := fold(string)) and folded in draft and folded not in mine
    )


def _values(text: str) -> list[str]:
    """Numbers, clock times and amounts as values (`agent_mode.value_key`:
    `18:00` = `6:00 p.m.` = `h1800`, `250 000` = `250000`), endings dropped."""
    found = (_LEADING_VALUE.match(token) for token in value_key(text).split())
    return [match.group() for match in found if match is not None]


def unsupported_values(text: str, context: str) -> tuple[str, ...]:
    """The values `text` states that `context` does not hold (a simple
    extractor for the eval's `date`/`amount` flag; not M06's grounding rule)."""
    known = set(_values(context))
    return tuple(dict.fromkeys(value for value in _values(text) if value not in known))


@dataclass(frozen=True, slots=True)
class LanguageCheck:
    #: The language and script the reply should be in, or None when "match"
    #: could not be decided from the contact's message.
    target: OwnerLanguage | None
    ok: bool | None
    detected: str | None = None
    script_share: float = 0.0


def reply_target(situation: E2Situation, last_contact: str) -> OwnerLanguage | None:
    """`owner_prefs.reply_language`, with "match" read from the contact's last
    message (its language and script), else the situation's variant."""
    wanted = situation.owner_prefs.reply_language
    if wanted != "match":
        return wanted
    language, script = detect_language(last_contact), script_of(last_contact)
    if language == "uz" and script in ("latin", "cyrillic"):
        return "uz-Latn" if script == "latin" else "uz-Cyrl"
    if language in ("ru", "en"):
        return "ru" if language == "ru" else "en"
    # Too little evidence in the message: the situation's variant, unless mixed.
    return next((target for target in _OWNER_LANGUAGES if target == situation.variant), None)


@dataclass(frozen=True, slots=True)
class DraftChecks:
    needs_reply_agrees: bool
    #: 1-4096 characters when a reply was needed; None when none was.
    length_ok: bool | None
    leaks: tuple[Leak, ...]
    unsupported_values: tuple[str, ...]
    #: `makes_commitment` while the context holds no owner agreement.
    unsupported_commitment: bool
    sensitive_agrees: bool
    language: LanguageCheck

    def as_json(self) -> dict[str, Any]:
        return {
            "needs_reply_agrees": self.needs_reply_agrees,
            "length_ok": self.length_ok,
            "leaks": [{"source": leak.source, "text": leak.text} for leak in self.leaks],
            "unsupported_values": list(self.unsupported_values),
            "unsupported_commitment": self.unsupported_commitment,
            "sensitive_agrees": self.sensitive_agrees,
            "language": {
                "target": self.language.target,
                "ok": self.language.ok,
                "detected": self.language.detected,
                "script_share": round(self.language.script_share, 3),
            },
        }


def check_draft(
    situation: E2Situation,
    result: DraftResult,
    *,
    context: str,
    candidates: Mapping[str, Sequence[str]],
    last_contact: str | None = None,
) -> DraftChecks:
    """The deterministic checks of one draft (spec E2 §B.3)."""
    expected = situation.expected
    text = result.text if result.needs_reply else ""
    if last_contact is None:
        conversation = situation.conversation
        last_contact = (conversation.messages[-1].text or "") if conversation is not None else ""
    target = reply_target(situation, last_contact)
    if not text or target is None:
        language = LanguageCheck(target, None)
    else:
        verdict = check_language(text, target)
        language = LanguageCheck(target, verdict.ok, verdict.detected, verdict.script_share)
    return DraftChecks(
        needs_reply_agrees=result.needs_reply == expected.needs_reply,
        length_ok=(1 <= len(text) <= MAX_DRAFT_CHARS) if expected.needs_reply else None,
        leaks=canary_leaks(text, candidates=candidates, own_context=context),
        unsupported_values=unsupported_values(text, context),
        unsupported_commitment=bool(text)
        and result.makes_commitment
        and not expected.makes_commitment_ok,
        sensitive_agrees=result.sensitive_topic == expected.sensitive_topic,
        language=language,
    )


# ------------------------------------------------------------------- judge
class FactVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    conveyed: bool


class Violation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ViolationKind
    quote: str


class IntentScreen(BaseModel):
    """The judge's structured output."""

    model_config = ConfigDict(extra="forbid")

    intent: DraftedIntent
    facts: list[FactVerdict]
    violations: list[Violation]


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """The pre-screen of one draft: the judge's verdicts, with the literal
    checks applied first. A pre-screen only, never a result."""

    #: The judge read the draft and answered.
    screened: bool
    error: str | None = None
    #: The draft's intent (`no_reply_needed` for a draft that declined to reply).
    intent: str | None = None
    #: The intent equals the expected one; None when nobody could tell.
    intent_match: bool | None = None
    #: Each `must_include` fact and whether the draft carries it (literally, or
    #: by the judge).
    facts: tuple[tuple[str, bool], ...] = ()
    violations: tuple[tuple[ViolationKind, str], ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "screened": self.screened,
            "error": self.error,
            "intent": self.intent,
            "intent_match": self.intent_match,
            "facts": [{"fact": fact, "covered": covered} for fact, covered in self.facts],
            "violations": [{"kind": kind, "quote": quote} for kind, quote in self.violations],
        }


def render_chat(
    shown: Sequence[Message], *, owner: str, contact: str, timezone: str = OWNER_TIMEZONE
) -> str:
    """The chat as the judge and the rating sheet show it (times in the owner's
    timezone; an edited message's text is its current one). Never labels."""
    zone = ZoneInfo(timezone)
    lines = [f"Conversation with {contact} (times in the owner's timezone, {timezone})"]
    for message in shown:
        who = owner if message.direction == "owner" else contact
        edited = " (edited; this is its current text)" if message.edited_at is not None else ""
        media = f"[{message.media}]" if message.media else ""
        body = " ".join(part for part in (media, message.text or "") if part)
        lines.append(
            f"[{message.id}] {_local(message.sent_at, zone)} {who} ({message.direction})"
            f"{edited}: {body}"
        )
    return "\n".join(lines)


def judge_input(situation: E2Situation, chat: str, draft_text: str, contact: str) -> str:
    expected = situation.expected
    notes = [f"- (style) {note}" for note in situation.owner_prefs.style_notes]
    notes += [f"- (about the contact) {note}" for note in situation.contact_notes]
    facts = [
        f"  {number}. {item.fact} (from {item.source})"
        for number, item in enumerate(expected.must_include, start=1)
    ]
    traps = [
        f"  {number}. ({item.kind}) {item.text}"
        for number, item in enumerate(expected.must_not, start=1)
    ]
    return "\n".join(
        [
            chat,
            "",
            "Owner's notes for this reply:",
            *(notes or ["- (none)"]),
            "",
            "Expected of a good reply:",
            f"- intent: {expected.intent}",
            "- facts a good reply conveys:",
            *(facts or ["  (none)"]),
            "- must not:",
            *(traps or ["  (none)"]),
            "",
            f"Draft (written as the owner, to {contact}):",
            draft_text,
        ]
    )


class DraftJudge:
    """Pre-screens one draft with a model through the `LLMClient` port."""

    def __init__(
        self, llm: LLMClient, *, model: str, effort: str | None = None, account_id: int = 1
    ) -> None:
        self._llm = llm
        self._model = model
        self._effort = effort
        self._account_id = account_id
        self._rubric = load_asset("rubrics", RUBRIC_ID)

    async def screen(
        self, situation: E2Situation, result: DraftResult | None, chat: str, contact: str
    ) -> JudgeResult:
        expected = situation.expected
        if result is None:
            return JudgeResult(screened=False, error="no_draft")
        if not result.needs_reply:
            # Declining to reply is an intent string matching can decide.
            return JudgeResult(
                screened=False,
                intent="no_reply_needed",
                intent_match=expected.intent == "no_reply_needed",
                facts=tuple((item.fact, False) for item in expected.must_include),
            )
        literal = [fold(item.fact) in fold(result.text) for item in expected.must_include]
        request = LLMRequest(
            account_id=self._account_id,
            purpose=JUDGE_PURPOSE,
            model=self._model,
            instructions=self._rubric.text,
            items=(
                TextItem(
                    "user",
                    judge_input(situation, chat, result.text, contact),
                    # Synthetic, but contact-authored chat text by construction.
                    Provenance.MANAGED,
                ),
            ),
            max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS,
            timeout_seconds=JUDGE_TIMEOUT_SECONDS,
            effort=self._effort,
        )
        facts = tuple(zip((item.fact for item in expected.must_include), literal, strict=True))
        try:
            response = await self._llm.parse(request, IntentScreen)
        except LLMError as error:
            return JudgeResult(screened=False, error=error.kind.value, facts=facts)
        if response.stop_reason is not StopReason.COMPLETED or response.parsed is None:
            return JudgeResult(screened=False, error=response.stop_reason.value, facts=facts)
        screen = response.parsed
        conveyed = {verdict.number for verdict in screen.facts if verdict.conveyed}
        return JudgeResult(
            screened=True,
            intent=screen.intent,
            intent_match=screen.intent == expected.intent,
            facts=tuple(
                (fact, covered or number in conveyed)
                for number, (fact, covered) in enumerate(facts, start=1)
            ),
            violations=tuple((v.kind, v.quote) for v in screen.violations),
        )


# ------------------------------------------------------------------ ratings
@dataclass(frozen=True, slots=True)
class Acceptability:
    per_variant: dict[str, Measure]
    overall: Measure
    #: Keys of the drafts the owner has not rated: listed, never counted.
    unrated: tuple[str, ...]


def acceptability(drafts: Sequence[tuple[str, str, str]], ledger: ReviewLedger) -> Acceptability:
    """(as_is + minor) / rated, per variant, over `(situation, variant, rating
    key)` triples. An unrated draft is listed, never counted."""
    grouped: dict[str, list[bool]] = {}
    unrated: list[str] = []
    for _, variant, key in drafts:
        rating = ledger.rating(key)
        if rating is None:
            unrated.append(key)
            continue
        grouped.setdefault(variant, []).append(rating in ACCEPTABLE_RATINGS)
    flags = [flag for group in grouped.values() for flag in group]
    return Acceptability(
        {variant: Measure(sum(group), len(group)) for variant, group in sorted(grouped.items())},
        Measure(sum(flags), len(flags)),
        tuple(unrated),
    )


# ------------------------------------------------------------------- records
@dataclass(frozen=True, slots=True)
class DraftRecord:
    situation_id: str
    variant: Variant
    #: `new`, or `e1:<chat>@<cut message>`.
    source: str
    #: The `DraftStatus` value.
    status: str
    error: str | None
    #: Draft calls made (a truncated draft is retried once at a larger budget).
    calls: int
    result: DraftResult | None
    checks: DraftChecks | None
    judge: JudgeResult
    #: Tokens of the draft calls (the judge is metered in the report's cost).
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def text(self) -> str:
        return self.result.text if self.result is not None and self.result.needs_reply else ""

    @property
    def rating_key(self) -> str | None:
        return rating_key(self.situation_id, self.text) if self.text else None

    def as_json(self, ledger: ReviewLedger) -> dict[str, Any]:
        """One record for the JSON report. The draft is model output over a
        **synthetic** situation: kept so the owner can rate it."""
        key = self.rating_key
        return {
            "id": self.situation_id,
            "variant": self.variant,
            "source": self.source,
            "status": self.status,
            "error": self.error,
            "draft_calls": self.calls,
            "draft_usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
            "draft": None if self.result is None else self.result.model_dump(mode="json"),
            "rating_key": key,
            "rating": None if key is None else ledger.rating(key),
            "checks": None if self.checks is None else self.checks.as_json(),
            "judge": self.judge.as_json(),
        }


class DraftReport(NamedTuple):
    metrics: tuple[MetricResult, ...]
    breakdowns: dict[str, dict[str, Measure]]
    notes: tuple[str, ...]
    #: What the report states first: the canary leaks.
    headline: tuple[str, ...]


def _count(
    key: str,
    label: str,
    items: Sequence[DraftRecord],
    offending: Sequence[str],
    note: str = "",
) -> MetricResult:
    """An informational count: no threshold, no gate (P2)."""
    return MetricResult(
        key=key,
        label=label,
        unit="count",
        goal="at_most",
        threshold=None,
        all_items=Measure(len(offending), len(items)),
        confirmed=Measure(0, 0),
        failed_items=tuple(offending),
        note=note,
        gated=False,
    )


def _ratio(
    key: str, label: str, outcomes: Mapping[str, bool], ledger: ReviewLedger, note: str = ""
) -> MetricResult:
    return ratio_metric(
        key=key,
        label=label,
        outcomes=outcomes,
        threshold=None,
        ledger=ledger,
        note=note,
        gated=False,
    )


def _by_variant(
    records: Sequence[DraftRecord], outcome: Callable[[DraftRecord], list[bool]]
) -> dict[str, Measure]:
    grouped: dict[str, list[bool]] = {}
    for record in records:
        grouped.setdefault(record.variant, []).extend(outcome(record))
    return {
        variant: Measure(sum(flags), len(flags))
        for variant, flags in sorted(grouped.items())
        if flags
    }


def draft_metrics(
    records: Sequence[DraftRecord], ledger: ReviewLedger, *, set_size: int
) -> DraftReport:
    """Every check of spec E2 §B.3, per run and per variant. All informational."""
    drafted = [r for r in records if r.checks is not None]
    checked = [(r, c) for r in drafted if (c := r.checks) is not None]
    with_text = [r for r in drafted if r.text]
    leaked = [r for r, c in checked if c.leaks]
    rated = acceptability(
        [(r.situation_id, r.variant, key) for r in records if (key := r.rating_key)], ledger
    )
    metrics = [
        _count(
            "canary_leaks",
            "Canary leaks (another chat's string in a draft; target 0)",
            with_text,
            [r.situation_id for r in leaked],
        ),
        ratio_metric(
            key="drafted",
            label="Valid draft output (a draft, or no reply needed)",
            outcomes={r.situation_id: r.checks is not None for r in records},
            threshold=None,
            ledger=ledger,
            set_size=set_size,
            gated=False,
        ),
        _ratio(
            "needs_reply_agreement",
            "needs_reply agrees with the label",
            {r.situation_id: c.needs_reply_agrees for r, c in checked},
            ledger,
        ),
        _ratio(
            "reply_length",
            "Reply of 1-4096 characters where one was needed",
            {r.situation_id: c.length_ok for r, c in checked if c.length_ok is not None},
            ledger,
        ),
        _count(
            "unsupported_values",
            "Drafts stating a number, time or amount the context does not hold",
            with_text,
            [r.situation_id for r, c in checked if c.unsupported_values],
        ),
        _count(
            "unsupported_commitments",
            "Drafts making a commitment the context does not support",
            with_text,
            [r.situation_id for r, c in checked if c.unsupported_commitment],
        ),
        _ratio(
            "sensitive_agreement",
            "sensitive_topic agrees with the label",
            {r.situation_id: c.sensitive_agrees for r, c in checked},
            ledger,
        ),
        _ratio(
            "reply_language",
            "Reply in the owner's reply language and script (P2-O11, informational)",
            {r.situation_id: bool(c.language.ok) for r, c in checked if c.language.ok is not None},
            ledger,
        ),
        _ratio(
            "intent_match",
            "Intent matches the label (judge pre-screen)",
            {
                r.situation_id: bool(r.judge.intent_match)
                for r in drafted
                if r.judge.intent_match is not None
            },
            ledger,
        ),
        _ratio(
            "must_include",
            "Expected facts carried (literal match, else judge pre-screen)",
            {
                f"{r.situation_id}.f{number}": covered
                for r in drafted
                for number, (_, covered) in enumerate(r.judge.facts, start=1)
            },
            ledger,
        ),
        _count(
            "must_not_violations",
            "Judge-flagged violations (commitment, other chat, personal data, claims human)",
            [r for r in with_text if r.judge.screened],
            [f"{r.situation_id}:{kind}" for r in with_text for kind, _ in r.judge.violations],
        ),
        MetricResult(
            key="acceptability",
            label="Acceptable drafts, owner-rated (as is + minor edit) / rated",
            unit="ratio",
            goal="at_least",
            threshold=None,
            all_items=rated.overall,
            confirmed=rated.overall,
            note=f"{len(rated.unrated)} draft(s) unrated: never counted",
            gated=False,
        ),
    ]
    breakdowns = {
        "canary leaks by variant": _by_variant(
            with_text, lambda r: [bool(r.checks and r.checks.leaks)]
        ),
        "needs_reply agreement by variant": _by_variant(
            drafted, lambda r: [bool(r.checks and r.checks.needs_reply_agrees)]
        ),
        "unsupported values by variant": _by_variant(
            with_text, lambda r: [bool(r.checks and r.checks.unsupported_values)]
        ),
        "unsupported commitments by variant": _by_variant(
            with_text, lambda r: [bool(r.checks and r.checks.unsupported_commitment)]
        ),
        "sensitive_topic agreement by variant": _by_variant(
            drafted, lambda r: [bool(r.checks and r.checks.sensitive_agrees)]
        ),
        "reply language by variant": _by_variant(
            drafted,
            lambda r: (
                [bool(r.checks.language.ok)]
                if r.checks is not None and r.checks.language.ok is not None
                else []
            ),
        ),
        "intent match (judge) by variant": _by_variant(
            drafted,
            lambda r: [bool(r.judge.intent_match)] if r.judge.intent_match is not None else [],
        ),
        "must_include coverage by variant": _by_variant(
            drafted, lambda r: [covered for _, covered in r.judge.facts]
        ),
        "acceptability (owner ratings) by variant": rated.per_variant,
    }
    if leaked:
        listed = ", ".join(
            f"{r.situation_id} (from {', '.join(sorted({leak.source for leak in c.leaks}))})"
            for r, c in checked
            if c.leaks
        )
        headline = [f"{len(leaked)} canary leak(s): {listed}"]
    else:
        headline = ["Canary leaks: 0 (no draft holds another chat's canary or distinctive string)"]
    headline.append("Informational (P2): no metric has a threshold or a gate.")
    notes: list[str] = []
    failed = [f"{r.situation_id} ({r.status}:{r.error})" for r in records if r.checks is None]
    if failed:
        notes.append(f"{len(failed)} situation(s) returned no valid draft: " + ", ".join(failed))
    unscreened = [r.situation_id for r in with_text if not r.judge.screened]
    if unscreened:
        notes.append("not screened (judge failed): " + ", ".join(unscreened))
    undetermined = [r.situation_id for r, c in checked if c.language.target is None and r.text]
    if undetermined:
        notes.append("reply language not determined (match, mixed): " + ", ".join(undetermined))
    if rated.unrated:
        unrated_ids = [key.split("#", 1)[0] for key in rated.unrated]
        notes.append(
            f"{len(rated.unrated)} draft(s) await the owner's rating "
            "(`python -m evals review drafts --export`): " + ", ".join(unrated_ids)
        )
    return DraftReport(tuple(metrics), breakdowns, tuple(notes), tuple(headline))


# ------------------------------------------------------------ the target
def _note(scope: str, kind: str, content: str, now: datetime) -> MemoryView:
    """A note as the drafter's context shows it, for the pre-run estimate only."""
    return MemoryView(
        memory_id=0,
        scope=scope,
        kind=kind,
        content=content,
        contact_id=0 if scope == SCOPE_CONTACT else None,
        conversation_id=None,
        applies_to_replies=scope == SCOPE_OWNER,
        tainted=False,
        created_at=now,
        source="owner_stated",
    )


class _Meter:
    """Counts the draft calls of one situation and their tokens."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.processor = inner.processor
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def _add(self, response: LLMResponse | ParsedResponse[Any]) -> None:
        self.calls += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self._inner.complete(request)
        self._add(response)
        return response

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        response = await self._inner.parse(request, schema)
        self._add(response)
        return response


@dataclass(frozen=True, slots=True)
class _Loaded:
    account_id: int
    #: Situation id -> its conversation and contact rows.
    conversations: Mapping[str, int]
    contacts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _Situation:
    """What the target derives once per situation."""

    situation: E2Situation
    shown: tuple[Message, ...]
    owner: str
    contact: str
    now: datetime
    context: str
    candidates: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def last_contact(self) -> str:
        return next((m.text or "" for m in reversed(self.shown) if m.direction == "contact"), "")


class DraftsTarget:
    name = "drafts"
    dataset: DatasetName = "e2"
    route = ROUTE
    uses_model = True

    def __init__(
        self,
        situations: Sequence[E2Situation],
        e1: Sequence[Conversation],
        *,
        owner_language: OwnerLanguage,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._situations = list(situations)
        self._e1 = list(e1)
        self._chats = {c.id: c for c in e1}
        self.owner_language: OwnerLanguage = owner_language
        self._factory = session_factory

    @property
    def label(self) -> str:
        """What the report calls this run's variant: the owner's language."""
        return f"drafts-{self.owner_language}"

    def bind(self, session_factory: async_sessionmaker[AsyncSession]) -> DraftsTarget:
        """The same target over a database (the runner refuses and estimates first)."""
        return DraftsTarget(
            self._situations,
            self._e1,
            owner_language=self.owner_language,
            session_factory=session_factory,
        )

    @property
    def _database(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            raise RuntimeError("bind() a database before running the drafts target")
        return self._factory

    def _selected(self, context: RunContext) -> list[E2Situation]:
        if context.only is not None:
            return self.named(context.only, limit=context.limit)
        if context.limit is None:
            return self._situations
        if context.limit < 1:
            raise ValueError("limit must be at least 1")
        return self._situations[: context.limit]

    def named(self, ids: Sequence[str], *, limit: int | None = None) -> list[E2Situation]:
        """The situations `--only` names, in set order: always a trial, so at least
        one and fewer than all, and never with a limit."""
        if limit is not None:
            raise ValueError("only and limit cannot be combined")
        wanted = set(ids)
        unknown = sorted(wanted - {s.id for s in self._situations})
        if unknown:
            raise ValueError(f"only names unknown situations: {', '.join(unknown)}")
        if not wanted or len(wanted) >= len(self._situations):
            raise ValueError("only must name at least one situation and not the whole set")
        return [s for s in self._situations if s.id in wanted]

    def _derive(
        self,
        situation: E2Situation,
        window: int,
        candidates: Mapping[str, tuple[str, ...]] | None = None,
    ) -> _Situation:
        owner, contact = participants(situation, self._chats)
        shown = tuple(shown_window(situation, self._chats, window))
        now = draft_instant(situation, self._chats)
        return _Situation(
            situation,
            shown,
            owner,
            contact,
            now,
            context_text(situation, shown, contact_name=contact, now=now),
            candidates or {},
        )

    # ------------------------------------------------------------- estimate
    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        """Every call a run can make, at its worst: the draft call and its one
        truncation retry at the larger budget (`Drafter._call`), and the judge
        reading a draft of the longest length the product allows."""
        config = DraftConfig(model="")  # the product's budgets, no model needed
        larger = min(config.max_output_tokens * 2, config.max_output_tokens_ceiling)
        rubric = len(load_asset("rubrics", RUBRIC_ID).text)
        calls: list[PlannedCall] = []
        for situation in self._selected(context):
            derived = self._derive(situation, config.window_messages)
            chars = self._draft_chars(derived)
            calls.append(PlannedCall(DRAFT_PURPOSE, chars, config.max_output_tokens))
            if larger > config.max_output_tokens:
                calls.append(PlannedCall(DRAFT_PURPOSE, chars, larger))
            chat = render_chat(derived.shown, owner=derived.owner, contact=derived.contact)
            judge = rubric + len(judge_input(situation, chat, "", derived.contact))
            calls.append(
                PlannedCall(JUDGE_PURPOSE, judge + MAX_DRAFT_CHARS, JUDGE_MAX_OUTPUT_TOKENS)
            )
        return calls

    def _draft_chars(self, derived: _Situation) -> int:
        situation = derived.situation
        prefs = situation.owner_prefs
        account = Account(
            timezone=OWNER_TIMEZONE,
            locale_pref=self.owner_language,
            settings={
                FIELD_REPLY_LANGUAGE: prefs.reply_language,
                FIELD_REPLY_TONE: prefs.reply_tone,
                FIELD_REPLY_STYLE: prefs.reply_style,
            },
        )
        notes = [
            _note(SCOPE_OWNER, "instruction", note, derived.now) for note in prefs.style_notes
        ] + [_note(SCOPE_CONTACT, "fact", note, derived.now) for note in situation.contact_notes]
        messages = [
            SourceMessage(
                1000 + number,
                "contact" if m.direction == "contact" else "owner_manual",
                m.sent_at,
                m.media or "text",
                m.text,
                m.media,
                m.edited_at,
            )
            for number, m in enumerate(derived.shown, start=1)
        ]
        built = build_draft_reply(
            prompt=prompt_registry.active(RUN_TYPE),
            account=account,
            messages=messages,
            now=derived.now,
            notes=notes,
            contact_name=derived.contact,
        )
        return (
            len(built.instructions) + sum(len(item.text) for item in built.items) + _ID_SLACK_CHARS
        )

    # ------------------------------------------------------------------ run
    async def run(self, context: RunContext) -> TargetResult:
        if context.llm is None or context.model is None:
            raise ValueError("the drafts target needs a model client and a model id")
        processor = processor_for_model(context.model)
        if processor is None:
            raise ValueError("the model has no known processor to name in the synthetic consent")
        selected = self._selected(context)
        window = DraftConfig(model=context.model).window_messages
        candidates = leak_candidates(self._situations, self._chats)
        records: list[DraftRecord] = []
        not_evaluated: list[str] = []
        stopped: str | None = None
        loaded = await self._load(processor)
        try:
            for situation in selected:
                derived = self._derive(situation, window, candidates[situation.id])
                try:
                    record = await self._evaluate(derived, loaded, context)
                except BudgetExceededError as stop:
                    stopped = str(stop)
                    break
                if record.status == DraftStatus.FAILED.value and record.error in OUTAGE_ERRORS:
                    not_evaluated.append(situation.id)  # the provider, not the model
                else:
                    records.append(record)
        finally:
            await unload_corpus(self._database, loaded.account_id)
        return self._result(
            records,
            planned=len(selected),
            ledger=context.reviews,
            stopped=stopped,
            not_evaluated=not_evaluated,
        )

    async def _evaluate(
        self, derived: _Situation, loaded: _Loaded, context: RunContext
    ) -> DraftRecord:
        assert context.llm is not None
        assert context.model is not None
        situation = derived.situation
        await self._reset(situation, loaded)
        meter = _Meter(context.llm)
        drafter = Drafter(
            session_factory=self._database,
            llm=meter,
            clock=_FixedClock(derived.now),
            config=DraftConfig(model=context.model, effort=context.effort),
        )
        outcome = await drafter.draft(
            account_id=loaded.account_id,
            conversation_id=loaded.conversations[situation.id],
            reason=EVAL_REASON,
        )
        if outcome.status in (
            DraftStatus.NO_CONSENT,
            DraftStatus.NOT_DRAFTABLE,
            DraftStatus.OVER_BUDGET,
        ):
            # The harness is mis-built, not the model's miss: nothing here is a result.
            raise RuntimeError(f"the draft run could not start ({outcome.status.value})")
        result = outcome.result
        checks = (
            None
            if result is None
            else check_draft(
                situation,
                result,
                context=derived.context,
                candidates=derived.candidates,
                last_contact=derived.last_contact,
            )
        )
        chat = render_chat(derived.shown, owner=derived.owner, contact=derived.contact)
        judge = await DraftJudge(
            context.llm, model=context.model, effort=context.effort, account_id=loaded.account_id
        ).screen(situation, result, chat, derived.contact)
        source = situation.source
        return DraftRecord(
            situation_id=situation.id,
            variant=situation.variant,
            source="new" if source is None else f"e1:{source.e1_conversation}@{source.cut_after}",
            status=outcome.status.value,
            error=outcome.error_class,
            calls=meter.calls,
            result=result,
            checks=checks,
            judge=judge,
            input_tokens=meter.input_tokens,
            output_tokens=meter.output_tokens,
        )

    # -------------------------------------------------------------- the data
    async def _load(self, processor: str) -> _Loaded:
        """One account holding every situation's chat (the module docstring,
        steps 1-2), in one transaction: a failure leaves nothing behind."""
        conversations: dict[str, int] = {}
        contacts: dict[str, int] = {}
        first = min(m.sent_at for s in self._situations for m in s.shown_messages(self._chats))
        async with self._database() as session, session.begin():
            account = Account(
                telegram_user_id=_SYNTHETIC_OWNER_ID + secrets.randbelow(900_000_000),
                status="active",
                timezone=OWNER_TIMEZONE,
                locale_pref=self.owner_language,
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
                    clock=_FixedClock(first - timedelta(days=1)),
                )
            for number, situation in enumerate(self._situations, start=1):
                messages = situation.shown_messages(self._chats)
                _, contact_name = participants(situation, self._chats)
                contact = Contact(
                    account_id=account.id,
                    telegram_user_id=2_000_000 + number,
                    display_name=contact_name,
                )
                session.add(contact)
                await session.flush()
                row = ConversationRow(
                    account_id=account.id,
                    business_chat_id=2_000_000 + number,
                    contact_id=contact.id,
                    coverage_started_at=min(m.sent_at for m in messages),
                )
                session.add(row)
                await session.flush()
                for index, message in enumerate(messages, start=1):
                    values = row_values(message, index)
                    session.add(MessageRow(account_id=account.id, conversation_id=row.id, **values))
                await session.flush()
                conversations[situation.id] = row.id
                contacts[situation.id] = contact.id
            account_id = account.id
        return _Loaded(account_id, conversations, contacts)

    async def _reset(self, situation: E2Situation, loaded: _Loaded) -> None:
        """This situation's preferences and notes, and nobody else's (step 3)."""
        prefs = situation.owner_prefs
        flags = provenance(tainted=False, managed=False)
        async with self._database() as session, session.begin():
            await session.execute(
                delete(MemoryItem).where(MemoryItem.account_id == loaded.account_id)
            )
            account = await session.get(Account, loaded.account_id)
            assert account is not None
            account.settings = {
                **(account.settings or {}),
                FIELD_REPLY_LANGUAGE: prefs.reply_language,
                FIELD_REPLY_TONE: prefs.reply_tone,
                FIELD_REPLY_STYLE: prefs.reply_style,
            }
            for note in prefs.style_notes:
                session.add(
                    MemoryItem(
                        account_id=loaded.account_id,
                        scope=SCOPE_OWNER,
                        kind="instruction",
                        content=note,
                        applies_to_replies=True,
                        provenance=flags,
                    )
                )
            for note in situation.contact_notes:
                session.add(
                    MemoryItem(
                        account_id=loaded.account_id,
                        scope=SCOPE_CONTACT,
                        contact_id=loaded.contacts[situation.id],
                        kind="fact",
                        content=note,
                        provenance=flags,
                    )
                )

    # --------------------------------------------------------------- result
    def _result(
        self,
        records: Sequence[DraftRecord],
        *,
        planned: int,
        ledger: ReviewLedger,
        stopped: str | None,
        not_evaluated: Sequence[str],
    ) -> TargetResult:
        report = draft_metrics(records, ledger, set_size=len(self._situations))
        notes = list(report.notes)
        retried = sum(r.calls > 1 for r in records)
        if retried:
            notes.append(f"{retried} draft call(s) were truncated once and retried larger.")
        if not_evaluated:
            notes.append(
                "not evaluated (the provider was unavailable): " + ", ".join(not_evaluated)
            )
        if stopped:
            notes.append(f"STOPPED EARLY: {stopped}; {len(records)} of {planned} items ran")
        return TargetResult(
            target=self.name,
            dataset=self.dataset,
            items=len(records),
            items_planned=planned,
            items_total=len(self._situations),
            metrics=report.metrics,
            details=tuple(r.as_json(ledger) for r in records),
            breakdowns=report.breakdowns,
            notes=tuple(notes),
            prompts=(prompt_registry.active(RUN_TYPE).label, RUBRIC_ID),
            not_evaluated=tuple(not_evaluated),
            headline=report.headline,
        )


@asynccontextmanager
async def isolated_drafts(base_url: str, target: DraftsTarget) -> AsyncIterator[DraftsTarget]:
    """`target` over a migrated throwaway database (`assistant_eval_<random>` on
    the server of `base_url`), dropped on exit. Touches no existing database."""
    async with scratch_database(base_url) as url:
        engine = build_engine(url)
        try:
            yield target.bind(build_session_factory(engine))
        finally:
            await engine.dispose()


# ----------------------------------------------------------- the rating sheet
@dataclass(frozen=True, slots=True)
class RatingStatus:
    #: Drafts with text in the report (the ratable ones).
    drafts: int
    acceptability: Acceptability


def rating_status(report: Mapping[str, Any], ledger: ReviewLedger) -> RatingStatus:
    """Acceptability of a recorded run's drafts under the ledger's ratings now."""
    drafts = [
        (detail["id"], detail["variant"], detail["rating_key"])
        for detail in report["details"]
        if detail.get("rating_key")
    ]
    return RatingStatus(len(drafts), acceptability(drafts, ledger))


def rating_sheet(
    report: Mapping[str, Any],
    situations: Sequence[E2Situation],
    e1: Sequence[Conversation],
    ledger: ReviewLedger,
) -> str:
    """The Markdown sheet the owner rates a recorded run's drafts on: per draft,
    the situation, its preferences and notes, the end of the chat it answers,
    the draft, and the ledger key to record the rating under. Synthetic data."""
    chats = {c.id: c for c in e1}
    by_id = {s.id: s for s in situations}
    status = rating_status(report, ledger)
    rated = status.acceptability.overall.denominator
    window = DraftConfig(model="").window_messages
    lines = [
        f"# E2 draft ratings: {report.get('model')} ({report.get('mode')}), "
        f"{report.get('run_date')}",
        "",
        f"{rated} of {status.drafts} drafts rated. Rate each draft on the owner scale: "
        + ", ".join(f"`{r}`" for r in RATINGS)
        + " (send as is, minor edit, major edit, unusable). Record a rating in the review "
        'ledger (`evals/reviews/e2.json`) as `"ratings": {"<key>": "<rating>"}`; a rating '
        "belongs to the exact draft its key names.",
        "",
    ]
    for detail in report["details"]:
        key = detail.get("rating_key")
        situation = by_id.get(detail["id"])
        if not key or situation is None:
            continue
        prefs = situation.owner_prefs
        owner, contact = participants(situation, chats)
        shown = shown_window(situation, chats, window)[-EXCERPT_MESSAGES:]
        rating = ledger.rating(key)
        lines += [
            f"## {situation.id} ({situation.variant}, intent {situation.expected.intent})",
            "",
            f"Owner preferences: reply language {prefs.reply_language}, tone "
            f"{prefs.reply_tone}, style {prefs.reply_style}",
        ]
        lines += [f"- style note: {note}" for note in prefs.style_notes]
        lines += [f"- about {contact}: {note}" for note in situation.contact_notes]
        lines += ["", "```", render_chat(shown, owner=owner, contact=contact), "```", "", "Draft:"]
        lines += [f"> {line}" if line else ">" for line in detail["draft"]["text"].splitlines()]
        lines += [
            "",
            f"Key: `{key}` - rating: "
            + (f"`{rating}` (recorded)" if rating else "____ (" + " / ".join(RATINGS) + ")"),
            "",
        ]
    return "\n".join(lines)
