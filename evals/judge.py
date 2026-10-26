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

from collections.abc import Collection, Sequence
from dataclasses import dataclass
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

RUBRIC_ID = "fabrication.v1"
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
LOOPS_RUBRIC_ID = "open_loops.v3"
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


class LLMOpenLoopJudge:
    """Pre-screens the open loops string matching could not decide: those with no
    `values`, and (E-s) those whose `values` were not found literally, e.g.
    because the summary translated them.

    It sees the summary and, for each loop, a one-line description with the
    loop's key values and the chat messages the loop cites (never the rest of the
    conversation); it quotes the summary line that surfaces the loop (or nothing)
    before it answers (v3). Its verdicts feed `recall_metric(..., judged=...)`,
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
    ) -> dict[str, LoopVerdict]:
        """`judge` with the evidence the judge quoted."""
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
        return {v.id: v for v in response.parsed.items if v.id in asked}


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
