"""Evaluation targets and the result types they share (S03.4).

A *target* is one thing the runner can evaluate: it takes a dataset, runs the
system under test over it and returns metrics. The runner knows nothing else
about it, so the summary/catch-up target (S03.2) plugs in without touching the
runner, the report writer or the cost guards.

Gate semantics (IMPLEMENTATION_PLAN §10.1): an LLM judge and the deterministic
checks only **pre-screen**. Every metric therefore carries two measurements:
over *all* items (the pre-screen) and over the items the owner has confirmed
(the gate). `gate_status` looks at the confirmed measurement only; with no
confirmed items it is `NOT_EVALUABLE`, never a pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from assistant.llm.port import LLMClient
from evals.review import ReviewLedger

DatasetName = Literal["e1", "retrieval"]


class Status(StrEnum):
    PASS = "pass"  # noqa: S105  (a status word, not a password)
    FAIL = "fail"
    NOT_EVALUABLE = "not evaluable"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Measure:
    """`numerator` out of `denominator` items."""

    numerator: int
    denominator: int

    @property
    def ratio(self) -> float | None:
        return None if self.denominator == 0 else self.numerator / self.denominator


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One metric against its threshold.

    `unit="ratio"`: the goal is on `numerator / denominator` (recall, hit rate).
    `unit="count"`: the goal is on the `numerator` itself (fabrications: zero).
    """

    key: str
    label: str
    unit: Literal["ratio", "count"]
    goal: Literal["at_least", "at_most"]
    threshold: float | None
    #: Over every item: the deterministic or judge pre-screen.
    all_items: Measure
    #: Over the owner-confirmed items only: the gate.
    confirmed: Measure
    #: Items that failed the metric (ids only, never content).
    failed_items: tuple[str, ...] = ()
    #: Judge findings still waiting for the owner's verdict. While any exist
    #: the gate cannot pass.
    pending: int = 0
    note: str = ""

    def _meets(self, measure: Measure) -> bool | None:
        if self.threshold is None or measure.denominator == 0:
            return None  # nothing measured: never a pass
        value = measure.numerator if self.unit == "count" else measure.ratio
        if value is None:
            return None
        return value >= self.threshold if self.goal == "at_least" else value <= self.threshold

    def prescreen_status(self) -> Status:
        meets = self._meets(self.all_items)
        if self.threshold is None:
            return Status.INFO
        if meets is None:
            return Status.NOT_EVALUABLE
        return Status.PASS if meets else Status.FAIL

    def gate_status(self, *, complete: bool = True) -> Status:
        """Pass/fail over owner-confirmed items; never a pass without them."""
        if self.threshold is None:
            return Status.INFO
        meets = self._meets(self.confirmed)
        if meets is False:
            return Status.FAIL  # a confirmed miss stays a miss, complete or not
        if meets is None or self.pending or not complete:
            return Status.NOT_EVALUABLE
        return Status.PASS


@dataclass(frozen=True, slots=True)
class PlannedCall:
    """One model call a target expects to make, for the pre-run estimate."""

    purpose: str
    input_chars: int
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class TargetResult:
    target: str
    dataset: DatasetName
    #: Items evaluated, and items the run planned to evaluate. They differ when
    #: a budget stop ended the run early.
    items: int
    items_planned: int
    metrics: tuple[MetricResult, ...]
    #: One JSON-able record per item (ids and numbers only).
    details: tuple[Mapping[str, Any], ...] = ()
    #: Informational slices, e.g. {"top-5 hit by intent": {"exact": Measure}}.
    breakdowns: Mapping[str, Mapping[str, Measure]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    #: Prompt/rubric versions used, `name.vN`.
    prompts: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.items == self.items_planned


@dataclass(frozen=True, slots=True)
class RunContext:
    """What the runner hands a target."""

    #: The model port (already budget-guarded), or None for model-free runs.
    llm: LLMClient | None = None
    #: The model id for this run (from configuration), or None.
    model: str | None = None
    #: Reasoning effort to request, or None to send none.
    effort: str | None = None
    reviews: ReviewLedger = field(default_factory=ReviewLedger.empty)
    #: Evaluate only the first N items (cheap trial runs).
    limit: int | None = None


class EvalTarget(Protocol):
    """The interface a target implements."""

    name: str
    dataset: DatasetName
    #: The run type whose configured model (`LLM_ROUTE_<ROUTE>_MODEL`) the
    #: target measures, e.g. `owner_request`, `summarize`.
    route: str
    #: True when `run` calls a model: the runner then needs `--live`.
    uses_model: bool

    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        """Model calls this run expects, for the cost estimate. Empty if none."""
        ...

    async def run(self, context: RunContext) -> TargetResult: ...


class TargetNotAvailableError(Exception):
    """A target exists in the interface but cannot run yet."""


class SummaryTarget:
    """PLACEHOLDER for the summary/catch-up evaluation (E1).

    Wired after S03.2 (conversation summaries and the catch-up renderer)
    merges. The metrics it will report already exist in `evals/metrics.py`
    (open-loop recall, caveat presence, owner-facing language) and
    `evals/judge.py` (fabrication pre-screen); what is missing is the thing to
    evaluate: a function that turns one `Conversation`'s messages (never its
    labels) into a `SummaryOutput`. Do not add behaviour here before S03.2.
    """

    name = "summary"
    dataset: DatasetName = "e1"
    route = "summarize"
    uses_model = True

    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        raise TargetNotAvailableError(self._reason())

    async def run(self, context: RunContext) -> TargetResult:
        raise TargetNotAvailableError(self._reason())

    @staticmethod
    def _reason() -> str:
        return "the summary target is wired after S03.2 (summaries and catch-up) merges"
