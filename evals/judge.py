"""The fabricated-facts check: an LLM-judge **pre-screen** (IMPLEMENTATION_PLAN §10.1).

Fabrication (any stated fact, date, amount, commitment or attribution the
messages do not support) cannot be decided by string matching: the summary is
in the owner's language, not the chat's. A model reads the chat and the
summary and flags suspicious statements. Its flags are not results:

- every flag starts as *pending owner confirmation*;
- the gate counts only flags the owner confirmed (`evals/review.py`), and stays
  open (`NOT_EVALUABLE`) while any flag is pending or any conversation could
  not be screened;
- a judge failure (refusal, truncation, provider error) marks the conversation
  *unscreened*, never "no fabrication".

The judge goes through the `LLMClient` port like every other model call, so the
budget guard meters it. Tests use `FakeLLM`; nothing here talks to a provider.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from assistant.llm.port import (
    LLMClient,
    LLMError,
    LLMRequest,
    Provenance,
    StopReason,
    TextItem,
)
from evals.assets import load_asset
from evals.metrics import SummaryOutput, open_loops
from evals.render import RENDER_VERSION, render_conversation
from evals.review import FindingStatus, ReviewLedger, finding_id
from evals.schema import Commitment, Conversation, QuestionToOwner
from evals.targets import Measure, MetricResult

#: v2 (S03.7 A2): "you" in the summary is the owner (3 of the 11 over-flags of the
#: summarize.v5 run were correct "You promised ..." lines); an edited message's
#: text is its current one. v1 stays: earlier reports name it.
RUBRIC_ID = "fabrication.v2"
#: The version of the summary text the judges read (`evals.summary.labelled_text`).
#: v1: "promised by the owner/contact" from the summary's fields; v2 (S03.7): who
#: asked, promised or agreed in the product's own English wording
#: (`agent/attribution.py`), with the contact's name.
LABELS_VERSION = "labels.v2"
#: The fabrication judge as a report names it: the rubric plus the version of the
#: conversation rendering it reads (`evals.render.RENDER_VERSION`), so runs whose
#: judge read UTC times are told apart from runs whose judge read local ones, plus
#: the version of the summary text it reads (`LABELS_VERSION`).
FABRICATION_LABEL = f"{RUBRIC_ID}+{RENDER_VERSION}+{LABELS_VERSION}"
#: v4 (S03.7 A2): "you" in the summary is the owner; a quote that is not in the
#: summary is no evidence (and `evidence_in_summary` checks it in code).
LOOPS_RUBRIC_ID = "open_loops.v4"
MAX_OUTPUT_TOKENS = 3000
TIMEOUT_SECONDS = 120.0
#: Fabrication threshold (S03.4): none.
FABRICATION_THRESHOLD = 0

FlagReason = Literal[
    "unsupported",
    "contradicts_source",
    "superseded_value",
    "deleted_content",
    "voice_no_transcript",
    "injection_followed",
    "outside_coverage",
    "wrong_attribution",
    "false_gap_claim",
]


class Flag(BaseModel):
    """One judge flag (the structured output)."""

    model_config = ConfigDict(extra="forbid")

    statement: str
    reason: FlagReason
    evidence_message_ids: list[str]


class FlagList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flags: list[Flag]


@dataclass(frozen=True, slots=True)
class FabricationFinding:
    finding_id: str
    conversation_id: str
    statement: str
    reason: FlagReason
    evidence_message_ids: tuple[str, ...]
    status: FindingStatus = "pending_owner"


@dataclass(frozen=True, slots=True)
class ScreenResult:
    conversation_id: str
    findings: tuple[FabricationFinding, ...]
    #: False when the judge could not screen this conversation.
    screened: bool = True
    error: str | None = None


class FabricationJudge(Protocol):
    """The interface the summary target uses; implementations may be a model
    (`LLMFabricationJudge`) or a hand-built stub in tests."""

    async def screen(self, conversation: Conversation, output: SummaryOutput) -> ScreenResult: ...


class LLMFabricationJudge:
    """Screens a summary with a model through the `LLMClient` port."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        model: str,
        ledger: ReviewLedger,
        effort: str | None = None,
        account_id: int = 1,
    ) -> None:
        self._llm = llm
        self._model = model
        self._ledger = ledger
        self._effort = effort
        self._account_id = account_id
        self._rubric = load_asset("rubrics", RUBRIC_ID)

    async def screen(self, conversation: Conversation, output: SummaryOutput) -> ScreenResult:
        request = LLMRequest(
            account_id=self._account_id,
            purpose="eval_fabrication_screen",
            model=self._model,
            instructions=self._rubric.text,
            items=(
                TextItem(
                    "user",
                    _judge_input(conversation, output),
                    # Synthetic eval data, but it is contact-authored chat text
                    # by construction, so it is labelled like the real thing.
                    Provenance.MANAGED,
                ),
            ),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            timeout_seconds=TIMEOUT_SECONDS,
            effort=self._effort,
        )
        try:
            response = await self._llm.parse(request, FlagList)
        except LLMError as error:
            return ScreenResult(conversation.id, (), screened=False, error=error.kind.value)
        if response.stop_reason is not StopReason.COMPLETED or response.parsed is None:
            return ScreenResult(
                conversation.id, (), screened=False, error=response.stop_reason.value
            )
        known = {message.id for message in conversation.messages}
        findings: list[FabricationFinding] = []
        for flag in response.parsed.flags:
            fid = finding_id(conversation.id, flag.statement)
            findings.append(
                FabricationFinding(
                    finding_id=fid,
                    conversation_id=conversation.id,
                    statement=flag.statement,
                    reason=flag.reason,
                    evidence_message_ids=tuple(m for m in flag.evidence_message_ids if m in known),
                    status=self._ledger.finding_status(fid),
                )
            )
        return ScreenResult(conversation.id, tuple(findings))


class LoopVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    #: The summary line(s) that tell the owner about the item, copied, or "" (v3):
    #: written before the verdict, and kept in the report for the owner's review.
    evidence: str
    surfaced: bool


class LoopVerdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LoopVerdict]


@dataclass(frozen=True, slots=True)
class CheckedLoopVerdict:
    """A loop judge's verdict after its quote was checked against the summary."""

    #: What the judge quoted, as it wrote it (kept for the owner's review).
    evidence: str
    #: The pre-screen verdict: the judge said surfaced AND its quote is in the summary.
    surfaced: bool
    #: What the judge said.
    judge_surfaced: bool
    #: Whether every quoted line occurs in the summary (exactly, or as a near quote).
    quoted: bool
    #: Whether a quoted line counted only as a near quote (`QuoteMatch.NEAR`): listed
    #: for the owner apart from exact quotes.
    near: bool = False


#: Apostrophe forms a quote may swap (the Uzbek o‘/g‘ is written several ways).
_APOSTROPHES = str.maketrans(dict.fromkeys("‘’ʻʼ`´", "'"))  # noqa: RUF001
#: Where a quote may join pieces of the summary: lines, an ellipsis, a slash or bar.
_QUOTE_BREAKS = re.compile(r"\n|\.\.\.|…| / | \| ")
#: Bullets, quote marks and spaces a quote may add around a piece.
_QUOTE_EDGES = " \t-•*\"'“”«»"


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFC", text).translate(_APOSTROPHES).casefold()
    return " ".join(folded.split())


class QuoteMatch(StrEnum):
    """How a loop judge's quote matches the summary it judged."""

    #: Every piece occurs in the summary.
    EXACT = "exact"
    #: Every piece occurs or is a near copy of one summary line (`NEAR_QUOTE_RATIO`).
    NEAR = "near"
    #: Empty, or a piece is in no summary line: no evidence.
    NONE = "none"


#: How similar a quoted line must be to a summary line to count as a copy of it
#: (S03.7 round 3, E-af). In the summarize.v6 run the judge copied four lines with a
#: slip (a Latin letter swapped for a Cyrillic one, a dropped "(not agreed yet)"):
#: 0.925-0.973 similar; the line it invented in the v5 run is 0.55 similar to its
#: closest one.
NEAR_QUOTE_RATIO = 0.90
_NUMBERS = re.compile(r"\d+")


def _near_line(piece: str, lines: Sequence[str]) -> bool:
    """A piece is a near copy of a line: at least `NEAR_QUOTE_RATIO` similar
    (difflib, on the folded text) and with exactly the line's numbers, so a copy
    with another time or amount never counts."""
    numbers = _NUMBERS.findall(piece)
    return any(
        _NUMBERS.findall(line) == numbers
        and SequenceMatcher(None, piece, line, autojunk=False).ratio() >= NEAR_QUOTE_RATIO
        for line in lines
    )


def quote_match(evidence: str, summary: str) -> QuoteMatch:
    """How a loop judge's quote matches the summary it judged (S03.7 A2: in the
    summarize.v5 run it once quoted a line the summary does not have). Every piece
    of the quote — split at line breaks and ellipses — must occur in the summary,
    ignoring case, spacing, bullets and apostrophe forms (`EXACT`), or be a near
    copy of one of its lines (`NEAR`, round 3). An empty quote is none."""
    haystack = _fold(summary)
    pieces = [_fold(piece).strip(_QUOTE_EDGES) for piece in _QUOTE_BREAKS.split(evidence)]
    pieces = [piece for piece in pieces if piece]
    if not pieces:
        return QuoteMatch.NONE
    missing = [piece for piece in pieces if piece not in haystack]
    if not missing:
        return QuoteMatch.EXACT
    lines = [_fold(line).strip(_QUOTE_EDGES) for line in summary.splitlines()]
    lines = [line for line in lines if line]
    return QuoteMatch.NEAR if all(_near_line(p, lines) for p in missing) else QuoteMatch.NONE


def evidence_in_summary(evidence: str, summary: str) -> bool:
    """Whether a loop judge's quote is in the summary, exactly or as a near copy."""
    return quote_match(evidence, summary) is not QuoteMatch.NONE


class LLMOpenLoopJudge:
    """Pre-screens the open loops string matching could not decide: those with no
    `values`, and (E-s) those whose `values` were not found literally, e.g.
    because the summary translated them.

    It sees the summary and, for each loop, a one-line description with the
    loop's key values and the chat messages the loop cites (never the rest of the
    conversation); it quotes the summary line that surfaces the loop (or nothing)
    before it answers (v3), and a "surfaced" whose quote is not in the summary
    counts as not surfaced (S03.7 A2; a near copy of a summary line counts, round 3,
    and is marked `near`). Its verdicts feed `recall_metric(..., judged=...)`,
    which counts them in the pre-screen only: the gate needs the owner's verdict
    (`ReviewLedger.coverage`). A judge failure returns no verdicts, so those loops
    stay *missed*.
    """

    def __init__(
        self, llm: LLMClient, *, model: str, effort: str | None = None, account_id: int = 1
    ) -> None:
        self._llm = llm
        self._model = model
        self._effort = effort
        self._account_id = account_id
        self._rubric = load_asset("rubrics", LOOPS_RUBRIC_ID)

    async def judge(
        self,
        conversation: Conversation,
        output: SummaryOutput,
        loop_ids: Collection[str] | None = None,
    ) -> dict[str, bool]:
        """Verdicts on the open loops named by `loop_ids` (default: the loops with
        no `values`); no call when there are none."""
        verdicts = await self.verdicts(conversation, output, loop_ids)
        return {loop: verdict.surfaced for loop, verdict in verdicts.items()}

    async def verdicts(
        self,
        conversation: Conversation,
        output: SummaryOutput,
        loop_ids: Collection[str] | None = None,
    ) -> dict[str, CheckedLoopVerdict]:
        """`judge` with the evidence the judge quoted. A verdict whose quote is not
        in the summary (`quote_match`, an empty quote included) is not surfaced: a
        quote that does not occur is no evidence (S03.7 A2). A near copy of a summary
        line counts and is marked `near` (round 3)."""
        pending = [
            loop
            for loop in open_loops(conversation)
            if (loop.id in loop_ids if loop_ids is not None else not loop.values)
        ]
        if not pending:
            return {}
        lines = [_item_block(conversation, loop) for loop in pending]
        request = LLMRequest(
            account_id=self._account_id,
            purpose="eval_open_loop_screen",
            model=self._model,
            instructions=self._rubric.text,
            items=(
                TextItem(
                    "user",
                    "Open items:\n" + "\n".join(lines) + f"\n\nSummary:\n{output.text}",
                    Provenance.MANAGED,
                ),
            ),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            timeout_seconds=TIMEOUT_SECONDS,
            effort=self._effort,
        )
        try:
            response = await self._llm.parse(request, LoopVerdicts)
        except LLMError:
            return {}
        if response.stop_reason is not StopReason.COMPLETED or response.parsed is None:
            return {}
        asked = {loop.id for loop in pending}
        checked: dict[str, CheckedLoopVerdict] = {}
        for verdict in response.parsed.items:
            if verdict.id not in asked:
                continue
            match = quote_match(verdict.evidence, output.text)
            quoted = match is not QuoteMatch.NONE
            checked[verdict.id] = CheckedLoopVerdict(
                evidence=verdict.evidence,
                surfaced=verdict.surfaced and quoted,
                judge_surfaced=verdict.surfaced,
                quoted=quoted,
                near=match is QuoteMatch.NEAR,
            )
        return checked


def _item_block(conversation: Conversation, loop: QuestionToOwner | Commitment) -> str:
    """One open item for the loop judge: its description and key values, then the
    chat messages the item cites (v3: the judge can recognise a translation of
    them in the summary). Only those messages, never the rest of the chat."""
    by_id = {message.id: message for message in conversation.messages}
    cited = [by_id[i] for i in loop.source_message_ids if i in by_id]
    quoted = [f"    [{m.direction}] {m.text}" for m in cited if m.text and not m.deleted]
    return "\n".join([f"- {loop.id}: {_describe(loop)}", *quoted])


def _describe(loop: QuestionToOwner | Commitment) -> str:
    values = f" [key values: {'; '.join(loop.values)}]" if loop.values else ""
    if isinstance(loop, QuestionToOwner):
        return f"the contact asked the owner: {loop.summary}{values}"
    due = f" (due {loop.when})" if loop.when else ""
    return f"commitment by {loop.who_name} ({loop.who}): {loop.what}{due}{values}"


def _judge_input(conversation: Conversation, output: SummaryOutput) -> str:
    traps = [trap.statement for trap in conversation.labels.must_not_state]
    trap_block = "\n".join(f"- {trap}" for trap in traps) or "- (none)"
    return (
        f"{render_conversation(conversation)}\n\n"
        f"Known traps (statements the summary must not make):\n{trap_block}\n\n"
        f"Summary to check (written for the owner, language {output.owner_language}):\n"
        f"{output.text}"
    )


def screen_input_chars(conversation: Conversation, summary_chars: int) -> int:
    """Characters of the fabrication judge's request for a summary of
    `summary_chars` characters (rubric and input), for the pre-run estimate."""
    empty = SummaryOutput(conversation.id, "", "en")
    return (
        len(load_asset("rubrics", RUBRIC_ID).text)
        + len(_judge_input(conversation, empty))
        + (summary_chars)
    )


def loops_input_chars(conversation: Conversation, summary_chars: int) -> int | None:
    """The same for the open-loop judge, at its worst: every open loop asked about
    (any loop whose `values` are not found literally goes to the judge, E-s);
    None when the conversation has no open loop, so the judge is never called."""
    pending = open_loops(conversation)
    if not pending:
        return None
    lines = "\n".join(_item_block(conversation, loop) for loop in pending)
    return (
        len(load_asset("rubrics", LOOPS_RUBRIC_ID).text)
        + len("Open items:\n" + lines + "\n\nSummary:\n")
        + summary_chars
    )


def fabrication_metric(
    results: Sequence[ScreenResult], ledger: ReviewLedger, set_size: int | None = None
) -> MetricResult:
    """Zero fabricated facts, over the owner's verdicts.

    Pre-screen: every flag not dismissed, over the screened conversations.
    Gate: owner-confirmed flags, over the owner-confirmed conversations (all of
    them, E-k); open while a flag is pending or a conversation is unscreened.

    **A judge negative decides the item.** The judge can only pre-screen what it
    sees: a conversation it did not flag counts as clean. The owner overrides
    that, per conversation, with `fabrications` in the review ledger (an owner
    fabrication counts as confirmed). Whether to spot-check judge-negative
    summaries, and how many, is the owner's call (E-c).
    """
    screened = [r for r in results if r.screened]
    live = [f for r in screened for f in r.findings if f.status != "dismissed"]
    confirmed_findings = [f for f in live if f.status == "confirmed"]
    ids = {r.conversation_id for r in results}
    owner_found = sorted(ledger.owner_fabrications & ids)
    # A conversation holding a finding the owner confirmed (or recorded) counts
    # as confirmed for the gate: the owner has looked at it, and it must fail.
    flagged_by_owner = {f.conversation_id for f in confirmed_findings} | set(owner_found)
    confirmed_conversations = {
        r.conversation_id
        for r in results
        if (r.screened or r.conversation_id in flagged_by_owner)
        and (ledger.is_confirmed(r.conversation_id) or r.conversation_id in flagged_by_owner)
    }
    pending = sum(f.status == "pending_owner" for f in live)
    unscreened = [r.conversation_id for r in results if not r.screened]
    notes = []
    if owner_found:
        notes.append("fabrication recorded by the owner in: " + ", ".join(owner_found))
    if pending:
        notes.append(f"{pending} flagged statement(s) pending owner confirmation")
    if unscreened:
        notes.append("not screened (judge failed): " + ", ".join(unscreened))
    return MetricResult(
        key="fabricated_facts",
        label="Fabricated facts (zero allowed)",
        unit="count",
        goal="at_most",
        threshold=FABRICATION_THRESHOLD,
        all_items=Measure(len(live) + len(owner_found), len(screened)),
        confirmed=Measure(len(confirmed_findings) + len(owner_found), len(confirmed_conversations)),
        failed_items=(
            *(f.finding_id for f in live),
            *(f"{conversation}#owner" for conversation in owner_found),
        ),
        pending=pending + len(unscreened),
        note="; ".join(notes),
        set_size=len(results) if set_size is None else set_size,
    )
