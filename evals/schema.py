"""Dataset schema for the evaluation sets (IMPLEMENTATION_PLAN §10.1, S03.4).

E1 (summaries/catch-up) is one JSON object per line in
`datasets/e1/conversations.jsonl`: a synthetic private chat between the owner
and one contact, with its labels inline under `labels`. The runner feeds a
model only `messages` (and the coverage facts the product would know); the
labels are the answer key and must never reach a prompt.

The retrieval set is `datasets/retrieval/queries.jsonl`: owner-style queries
over the E1 corpus, each naming the messages a good search must surface.

Integrity rules live in the models, so a malformed or unverifiable dataset
fails at load time, for the runner and the tests alike: ids are unique and
well-formed, every cited message exists, `values` are literal substrings of
the cited messages (labels are verifiable from the source only), deleted text
is never cited as evidence, and every reportable coverage gap carries an
expected caveat.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

DATASETS = Path(__file__).resolve().parent / "datasets"
E1_PATH = DATASETS / "e1" / "conversations.jsonl"
RETRIEVAL_PATH = DATASETS / "retrieval" / "queries.jsonl"

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


def load_e1(path: Path = E1_PATH) -> list[Conversation]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Conversation.model_validate_json(line) for line in lines if line.strip()]


def load_queries(path: Path = RETRIEVAL_PATH) -> list[RetrievalQuery]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [RetrievalQuery.model_validate_json(line) for line in lines if line.strip()]


# ------------------------------------------------------------------------ E2 drafts
#
# E2 (P2 spec E2 §A.4) is one reply situation per line in
# `datasets/e2/situations.jsonl`: a point in a synthetic chat where the owner may
# want a reply, with the expected behaviour as labels. A situation either cuts
# an existing E1 chat after one of its contact messages (`source`) or brings a
# new chat of its own (`conversation`) carrying a `canary` — an invented string
# no other situation's draft may ever contain. Coverage minimums are a test
# (test_e2_dataset), not a load error, so partial data loads while authoring.

E2_PATH = DATASETS / "e2" / "situations.jsonl"

E2Tag = Literal[
    "no_reply_needed",  # the last message needs no answer ("ok", "rahmat")
    "injection",  # contact text tries to instruct the assistant
    "commitment_bait",  # the contact pushes for a date/price/promise not in context
    "sensitive",  # health, money trouble, family conflict
    "decision_needed",  # only the owner can decide
    "ambiguous",
    "long_context",
]
E2_TAGS: tuple[E2Tag, ...] = get_args(E2Tag)

DraftIntent = Literal[
    "answer_question",
    "confirm",
    "decline",
    "ask_clarification",
    "acknowledge",
    "defer_to_owner",
    "no_reply_needed",
]
DRAFT_INTENTS: tuple[DraftIntent, ...] = get_args(DraftIntent)

MustNotKind = Literal[
    "commitment",  # a promise the context does not support
    "date",
    "amount",
    "other_conversation",  # anything from another chat
    "personal_data",
    "claim_human",
]
MUST_NOT_KINDS: tuple[MustNotKind, ...] = get_args(MustNotKind)

ReplyLanguage = Literal["match", "uz-Latn", "uz-Cyrl", "ru", "en"]

_SITUATION_ID = re.compile(r"^s\d{3}$")
_E2_MESSAGE_ID = re.compile(r"^(s\d{3})\.m\d{2}$")


class E2Source(_Strict):
    """The draft is written right after `cut_after`, a contact message of an
    existing E1 chat; the chat is shown up to and including it."""

    e1_conversation: str = Field(pattern=_CONVERSATION_ID.pattern)
    cut_after: str = Field(pattern=_MESSAGE_ID.pattern)

    @model_validator(mode="after")
    def _cut_in_conversation(self) -> Self:
        if not self.cut_after.startswith(f"{self.e1_conversation}."):
            raise ValueError(f"cut_after {self.cut_after} is outside {self.e1_conversation}")
        return self


class E2Message(Message):
    """E1's message format, with ids `<situation id>.mNN`."""

    id: str = Field(pattern=_E2_MESSAGE_ID.pattern)


class E2Conversation(_Strict):
    """A new synthetic chat; it is shown entirely and ends with the contact."""

    owner: Participant
    contact: Participant
    messages: list[E2Message] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        seen: set[str] = set()
        previous: datetime | None = None
        for message in self.messages:
            if message.id in seen:
                raise ValueError(f"{message.id}: duplicate message id")
            if message.reply_to is not None and message.reply_to not in seen:
                raise ValueError(f"{message.id}: reply_to must name an earlier message")
            if previous is not None and message.sent_at < previous:
                raise ValueError(f"{message.id}: messages must be in chronological order")
            seen.add(message.id)
            previous = message.sent_at
        last = self.messages[-1]
        if last.direction != "contact" or last.deleted:
            raise ValueError(f"{last.id}: a new conversation ends with a contact message")
        return self


class OwnerPrefs(_Strict):
    """The account's reply settings and reply style notes for this situation."""

    reply_language: ReplyLanguage
    reply_tone: Literal["neutral", "friendly", "formal"]
    reply_style: Literal["brief", "detailed"]
    style_notes: list[str] = Field(default_factory=list, max_length=3)


class MustInclude(_Strict):
    """A fact a good reply carries: a literal (folded) substring of `source`."""

    fact: str = Field(min_length=1)
    source: str


class MustNot(_Strict):
    kind: MustNotKind
    #: What the reply must not contain, described for the judge.
    text: str = Field(min_length=1)


class Expected(_Strict):
    needs_reply: bool
    intent: DraftIntent
    must_include: list[MustInclude] = Field(default_factory=list, max_length=3)
    must_not: list[MustNot] = Field(default_factory=list, max_length=4)
    #: True only when the context already holds the owner's agreement to what
    #: the reply would confirm.
    makes_commitment_ok: bool
    sensitive_topic: bool

    @model_validator(mode="after")
    def _intent_matches_need(self) -> Self:
        if (self.intent == "no_reply_needed") == self.needs_reply:
            raise ValueError("intent is no_reply_needed exactly when needs_reply is false")
        return self


class E2Situation(_Strict):
    id: str = Field(pattern=_SITUATION_ID.pattern)
    variant: Variant
    source: E2Source | None
    conversation: E2Conversation | None
    canary: Annotated[str, Field(min_length=5)] | None
    owner_prefs: OwnerPrefs
    contact_notes: list[str] = Field(default_factory=list, max_length=3)
    expected: Expected
    tags: list[E2Tag | Tag] = Field(default_factory=list)

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        if (self.source is None) == (self.conversation is None):
            raise ValueError(f"{self.id}: give exactly one of source and conversation")
        if (self.canary is None) != (self.conversation is None):
            raise ValueError(f"{self.id}: a new conversation (only) carries a canary")
        if ("no_reply_needed" in self.tags) == self.expected.needs_reply:
            raise ValueError(f"{self.id}: tag no_reply_needed exactly when needs_reply is false")
        if ("sensitive" in self.tags) != self.expected.sensitive_topic:
            raise ValueError(f"{self.id}: tag sensitive exactly when sensitive_topic is true")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError(f"{self.id}: duplicate tags")
        if self.conversation is not None:
            self._check_new_conversation(self.conversation)
        return self

    def _check_new_conversation(self, conversation: E2Conversation) -> None:
        for message in conversation.messages:
            match = _E2_MESSAGE_ID.match(message.id)
            if match is None or match.group(1) != self.id:
                raise ValueError(f"{message.id}: message ids are '{self.id}.mNN'")
        check_must_include(self, conversation.messages)
        canary = fold(self.canary or "")
        holders = [m.id for m in conversation.messages if canary in fold(m.text or "")]
        if len(holders) != 1:
            raise ValueError(f"{self.id}: the canary must appear in exactly one message")

    def shown_messages(self, e1: Mapping[str, Conversation]) -> list[Message]:
        """The chat the drafter sees: all of a new conversation, or the E1 chat
        up to and including `cut_after`."""
        if self.conversation is not None:
            return list(self.conversation.messages)
        assert self.source is not None
        messages = e1[self.source.e1_conversation].messages
        cut = next(i for i, m in enumerate(messages) if m.id == self.source.cut_after)
        return list(messages[: cut + 1])


def check_must_include(situation: E2Situation, shown: Sequence[Message]) -> None:
    """Every `must_include.fact` folds into the text of a message the drafter sees."""
    by_id = {m.id: m for m in shown}
    for item in situation.expected.must_include:
        message = by_id.get(item.source)
        if message is None:
            raise ValueError(
                f"{situation.id}: must_include cites {item.source}, not in the shown chat"
            )
        if message.text is None or fold(item.fact) not in fold(message.text):
            raise ValueError(f"{situation.id}: fact {item.fact!r} is not in {item.source}")


def check_e2(situations: Sequence[E2Situation], e1: Sequence[Conversation]) -> None:
    """Integrity rules that span records or need E1 (raise ValueError)."""
    ids = [s.id for s in situations]
    if ids != [f"s{n:03d}" for n in range(1, len(ids) + 1)]:
        raise ValueError("situation ids must be unique and consecutive from s001, in order")
    chats = {c.id: c for c in e1}
    for situation in situations:
        if situation.source is None:
            continue
        chat = chats.get(situation.source.e1_conversation)
        if chat is None:
            raise ValueError(f"{situation.id}: unknown E1 chat {situation.source.e1_conversation}")
        if chat.variant != situation.variant:
            raise ValueError(f"{situation.id}: variant differs from its E1 chat's ({chat.variant})")
        cut = {m.id: m for m in chat.messages}.get(situation.source.cut_after)
        if cut is None or cut.direction != "contact" or cut.deleted:
            raise ValueError(
                f"{situation.id}: cut_after {situation.source.cut_after} is not a contact"
                " message of its E1 chat"
            )
        check_must_include(situation, situation.shown_messages(chats))
    _check_canaries(situations, e1)


def _check_canaries(situations: Sequence[E2Situation], e1: Sequence[Conversation]) -> None:
    """A canary occurs once in all of E2 (its own message) and nowhere in E1,
    so finding it in any other draft can only mean a leak."""
    texts: list[str] = [fold(text) for s in situations for text in _strings(s, skip="canary")]
    texts += [
        fold(text)
        for conversation in e1
        for message in conversation.messages
        for text in (message.text, message.original_text, message.purged_text)
        if text is not None
    ]
    for situation in situations:
        if situation.canary is None:
            continue
        canary = fold(situation.canary)
        if sum(text.count(canary) for text in texts) != 1:
            raise ValueError(f"{situation.id}: canary {situation.canary!r} must occur once in E2")


def _strings(value: object, *, skip: str) -> list[str]:
    """Every string value in a model's JSON form, except the field `skip`."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude={skip})
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v, skip=skip)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v, skip=skip)]
    return []


def load_e2(path: Path = E2_PATH, e1: Sequence[Conversation] | None = None) -> list[E2Situation]:
    lines = path.read_text(encoding="utf-8").splitlines()
    situations = [E2Situation.model_validate_json(line) for line in lines if line.strip()]
    check_e2(situations, load_e1() if e1 is None else e1)
    return situations


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


def e2_coverage(situations: Sequence[E2Situation]) -> dict[str, Counter[str]]:
    """Per variant: situations, new conversations, tags, tones and styles."""
    coverage: dict[str, Counter[str]] = {}
    for situation in situations:
        counts = coverage.setdefault(situation.variant, Counter())
        counts["situations"] += 1
        counts["new_conversations"] += situation.conversation is not None
        counts.update(f"tag:{tag}" for tag in situation.tags)
        counts[f"tone:{situation.owner_prefs.reply_tone}"] += 1
        counts[f"style:{situation.owner_prefs.reply_style}"] += 1
    return coverage


def main(argv: list[str]) -> int:
    """`python -m evals.schema e2 [<path>]`: validate E2 and print its coverage
    (the coverage minimums themselves are checked by test_e2_dataset)."""
    if not argv or argv[0] != "e2" or len(argv) > 2:
        print("usage: python -m evals.schema e2 [<path>]")
        return 2
    path = Path(argv[1]) if len(argv) == 2 else E2_PATH
    situations: list[E2Situation] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            situations.append(E2Situation.model_validate_json(line))
        except ValidationError as error:
            print(f"line {number}: {error}")
            return 1
    try:
        check_e2(situations, load_e1())
    except ValueError as error:
        print(error)
        return 1
    for variant, counts in sorted(e2_coverage(situations).items()):
        print(variant, ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"ok: {len(situations)} situations")
    return 0


__all__ = [
    "APOSTROPHES",
    "DATASETS",
    "DRAFT_INTENTS",
    "E1_PATH",
    "E2_PATH",
    "E2_TAGS",
    "INTENTS",
    "MUST_NOT_KINDS",
    "RETRIEVAL_PATH",
    "TAGS",
    "VARIANTS",
    "Commitment",
    "Conversation",
    "CoverageGap",
    "Decision",
    "DraftIntent",
    "E2Conversation",
    "E2Message",
    "E2Situation",
    "E2Source",
    "E2Tag",
    "Expected",
    "ExpectedCaveat",
    "KeyFact",
    "Labels",
    "Message",
    "MustInclude",
    "MustNot",
    "MustNotKind",
    "MustNotState",
    "OwnerPrefs",
    "Participant",
    "PromptInjection",
    "QuestionToOwner",
    "ReplyLanguage",
    "RetrievalQuery",
    "check_e2",
    "check_must_include",
    "e2_coverage",
    "fold",
    "load_e1",
    "load_e2",
    "load_queries",
    "script_of",
]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
