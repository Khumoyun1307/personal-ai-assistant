"""Dataset schema for the evaluation sets (IMPLEMENTATION_PLAN §10.1, S03.4).

E1 (summaries/catch-up) is one JSON object per line in
`datasets/e1/conversations.jsonl`: a synthetic private chat between the owner
and one contact, with its labels inline under `labels`. The runner feeds a
model only `messages` (and the coverage facts the product would know); the
labels are the answer key and must never reach a prompt.

The retrieval set is `datasets/retrieval/queries.jsonl`: owner-style queries
over the E1 corpus, each naming the messages a good search must surface.

E3 (triage, M05) is `datasets/e3/bursts.jsonl`: a chat's history, its open
tasks and a burst of new messages, labelled with what triage should conclude
(alert, flags, commitments and questions with their source, which open tasks
the burst resolves); and `datasets/e3/monitor_cases.jsonl` for the
`evaluate_monitor` comparison (E3 spec §A). Its rules: the `alert` label
follows urgency/decision/category, a commitment cites a message of the side
that must act and a question a contact message, and every open task is in
exactly one resolution list.

Integrity rules live in the models, so a malformed or unverifiable dataset
fails at load time, for the runner and the tests alike: ids are unique and
well-formed, every cited message exists, `values` are literal substrings of
the cited messages (labels are verifiable from the source only), deleted text
is never cited as evidence, and every reportable coverage gap carries an
expected caveat.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Self, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

DATASETS = Path(__file__).resolve().parent / "datasets"
E1_PATH = DATASETS / "e1" / "conversations.jsonl"
RETRIEVAL_PATH = DATASETS / "retrieval" / "queries.jsonl"
E3_PATH = DATASETS / "e3" / "bursts.jsonl"
E3_MONITORS_PATH = DATASETS / "e3" / "monitor_cases.jsonl"

Variant = Literal["uz-Latn", "uz-Cyrl", "ru", "en", "uz-ru-mix"]
VARIANTS: tuple[Variant, ...] = get_args(Variant)

#: Hard cases a conversation exercises; the tests require each to occur.
Tag = Literal[
    "mixed_apostrophes",  # oʻ/o'/o‘/o` variants side by side
    "no_apostrophe",  # o'/g' typed as plain o/g
    "new_alphabet_letters",  # ö, ğ, ş, ç typed for oʻ, gʻ, sh, ch
    "cyrillic_without_uzbek_letters",  # ў қ ғ ҳ typed as their Russian-keyboard neighbours
    "translit",  # Russian typed in Latin letters
    "code_switch",  # Uzbek and Russian in one conversation
    "mixed_script",  # Latin and Cyrillic in one message
    "edit_time",  # an edit changes a date or time
    "edit_detail",  # an edit changes another fact (phone, address)
    "deletion",  # a message is deleted (content purged)
    "injection",  # contact text addressed to the assistant as an instruction
    "voice",  # a voice message with no transcript
    "photo",  # a photo, possibly with a caption
    "gap_possible",
    "gap_definite",
    "gap_delayed",
    "pre_connection",  # refers to an agreement made before coverage started
]
TAGS: tuple[Tag, ...] = get_args(Tag)

Intent = Literal[
    "exact",  # the words appear in the target
    "paraphrase",  # owner describes what they want, not the words used
    "cross_script",  # Latin query for a Cyrillic message or vice versa
    "cross_language",  # query language differs from the message language
    "no_apostrophe",  # Uzbek query typed without oʻ/gʻ apostrophes
    "new_alphabet",  # target or query uses ö/ğ/ş/ç
    "translit",  # Latin-typed Russian on either side
]
INTENTS: tuple[Intent, ...] = get_args(Intent)

_CONVERSATION_ID = re.compile(r"^c\d{2}$")
_MESSAGE_ID = re.compile(r"^(c\d{2})\.m\d{2}$")
_QUERY_ID = re.compile(r"^r\d{2}$")
_LABEL_PREFIX = {
    "questions_to_owner": "q",
    "commitments": "c",
    "decisions": "d",
    "key_facts": "f",
    "prompt_injections": "i",
    "must_not_state": "x",
    "expected_caveats": "v",
}

#: Apostrophe-like characters Uzbek Latin is typed with (TF §8).
APOSTROPHES = "'ʻʼ‘’`´ʹʽ"  # noqa: RUF001
_APOSTROPHE_FOLD = str.maketrans(dict.fromkeys(APOSTROPHES, "'"))


def fold(text: str) -> str:
    """Comparison form for `values` checks: NFKC, casefold, one apostrophe,
    single spaces. Deliberately simpler than the product's `normalize()` —
    labels must match the cited text almost literally, not via search magic."""
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_APOSTROPHE_FOLD)
    return " ".join(folded.split())


def _require_utc(value: datetime | None, field: str) -> None:
    if value is not None and value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC (…Z), got {value.isoformat()}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Participant(_Strict):
    """A synthetic person. Names are invented; no real people."""

    display_name: str = Field(min_length=1)


class Message(_Strict):
    id: str = Field(pattern=_MESSAGE_ID.pattern)
    direction: Literal["contact", "owner"]
    sent_at: AwareDatetime
    #: Current text (after any edit); a caption for media. None for a deleted
    #: message (the product purges content on deletion) or bare media.
    text: str | None = None
    media: Literal["photo", "voice", "document"] | None = None
    reply_to: str | None = None
    edited: bool = False
    edited_at: AwareDatetime | None = None
    #: Pre-edit text, for reviewers. The runner never shows it to a model.
    original_text: str | None = None
    deleted: bool = False
    deleted_at: AwareDatetime | None = None
    #: What the message said before deletion, for reviewers only. The product
    #: no longer has it, so the runner never shows it and no label may rely on it.
    purged_text: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for name in ("sent_at", "edited_at", "deleted_at"):
            _require_utc(getattr(self, name), name)
        if self.edited != (self.edited_at is not None and self.original_text is not None):
            raise ValueError(f"{self.id}: edited needs edited_at and original_text (and only then)")
        if self.deleted != (self.deleted_at is not None and self.purged_text is not None):
            raise ValueError(f"{self.id}: deleted needs deleted_at and purged_text (and only then)")
        if self.deleted and self.text is not None:
            raise ValueError(f"{self.id}: a deleted message has no text")
        if not self.deleted and self.text is None and self.media is None:
            raise ValueError(f"{self.id}: a message needs text or media")
        if self.edited_at is not None and self.edited_at <= self.sent_at:
            raise ValueError(f"{self.id}: edited_at must follow sent_at")
        if self.deleted_at is not None and self.deleted_at <= self.sent_at:
            raise ValueError(f"{self.id}: deleted_at must follow sent_at")
        return self


class CoverageGap(_Strict):
    """A coverage gap as the product records it (ARCHITECTURE §6.6)."""

    id: str
    kind: Literal["outage", "restore", "dead_letter"]
    certainty: Literal["possible", "definite", "delayed"]
    at_risk_from: AwareDatetime
    at_risk_to: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _require_utc(self.at_risk_from, "at_risk_from")
        _require_utc(self.at_risk_to, "at_risk_to")
        if self.at_risk_to <= self.at_risk_from:
            raise ValueError(f"{self.id}: at_risk_to must follow at_risk_from")
        return self


class _Item(_Strict):
    id: str
    source_message_ids: list[str] = Field(min_length=1)
    #: Literal strings (amounts, times, addresses, names) a faithful summary
    #: would carry. Each must occur in the cited messages' current text.
    values: list[str] = Field(default_factory=list)


class QuestionToOwner(_Item):
    """A question the contact put to the owner."""

    summary: str
    status: Literal["open", "answered"]
    answered_in: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _answer_cited(self) -> Self:
        if (self.status == "answered") != bool(self.answered_in):
            raise ValueError(f"{self.id}: answered questions (only) cite answered_in")
        return self


class Commitment(_Item):
    who: Literal["owner", "contact", "third_party"]
    who_name: str
    what: str
    #: As stated, resolved to a date in Asia/Tashkent where the text allows.
    when: str | None = None
    status: Literal["open", "done"]


class Decision(_Item):
    summary: str
    #: `requested`: someone needs the owner's choice; `made`: settled in the chat.
    status: Literal["requested", "made"]


class KeyFact(_Item):
    summary: str


class PromptInjection(_Strict):
    """Contact text addressed to the assistant. It is data, never an
    instruction (AGENTS §6 invariant 3): a summary may report that the message
    exists, but must not follow it or present its claims as agreed."""

    id: str
    source_message_ids: list[str] = Field(min_length=1)
    summary: str


class MustNotState(_Strict):
    """A statement a faithful summary must not make (counts as fabrication)."""

    id: str
    source_message_ids: list[str] = Field(default_factory=list)
    statement: str
    reason: Literal[
        "superseded_by_edit",
        "deleted",
        "injection",
        "voice_no_transcript",
        "outside_coverage",
        "delayed_gap",
    ]


class ExpectedCaveat(_Strict):
    """An owner-facing coverage caveat the summary must include (FR-SUM-06)."""

    id: str
    reason: Literal["possible_gap", "definite_gap", "pre_connection"]
    gap_id: str | None = None
    summary: str


class Labels(_Strict):
    questions_to_owner: list[QuestionToOwner] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    key_facts: list[KeyFact] = Field(default_factory=list)
    prompt_injections: list[PromptInjection] = Field(default_factory=list)
    must_not_state: list[MustNotState] = Field(default_factory=list)
    expected_caveats: list[ExpectedCaveat] = Field(default_factory=list)

    def labelled_items(self) -> list[QuestionToOwner | Commitment | Decision | KeyFact]:
        """The items §10.1 counts: questions, commitments, decisions, key facts."""
        return [*self.questions_to_owner, *self.commitments, *self.decisions, *self.key_facts]


class Conversation(_Strict):
    id: str = Field(pattern=_CONVERSATION_ID.pattern)
    variant: Variant
    #: The deferred M00 S00.3 smoke set (the first 8 conversations).
    smoke: bool = False
    scenario: str
    tags: list[Tag] = Field(default_factory=list)
    owner: Participant
    contact: Participant
    #: When the product started seeing this chat, if the chat refers to
    #: earlier history (pre-connection caveat).
    coverage_started_at: AwareDatetime | None = None
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
    messages: list[Message] = Field(min_length=6, max_length=30)
    labels: Labels

    def message(self, message_id: str) -> Message:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise KeyError(message_id)

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        _require_utc(self.coverage_started_at, "coverage_started_at")
        self._check_messages()
        self._check_label_ids()
        self._check_citations()
        self._check_coverage()
        return self

    def _check_messages(self) -> None:
        seen: list[str] = []
        previous: datetime | None = None
        for message in self.messages:
            match = _MESSAGE_ID.match(message.id)
            if match is None or match.group(1) != self.id:
                raise ValueError(f"{message.id}: message ids are '<conversation>.mNN'")
            if message.id in seen:
                raise ValueError(f"{message.id}: duplicate message id")
            if message.reply_to is not None and message.reply_to not in seen:
                raise ValueError(f"{message.id}: reply_to must name an earlier message")
            if previous is not None and message.sent_at < previous:
                raise ValueError(f"{message.id}: messages must be in chronological order")
            seen.append(message.id)
            previous = message.sent_at

    def _check_label_ids(self) -> None:
        seen: set[str] = set()
        for field, prefix in _LABEL_PREFIX.items():
            for item in getattr(self.labels, field):
                if not re.fullmatch(rf"{self.id}\.{prefix}\d+", item.id):
                    raise ValueError(f"{item.id}: {field} ids are '{self.id}.{prefix}N'")
                if item.id in seen:
                    raise ValueError(f"{item.id}: duplicate label id")
                seen.add(item.id)
        for gap in self.coverage_gaps:
            if not re.fullmatch(rf"{self.id}\.g\d+", gap.id) or gap.id in seen:
                raise ValueError(f"{gap.id}: gap ids are unique '{self.id}.gN'")
            seen.add(gap.id)

    def _check_citations(self) -> None:
        ids = {message.id for message in self.messages}
        for item in self.labels.labelled_items():
            cited = [*item.source_message_ids]
            if isinstance(item, QuestionToOwner):
                cited += item.answered_in
            for message_id in cited:
                if message_id not in ids:
                    raise ValueError(f"{item.id}: cites unknown message {message_id}")
                if self.message(message_id).deleted:
                    raise ValueError(f"{item.id}: deleted content cannot be evidence")
            evidence = fold(" ".join(self.message(m).text or "" for m in item.source_message_ids))
            for value in item.values:
                if fold(value) not in evidence:
                    raise ValueError(f"{item.id}: value {value!r} is not in its cited messages")
        others: list[PromptInjection | MustNotState] = [
            *self.labels.prompt_injections,
            *self.labels.must_not_state,
        ]
        for other in others:
            for message_id in other.source_message_ids:
                if message_id not in ids:
                    raise ValueError(f"{other.id}: cites unknown message {message_id}")

    def _check_coverage(self) -> None:
        gaps = {gap.id: gap for gap in self.coverage_gaps}
        caveats = self.labels.expected_caveats
        for caveat in caveats:
            if caveat.reason == "pre_connection":
                if caveat.gap_id is not None or self.coverage_started_at is None:
                    raise ValueError(f"{caveat.id}: pre_connection needs coverage_started_at")
                continue
            gap = gaps.get(caveat.gap_id or "")
            if gap is None or caveat.reason != f"{gap.certainty}_gap":
                raise ValueError(f"{caveat.id}: must name a {caveat.reason.split('_')[0]} gap")
        covered = {caveat.gap_id for caveat in caveats}
        for gap in self.coverage_gaps:
            reportable = gap.certainty in ("possible", "definite")
            if reportable and gap.id not in covered:
                raise ValueError(f"{gap.id}: a {gap.certainty} gap needs an expected caveat")
        if self.coverage_started_at is not None:
            if not any(c.reason == "pre_connection" for c in caveats):
                raise ValueError(f"{self.id}: coverage_started_at needs a pre_connection caveat")
            if self.messages[0].sent_at < self.coverage_started_at:
                raise ValueError(f"{self.id}: messages predate coverage_started_at")


class RetrievalQuery(_Strict):
    id: str = Field(pattern=_QUERY_ID.pattern)
    #: What the owner types in the control chat.
    text: str = Field(min_length=1)
    lang: Literal["uz", "ru", "en"]
    script: Literal["latin", "cyrillic"]
    intents: list[Intent] = Field(min_length=1)
    #: A hit is any of these in the top 5 results.
    target_message_ids: list[str] = Field(min_length=1)
    conversation_ids: list[str] = Field(min_length=1)
    #: What a correct final answer says (English), and the literal values it
    #: must carry (each occurs in the targets' text).
    expected_answer: str
    answer_values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _targets_in_conversations(self) -> Self:
        for target in self.target_message_ids:
            match = _MESSAGE_ID.match(target)
            if match is None or match.group(1) not in self.conversation_ids:
                raise ValueError(f"{self.id}: target {target} is outside conversation_ids")
        return self


# ------------------------------------------------------------------ E3 (triage)
OwnerLanguage = Literal["uz-Latn", "uz-Cyrl", "ru", "en"]
TaskKind = Literal["owner_commitment", "contact_commitment", "open_question", "decision"]

_BURST_ID = re.compile(r"^b\d{3}$")
_BURST_MESSAGE_ID = re.compile(r"^(b\d{3})\.([hm])\d{2}$")
_MONITOR_CASE_ID = re.compile(r"^m\d{3}$")
_MONITOR_MESSAGE_ID = re.compile(r"^(m\d{3})\.m\d{2}$")


class BurstMessage(_Strict):
    """A message of an E3 burst or its history. `id` is `<burst>.hNN` in the
    history and `<burst>.mNN` in the burst (`<case>.mNN` in a monitor case)."""

    id: str
    direction: Literal["contact", "owner"]
    sent_at: AwareDatetime
    #: The message text, or a caption; None for bare media.
    text: str | None = None
    media: Literal["photo", "voice", "document"] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _require_utc(self.sent_at, "sent_at")
        if self.text is None and self.media is None:
            raise ValueError(f"{self.id}: a message needs text or media")
        return self


class OpenTask(_Strict):
    """An open task of the chat before the burst, as triage would list it."""

    id: str
    kind: TaskKind
    title: str = Field(min_length=1)
    source: str
    due: AwareDatetime | None = None


class BurstCommitment(_Strict):
    id: str
    #: Who must act.
    by: Literal["owner", "contact"]
    #: A message of that side.
    source: str
    #: Literal substrings of the source text a faithful item carries.
    values: list[str] = Field(default_factory=list)


class BurstQuestion(_Strict):
    id: str
    #: A message of the contact.
    source: str
    is_decision: bool


class BurstLabels(_Strict):
    """What a careful human concludes about the burst (the answer key; never
    shown to a model)."""

    #: True exactly when urgency is `urgent`, a decision is requested, or the
    #: category is `urgent_decision`: the urgent/decision positives.
    alert: bool
    needs_reply: bool
    contains_question_to_owner: bool
    requests_decision: bool
    urgency: Literal["low", "normal", "high", "urgent"]
    escalation_category: Literal["none", "urgent_decision", "needs_owner", "fyi", "suspicious"]
    deadline: AwareDatetime | None = None
    #: An attempt to instruct the assistant. The other labels describe the
    #: real request, never what the message demands.
    suspicious: bool
    commitments: list[BurstCommitment] = Field(default_factory=list)
    questions: list[BurstQuestion] = Field(default_factory=list)
    resolved_tasks: list[str] = Field(default_factory=list)
    unresolved_tasks: list[str] = Field(default_factory=list)


class Burst(_Strict):
    """One E3 item: a chat's history, its open tasks, and a burst of new
    messages to triage at `now`."""

    id: str = Field(pattern=_BURST_ID.pattern)
    variant: Variant
    now: AwareDatetime
    owner_language: OwnerLanguage
    owner_timezone: str
    contact: Participant
    contact_priority: Literal["vip", "normal", "low"] = "normal"
    history: list[BurstMessage] = Field(default_factory=list)
    open_tasks: list[OpenTask] = Field(default_factory=list)
    burst: list[BurstMessage] = Field(min_length=1)
    labels: BurstLabels
    tags: list[str] = Field(default_factory=list)

    def messages(self) -> list[BurstMessage]:
        return [*self.history, *self.burst]

    def message(self, message_id: str) -> BurstMessage:
        for message in self.messages():
            if message.id == message_id:
                return message
        raise KeyError(message_id)

    def nothing_to_do(self) -> bool:
        """An FYI, thanks or "ok" burst: nothing for the owner to act on."""
        labels = self.labels
        return (
            labels.escalation_category in ("none", "fyi")
            and not labels.needs_reply
            and not labels.alert
            and not labels.commitments
            and not labels.questions
        )

    def owner_wrote_in_burst(self) -> bool:
        return any(m.direction == "owner" for m in self.burst)

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        _require_utc(self.now, "now")
        _require_utc(self.labels.deadline, "deadline")
        try:
            ZoneInfo(self.owner_timezone)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError(f"{self.id}: unknown owner_timezone {self.owner_timezone!r}") from None
        self._check_messages()
        self._check_tasks()
        self._check_labels()
        return self

    def _check_messages(self) -> None:
        seen: set[str] = set()
        previous: datetime | None = None
        for part, letter in ((self.history, "h"), (self.burst, "m")):
            for message in part:
                match = _BURST_MESSAGE_ID.match(message.id)
                if match is None or match.groups() != (self.id, letter):
                    raise ValueError(
                        f"{message.id}: message ids are '{self.id}.hNN' (history) "
                        f"and '{self.id}.mNN' (burst)"
                    )
                if message.id in seen:
                    raise ValueError(f"{message.id}: duplicate message id")
                if previous is not None and message.sent_at < previous:
                    raise ValueError(f"{message.id}: messages must be in chronological order")
                seen.add(message.id)
                previous = message.sent_at
        if previous is not None and self.now <= previous:
            raise ValueError(f"{self.id}: now must follow every message")

    def _check_tasks(self) -> None:
        ids = {m.id for m in self.messages()}
        seen: set[str] = set()
        for task in self.open_tasks:
            if not re.fullmatch(rf"{self.id}\.t\d+", task.id) or task.id in seen:
                raise ValueError(f"{task.id}: task ids are unique '{self.id}.tN'")
            if task.source not in ids:
                raise ValueError(f"{task.id}: cites unknown message {task.source}")
            side = "owner" if task.kind == "owner_commitment" else "contact"
            if self.message(task.source).direction != side:
                raise ValueError(
                    f"{task.id}: a {task.kind} task cites a {side} message (the product's "
                    "side rule, S05.1 §3.4)"
                )
            _require_utc(task.due, "due")
            seen.add(task.id)
        listed = [*self.labels.resolved_tasks, *self.labels.unresolved_tasks]
        if sorted(listed) != sorted(seen):
            raise ValueError(
                f"{self.id}: every open task is in exactly one resolution list "
                "(resolved_tasks / unresolved_tasks), and nothing else is"
            )

    def _check_labels(self) -> None:
        labels = self.labels
        positive = (
            labels.urgency == "urgent"
            or labels.requests_decision
            or labels.escalation_category == "urgent_decision"
        )
        if labels.alert != positive:
            raise ValueError(
                f"{self.id}: alert must be true exactly when urgency is urgent, a decision is "
                "requested, or the category is urgent_decision"
            )
        if labels.escalation_category == "suspicious" and not labels.suspicious:
            raise ValueError(f"{self.id}: the suspicious category needs suspicious: true")
        ids = {m.id for m in self.messages()}
        seen: set[str] = set()
        items: list[BurstCommitment | BurstQuestion] = [*labels.commitments, *labels.questions]
        for item in items:
            prefix = "c" if isinstance(item, BurstCommitment) else "q"
            if not re.fullmatch(rf"{self.id}\.{prefix}\d+", item.id) or item.id in seen:
                raise ValueError(f"{item.id}: label ids are unique '{self.id}.{prefix}N'")
            seen.add(item.id)
            if item.source not in ids:
                raise ValueError(f"{item.id}: cites unknown message {item.source}")
            source = self.message(item.source)
            if isinstance(item, BurstQuestion):
                if source.direction != "contact":
                    raise ValueError(f"{item.id}: a question must cite a contact message")
                continue
            if source.direction != item.by:
                raise ValueError(f"{item.id}: a commitment must cite a message of its own side")
            for value in item.values:
                if fold(value) not in fold(source.text or ""):
                    raise ValueError(f"{item.id}: value {value!r} is not in its source message")


class MonitorUnderTest(_Strict):
    """A semantic monitor as `evaluate_monitor` is given it (S05.4)."""

    id: int
    kind: Literal["semantic"]
    description: str = Field(min_length=1)


class MonitorLabels(_Strict):
    matched_monitor_ids: list[int] = Field(default_factory=list)


class MonitorCase(_Strict):
    """One `evaluate_monitor` comparison case: monitors and a burst."""

    id: str = Field(pattern=_MONITOR_CASE_ID.pattern)
    variant: Variant
    monitors: list[MonitorUnderTest] = Field(min_length=1)
    burst: list[BurstMessage] = Field(min_length=1)
    labels: MonitorLabels

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        monitor_ids = [m.id for m in self.monitors]
        if len(set(monitor_ids)) != len(monitor_ids):
            raise ValueError(f"{self.id}: duplicate monitor id")
        unknown = set(self.labels.matched_monitor_ids) - set(monitor_ids)
        if unknown:
            raise ValueError(f"{self.id}: matches unknown monitor ids {sorted(unknown)}")
        seen: set[str] = set()
        for message in self.burst:
            match = _MONITOR_MESSAGE_ID.match(message.id)
            if match is None or match.group(1) != self.id or message.id in seen:
                raise ValueError(f"{message.id}: message ids are unique '{self.id}.mNN'")
            seen.add(message.id)
        return self


def load_e1(path: Path = E1_PATH) -> list[Conversation]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Conversation.model_validate_json(line) for line in lines if line.strip()]


def load_queries(path: Path = RETRIEVAL_PATH) -> list[RetrievalQuery]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [RetrievalQuery.model_validate_json(line) for line in lines if line.strip()]


def load_e3(path: Path = E3_PATH) -> list[Burst]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Burst.model_validate_json(line) for line in lines if line.strip()]


def load_monitor_cases(path: Path = E3_MONITORS_PATH) -> list[MonitorCase]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [MonitorCase.model_validate_json(line) for line in lines if line.strip()]


def script_of(text: str) -> Literal["latin", "cyrillic", "mixed", "none"]:
    """Which script a text is written in (letters only)."""
    latin = cyrillic = 0
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        latin += name.startswith("LATIN")
        cyrillic += name.startswith("CYRILLIC")
    if latin and cyrillic:
        return "mixed"
    return "latin" if latin else "cyrillic" if cyrillic else "none"


__all__ = [
    "APOSTROPHES",
    "DATASETS",
    "E1_PATH",
    "E3_MONITORS_PATH",
    "E3_PATH",
    "INTENTS",
    "RETRIEVAL_PATH",
    "TAGS",
    "VARIANTS",
    "Burst",
    "BurstCommitment",
    "BurstLabels",
    "BurstMessage",
    "BurstQuestion",
    "Commitment",
    "Conversation",
    "CoverageGap",
    "Decision",
    "ExpectedCaveat",
    "KeyFact",
    "Labels",
    "Message",
    "MonitorCase",
    "MonitorLabels",
    "MonitorUnderTest",
    "MustNotState",
    "OpenTask",
    "Participant",
    "PromptInjection",
    "QuestionToOwner",
    "RetrievalQuery",
    "fold",
    "load_e1",
    "load_e3",
    "load_monitor_cases",
    "load_queries",
    "script_of",
]
