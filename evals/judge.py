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

from collections.abc import Sequence
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
from evals.render import render_conversation
from evals.review import FindingStatus, ReviewLedger, finding_id
from evals.schema import Commitment, Conversation, QuestionToOwner
from evals.targets import Measure, MetricResult

RUBRIC_ID = "fabrication.v1"
LOOPS_RUBRIC_ID = "open_loops.v1"
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
    surfaced: bool


class LoopVerdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LoopVerdict]


class LLMOpenLoopJudge:
    """Pre-screens open loops that have no `values` to match by string.

    It sees only the summary and a one-line description of each loop (never the
    conversation), and answers whether the summary tells the owner about it.
    Its verdicts feed `recall_metric(..., judged=...)`, which counts them in the
    pre-screen only: the gate needs the owner's verdict (`ReviewLedger.coverage`).
    A judge failure returns no verdicts, so those loops stay *missed*.
    """

    def __init__(
        self, llm: LLMClient, *, model: str, effort: str | None = None, account_id: int = 1
    ) -> None:
        self._llm = llm
        self._model = model
        self._effort = effort
        self._account_id = account_id
        self._rubric = load_asset("rubrics", LOOPS_RUBRIC_ID)

    async def judge(self, conversation: Conversation, output: SummaryOutput) -> dict[str, bool]:
        pending = [loop for loop in open_loops(conversation) if not loop.values]
        if not pending:
            return {}
        lines = [f"- {loop.id}: {_describe(loop)}" for loop in pending]
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
        return {v.id: v.surfaced for v in response.parsed.items if v.id in asked}


def _describe(loop: QuestionToOwner | Commitment) -> str:
    if isinstance(loop, QuestionToOwner):
        return f"the contact asked the owner: {loop.summary}"
    due = f" (due {loop.when})" if loop.when else ""
    return f"commitment by {loop.who_name} ({loop.who}): {loop.what}{due}"


def _judge_input(conversation: Conversation, output: SummaryOutput) -> str:
    traps = [trap.statement for trap in conversation.labels.must_not_state]
    trap_block = "\n".join(f"- {trap}" for trap in traps) or "- (none)"
    return (
        f"{render_conversation(conversation)}\n\n"
        f"Known traps (statements the summary must not make):\n{trap_block}\n\n"
        f"Summary to check (written for the owner, language {output.owner_language}):\n"
        f"{output.text}"
    )


def fabrication_metric(results: Sequence[ScreenResult], ledger: ReviewLedger) -> MetricResult:
    """Zero fabricated facts, over the owner's verdicts.

    Pre-screen: every flag not dismissed, over the screened conversations.
    Gate: owner-confirmed flags, over the owner-confirmed conversations; open
    while a flag is pending or a conversation is unscreened.
    """
    screened = [r for r in results if r.screened]
    live = [f for r in screened for f in r.findings if f.status != "dismissed"]
    confirmed_findings = [f for f in live if f.status == "confirmed"]
    # A conversation holding a finding the owner confirmed counts as confirmed
    # for the gate: the owner has looked at it, and it must fail the gate.
    flagged_by_owner = {f.conversation_id for f in confirmed_findings}
    confirmed_conversations = [
        r
        for r in screened
        if ledger.is_confirmed(r.conversation_id) or r.conversation_id in flagged_by_owner
    ]
    pending = sum(f.status == "pending_owner" for f in live)
    unscreened = [r.conversation_id for r in results if not r.screened]
    notes = []
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
        all_items=Measure(len(live), len(screened)),
        confirmed=Measure(len(confirmed_findings), len(confirmed_conversations)),
        failed_items=tuple(f.finding_id for f in live),
        pending=pending + len(unscreened),
        note="; ".join(notes),
    )
