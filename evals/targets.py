"""Evaluation targets and the result types they share (S03.4).

A *target* is one thing the runner can evaluate: it takes a dataset, runs the
system under test over it and returns metrics. The runner knows nothing else
about it, so the retrieval target (`evals/retrieval.py`) and the summary target
(`evals/summary.py`) share the runner, the report writer and the cost guards.

Gate semantics (IMPLEMENTATION_PLAN §10.1): an LLM judge and the deterministic
checks only **pre-screen**. Every metric therefore carries two measurements:
over *all* items (the pre-screen) and over the items the owner has confirmed
(the gate). `gate_status` looks at the confirmed measurement only, and only
passes when *every* item of the set is owner-confirmed; with fewer it is
`NOT_EVALUABLE` (unless confirmed misses already make the threshold
unreachable), never a pass. A trial or stopped run measures part of the set, so
it can never pass either.
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
    #: How many items the whole set has for this metric (a full run's
    #: denominator). None means "the items measured". A trial or stopped run
    #: measures fewer than the set, so its gate can never pass.
    set_size: int | None = None
    #: False when the threshold is only a comparison for the pre-screen (the
    #: retrieval 85 % is defined with model-generated variants; a model-free
    #: run reports it but has no gate).
    gated: bool = True

    def _meets(self, measure: Measure) -> bool | None:
        if self.threshold is None or measure.denominator == 0:
            return None  # nothing measured: never a pass
        value = measure.numerator if self.unit == "count" else measure.ratio
        if value is None:
            return None
        return value >= self.threshold if self.goal == "at_least" else value <= self.threshold

    def confirmation(self) -> tuple[int, int]:
        """`(k, N)`: items the owner confirmed, of the items in the set."""
        total = self.set_size if self.set_size is not None else self.all_items.denominator
        return self.confirmed.denominator, total

    def prescreen_status(self) -> Status:
        meets = self._meets(self.all_items)
        if self.threshold is None:
            return Status.INFO
        if meets is None:
            return Status.NOT_EVALUABLE
        return Status.PASS if meets else Status.FAIL

    def _cannot_pass(self) -> bool:
        """True when owner-confirmed misses alone already sink the threshold,
        however the unreviewed rest turns out."""
        if self.threshold is None:
            return False
        if self.unit == "count":
            return self.goal == "at_most" and self.confirmed.numerator > self.threshold
        _, total = self.confirmation()
        if self.goal != "at_least" or total == 0:
            return False
        confirmed_misses = self.confirmed.denominator - self.confirmed.numerator
        return (total - confirmed_misses) / total < self.threshold

    def gate_status(self) -> Status:
        """The gate: pass/fail only when **every item of the set** is
        owner-confirmed (E-k); a definite miss fails earlier; otherwise not
        evaluable. A model-free comparison (`gated=False`) is informational."""
        if self.threshold is None or not self.gated:
            return Status.INFO
        if self._cannot_pass():
            return Status.FAIL
        confirmed, total = self.confirmation()
        if self.pending or total == 0 or confirmed < total:
            return Status.NOT_EVALUABLE
        return Status.PASS if self._meets(self.confirmed) else Status.FAIL


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
    #: Items evaluated, items the run planned to evaluate (fewer than the set
    #: for a `--limit` trial), and items in the whole set. `items` falls short
    #: of `items_planned` when a budget stop ended the run early.
    items: int
    items_planned: int
    items_total: int
    metrics: tuple[MetricResult, ...]
    #: One JSON-able record per item (ids and numbers only).
    details: tuple[Mapping[str, Any], ...] = ()
    #: Informational slices, e.g. {"top-5 hit by intent": {"exact": Measure}}.
    breakdowns: Mapping[str, Mapping[str, Measure]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    #: Prompt/rubric versions used, `name.vN`.
    prompts: tuple[str, ...] = ()

    @property
    def trial(self) -> bool:
        """A `--limit` or `--only` run over part of the set: never a gate."""
        return self.items_planned < self.items_total

    @property
    def stopped(self) -> bool:
        """The run ended before its planned items (a budget stop): a failure."""
        return self.items < self.items_planned

    @property
    def complete(self) -> bool:
        return not self.trial and not self.stopped


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
    #: Evaluate only the first N items (N >= 1): a trial run, never a gate.
    limit: int | None = None
    #: Evaluate only the items with these ids, in set order (the summary target,
    #: `--only`): a trial run, never a gate.
    only: tuple[str, ...] | None = None


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
