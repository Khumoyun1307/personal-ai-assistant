"""Running a target and recording the result (S03.4).

`run_target` is the one place a result becomes files: it executes the target,
totals the metered calls and writes the Markdown and JSON reports. Everything
the CLI decides before that (frozen datasets, `--live`, the cost estimate) is
here too, so it is testable without a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from evals import manifest
from evals.budget import CallLedger, CostEstimate, Prices, estimate
from evals.errors import RefusedError
from evals.report import RunMeta, write_report, write_spend_ledger
from evals.targets import EvalTarget, RunContext, TargetResult

__all__ = [
    "RefusedError",
    "RunOutcome",
    "check_estimate",
    "dataset_digests",
    "ensure_frozen",
    "require_live",
    "run_target",
]


def dataset_digests() -> dict[str, str]:
    """SHA-256 of every frozen dataset file, as the manifest lists them."""
    return {
        path.relative_to(manifest.DATASETS).as_posix(): manifest.digest(path)
        for path in manifest.frozen_files()
    }


def ensure_frozen() -> dict[str, str]:
    """Refuse unless every dataset file matches `MANIFEST.sha256`.

    A result is only comparable when the dataset is the frozen one: never edit
    a dataset to make a model pass (evals/README "Freezing").
    """
    problems = manifest.problems()
    if problems:
        raise RefusedError(
            "the datasets do not match evals/datasets/MANIFEST.sha256 ("
            + "; ".join(problems)
            + "). Datasets are frozen: bump the version and regenerate the manifest, "
            "never edit a dataset to change a result."
        )
    return dataset_digests()


def require_live(target: EvalTarget, *, live: bool) -> None:
    """A model run needs the explicit flag; without it nothing is called."""
    if target.uses_model and not live:
        raise RefusedError(
            f"refusing: the '{target.name}' run calls a real model and costs money. "
            "Pass --live only when the owner has asked for this specific run and approved "
            "its cost (AGENTS §8, §9). `--estimate-only` shows the cost without calling anything."
        )


def check_estimate(
    target: EvalTarget,
    context: RunContext,
    *,
    model: str,
    cap_usd: Decimal,
    prices: Prices,
) -> CostEstimate:
    """The worst-case cost of the run; refuses when it exceeds the cap."""
    result = estimate(target.plan(context), model, cap_usd, prices)
    if not result.fits:
        raise RefusedError(
            f"refusing: the worst case of this run is ${result.upper_bound_usd:.4f} for "
            f"{result.calls} calls, over the ${cap_usd} cap. Use --limit for a smaller trial, or "
            "raise --budget-usd with the owner's approval."
        )
    return result


@dataclass(frozen=True, slots=True)
class RunOutcome:
    result: TargetResult
    markdown: Path | None
    machine: Path | None


async def run_target(
    target: EvalTarget,
    context: RunContext,
    *,
    meta: RunMeta,
    ledger: CallLedger,
    out_dir: Path | None,
) -> RunOutcome:
    """Run `target`; write its reports to `out_dir` unless it is None.

    If the run fails midway, the spend so far is still written down (a content-free
    `…-aborted.json`) before the error propagates: money spent is never lost with
    the exception.
    """
    try:
        result = await target.run(context)
    except BaseException as error:
        if out_dir is not None and ledger.entries:
            write_spend_ledger(out_dir, meta, ledger, error_type=type(error).__name__)
        raise
    if out_dir is None:
        return RunOutcome(result, None, None)
    markdown, machine = write_report(out_dir, meta, result, ledger)
    return RunOutcome(result, markdown, machine)
