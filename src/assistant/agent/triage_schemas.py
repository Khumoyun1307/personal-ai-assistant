"""The `triage` run's structured output (ARCHITECTURE §7.5 `TriageResult`;
S05.1) and the deterministic sanitizing that follows it.

Plain on purpose, like the summary schema (M03 D52): every field required,
nullable where optional, no length, format or pattern keywords, so strict
structured outputs accept it; `language` first, so the model commits to the
owner's language before it writes a word (S03.6). The limits live in
`sanitize_triage`: list caps, text trimmed at a word, and dates the model
writes in the owner's local time parsed into UTC — an unparseable value
becomes None, never a guess.

Who is to do what is a structured field (`by`), checked afterwards against
the cited message's author (`agent/triage.py::validate_triage`). A change
here bumps the `triage` prompt version (AGENTS §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

Urgency = Literal["low", "normal", "high", "urgent"]
EscalationCategory = Literal["none", "urgent_decision", "needs_owner", "fyi", "suspicious"]
Script = Literal["latin", "cyrillic", "mixed", "other"]

_LOCAL_TIME = (
    "In the owner's local time as 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' when only the day "
    "is known; null when the messages state none."
)


class TriageCommitment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: Literal["owner", "contact"] = Field(
        description="Who is to do it: owner (YOU) or contact (CONTACT)."
    )
    text: str = Field(
        description=(
            "Only what is to be done, in the owner's language and script; never who "
            "promised it or to whom."
        )
    )
    due: str | None = Field(description=f"When it is due, as stated. {_LOCAL_TIME}")
    source_message_id: int = Field(
        description="The id (from [msg:<id>]) of the message where it was promised."
    )
    existing_task_id: int | None = Field(
        description="The id (from [task:<id>]) of the open task this updates, or null."
    )


class TriageQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        description="Only the question or the decision asked for, in the owner's language."
    )
    source_message_id: int = Field(
        description="The id (from [msg:<id>]) of CONTACT's message that asks it."
    )
    is_decision: bool = Field(description="True when CONTACT waits for the owner's decision.")


class TriageResult(BaseModel):
    """One burst of one conversation, triaged. Never about another chat (§6
    invariant 4)."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(
        description=(
            "The owner's preferred language tag, e.g. 'uz-Latn': every text below is "
            "written in that language and script."
        )
    )
    needs_reply: bool = Field(
        description="True when CONTACT's latest messages call for a reply from the owner."
    )
    contains_question_to_owner: bool = Field(
        description="True when the new messages ask the owner something still unanswered."
    )
    requests_decision: bool = Field(
        description="True when CONTACT waits for the owner to decide something."
    )
    urgency: Urgency = Field(description="How soon the owner needs to see this.")
    deadline: str | None = Field(
        description=f"The earliest deadline the new messages state. {_LOCAL_TIME}"
    )
    commitments: list[TriageCommitment] = Field(
        description="Things either side is to do that no message shows done."
    )
    open_questions: list[TriageQuestion] = Field(
        description="Questions and decisions CONTACT is waiting for, unanswered."
    )
    resolved_task_ids: list[int] = Field(
        description="Ids of listed open tasks a message shown here fulfils or answers."
    )
    escalation_category: EscalationCategory = Field(
        description="What kind of attention the burst needs from the owner."
    )
    reasons: list[str] = Field(
        description="At most three short, factual reasons, in the owner's language."
    )
    script: Script = Field(description="The script CONTACT's new messages are written in.")


#: Deterministic limits applied after validation (S05.1 §3.1).
MAX_ITEMS = 10
MAX_REASONS = 3
MAX_TEXT = 300
MAX_LANGUAGE = 20
#: A due date without a time is due by the end of that local day.
_END_OF_DAY = time(23, 59)
_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")


@dataclass(frozen=True, slots=True)
class CommitmentCandidate:
    by: str
    text: str
    due_at: datetime | None
    source_message_id: int
    existing_task_id: int | None


@dataclass(frozen=True, slots=True)
class QuestionCandidate:
    text: str
    source_message_id: int
    is_decision: bool


@dataclass(frozen=True, slots=True)
class SanitizedTriage:
    """A verdict within limits, times in UTC. Not yet checked against the
    messages and tasks it cites (`validate_triage` does that)."""

    language: str
    needs_reply: bool
    contains_question_to_owner: bool
    requests_decision: bool
    urgency: str
    deadline_at: datetime | None
    commitments: tuple[CommitmentCandidate, ...]
    open_questions: tuple[QuestionCandidate, ...]
    resolved_task_ids: tuple[int, ...]
    escalation_category: str
    reasons: tuple[str, ...]
    script: str
    #: Items and reasons cut by the caps.
    capped: int = 0
    #: Items and reasons dropped because no text was left.
    empty: int = 0


def parse_owner_local(value: str | None, timezone: str) -> datetime | None:
    """An owner-local 'YYYY-MM-DD HH:MM' (or a bare date: the end of that
    day) as a UTC instant; None for anything else — relative words, other
    formats and impossible dates are never guessed at."""
    if value is None:
        return None
    stripped = value.strip()
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        zone = ZoneInfo("UTC")
    for pattern in _FORMATS:
        try:
            local = datetime.strptime(stripped, pattern).replace(tzinfo=zone)
        except ValueError:
            continue
        return local.astimezone(UTC)
    try:
        day = datetime.strptime(stripped, "%Y-%m-%d").replace(tzinfo=zone).date()
    except ValueError:
        return None
    return datetime.combine(day, _END_OF_DAY, tzinfo=zone).astimezone(UTC)


def trim(text: str, limit: int = MAX_TEXT) -> str:
    """One line, at most `limit` characters, cut at a word and marked."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…"


def sanitize_triage(result: TriageResult, *, timezone: str) -> SanitizedTriage:
    """`result` within the limits: lists capped, texts trimmed, empty items
    dropped, dates parsed in the owner's `timezone` (`accounts.timezone`)."""
    capped = empty = 0
    commitments: list[CommitmentCandidate] = []
    for item in result.commitments:
        text = trim(item.text)
        if not text:
            empty += 1
            continue
        commitments.append(
            CommitmentCandidate(
                by=item.by,
                text=text,
                due_at=parse_owner_local(item.due, timezone),
                source_message_id=item.source_message_id,
                existing_task_id=item.existing_task_id,
            )
        )
    questions: list[QuestionCandidate] = []
    for question in result.open_questions:
        text = trim(question.text)
        if not text:
            empty += 1
            continue
        questions.append(QuestionCandidate(text, question.source_message_id, question.is_decision))
    reasons: list[str] = []
    for reason in result.reasons:
        text = trim(reason)
        if not text:
            empty += 1
            continue
        reasons.append(text)
    capped += max(0, len(commitments) - MAX_ITEMS)
    capped += max(0, len(questions) - MAX_ITEMS)
    capped += max(0, len(reasons) - MAX_REASONS)
    return SanitizedTriage(
        language=trim(result.language, MAX_LANGUAGE),
        needs_reply=result.needs_reply,
        contains_question_to_owner=result.contains_question_to_owner,
        requests_decision=result.requests_decision,
        urgency=result.urgency,
        deadline_at=parse_owner_local(result.deadline, timezone),
        commitments=tuple(commitments[:MAX_ITEMS]),
        open_questions=tuple(questions[:MAX_ITEMS]),
        resolved_task_ids=tuple(dict.fromkeys(result.resolved_task_ids)),
        escalation_category=result.escalation_category,
        reasons=tuple(reasons[:MAX_REASONS]),
        script=result.script,
        capped=capped,
        empty=empty,
    )
