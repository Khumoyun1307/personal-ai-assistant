"""`python -m evals`: verify datasets, run a target, compare runs.

    python -m evals verify
    python -m evals run retrieval [--mode deterministic|variants|both] [--live] ...
    python -m evals run summary            # placeholder until S03.2 merges
    python -m evals compare docs/evals/<run>.json [...]

Safe by default: `run retrieval` is the deterministic, model-free baseline.
Any mode that calls a model refuses without `--live`, refuses when the
worst-case cost exceeds the cap, and stops mid-run before overspending. See
`evals/README.md` "Running". Exit codes: 0 done, 2 refused or misconfigured,
3 target not available yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from assistant.core.clock import SystemClock
from evals.budget import (
    DEFAULT_BUDGET_USD,
    BudgetedLLMClient,
    BudgetError,
    CallLedger,
    Prices,
    parse_price,
)
from evals.corpus import eval_database_url, isolated_corpus
from evals.live import LiveSetupError, ModelChoice, build_client, load_live_config, resolve_model
from evals.report import RunMeta, fmt_measure, render_comparison
from evals.retrieval import RetrievalMode, RetrievalTarget
from evals.review import ReviewLedger
from evals.runner import RefusedError, check_estimate, ensure_frozen, require_live, run_target
from evals.schema import load_e1, load_queries
from evals.targets import RunContext, SummaryTarget, TargetNotAvailableError

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / "docs" / "evals"
REVIEWS_DIR = Path(__file__).resolve().parent / "reviews"
EXIT_REFUSED = 2
EXIT_NOT_AVAILABLE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="check the datasets against the frozen manifest")

    run = sub.add_parser("run", help="run a target and record the result")
    run.add_argument("target", choices=["retrieval", "summary"])
    run.add_argument(
        "--mode",
        choices=[m.value for m in RetrievalMode] + ["both"],
        default=RetrievalMode.DETERMINISTIC.value,
        help="retrieval only: deterministic (no model, default), variants (a model), both",
    )
    run.add_argument(
        "--live",
        action="store_true",
        help="allow calling a real model (the owner's go-ahead and cost approval are needed)",
    )
    run.add_argument("--model", help="model id override (default: the route's configured model)")
    run.add_argument(
        "--budget-usd",
        default=str(DEFAULT_BUDGET_USD),
        help=f"spending cap for the run in USD (default {DEFAULT_BUDGET_USD})",
    )
    run.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="MODEL=IN:OUT",
        help="USD per 1M tokens for a model the price table lacks (repeatable)",
    )
    run.add_argument("--limit", type=int, help="evaluate only the first N items")
    run.add_argument("--estimate-only", action="store_true", help="print the cost and stop")
    run.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="where to write reports")
    run.add_argument("--no-write", action="store_true", help="do not write report files")
    run.add_argument("--reviews", type=Path, help="owner review ledger (JSON)")
    run.add_argument("--date", type=date.fromisoformat, help="run date (default: today, UTC)")

    compare = sub.add_parser("compare", help="compare recorded runs (quality and cost)")
    compare.add_argument("reports", nargs="+", type=Path, help="JSON reports from docs/evals/")
    return parser


def _say(message: str) -> None:
    print(message, flush=True)


def _refuse(message: str, code: int = EXIT_REFUSED) -> int:
    print(message, file=sys.stderr, flush=True)
    return code


def _reviews(path: Path | None, dataset: str) -> ReviewLedger:
    if path is not None:
        return ReviewLedger.load(path)
    default = REVIEWS_DIR / f"{dataset}.json"
    return ReviewLedger.load(default) if default.exists() else ReviewLedger.empty()


def _cap(value: str) -> Decimal:
    try:
        cap = Decimal(value)
    except InvalidOperation:
        raise RefusedError(f"--budget-usd expects a number, got '{value}'") from None
    if cap <= 0:
        raise RefusedError("--budget-usd must be positive")
    return cap


def _prices(specs: Sequence[str]) -> Prices:
    try:
        return dict(parse_price(spec) for spec in specs)
    except ValueError as error:
        raise RefusedError(str(error)) from None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            return _verify()
        if args.command == "compare":
            return _compare(args.reports)
        if args.target == "summary":
            return _summary(args)
        return asyncio.run(_retrieval(args))
    except (RefusedError, LiveSetupError, BudgetError, RuntimeError) as error:
        return _refuse(str(error))
    except TargetNotAvailableError as error:
        return _refuse(str(error), EXIT_NOT_AVAILABLE)


def _verify() -> int:
    digests = ensure_frozen()
    _say("datasets match evals/datasets/MANIFEST.sha256:")
    for name, digest in digests.items():
        _say(f"  {digest}  {name}")
    return 0


def _compare(paths: Sequence[Path]) -> int:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    sys.stdout.write(render_comparison(reports))
    return 0


def _summary(args: argparse.Namespace) -> int:
    ensure_frozen()
    SummaryTarget().plan(RunContext(limit=args.limit))  # raises: not available yet
    return 0  # pragma: no cover


async def _retrieval(args: argparse.Namespace) -> int:
    digests = ensure_frozen()
    cap, prices = _cap(args.budget_usd), _prices(args.price)
    reviews = _reviews(args.reviews, "retrieval")
    modes = (
        [RetrievalMode.DETERMINISTIC, RetrievalMode.VARIANTS]
        if args.mode == "both"
        else [RetrievalMode(args.mode)]
    )
    queries = load_queries()
    targets = [RetrievalTarget(queries, mode=mode) for mode in modes]
    choices = _guard(targets, args, cap, prices, reviews)
    if args.estimate_only:
        return 0
    live_client = None
    if any(t.uses_model for t in targets):
        live_client = build_client(load_live_config(targets[-1].route, args.model))
    run_date = args.date or SystemClock().now().date()
    out_dir = None if args.no_write else args.out_dir
    async with isolated_corpus(eval_database_url(), load_e1()) as corpus:
        for target, choice in zip(targets, choices, strict=True):
            ledger = CallLedger()
            if choice is not None and live_client is not None:
                llm = BudgetedLLMClient(live_client, cap_usd=cap, overrides=prices, ledger=ledger)
                context = RunContext(
                    llm=llm,
                    model=choice.model,
                    effort=choice.effort,
                    reviews=reviews,
                    limit=args.limit,
                )
            else:
                context = RunContext(reviews=reviews, limit=args.limit)
            meta = RunMeta(
                run_date=run_date,
                set_name=target.name,
                mode=target.mode.value,
                model=None if choice is None else choice.model,
                live=choice is not None,
                effort=None if choice is None else choice.effort,
                dataset_files=digests,
                cap_usd=None if choice is None else cap,
                reviews=reviews,
            )
            outcome = await run_target(
                target.bind(corpus), context, meta=meta, ledger=ledger, out_dir=out_dir
            )
            _say(f"retrieval ({target.mode.value}): {outcome.result.items} items")
            for metric in outcome.result.metrics:
                status = metric.gate_status(complete=outcome.result.complete)
                _say(
                    f"  {metric.label}: {fmt_measure(metric.all_items, unit=metric.unit)} "
                    f"pre-screen; gate {status.value}"
                )
            for note in outcome.result.notes:
                _say(f"  note: {note}")
            if outcome.markdown is not None:
                _say(f"  report: {outcome.markdown}")
            if choice is not None:
                _say(f"  spent ${ledger.spent:.4f} of ${cap}")
    return 0


def _guard(
    targets: Sequence[RetrievalTarget],
    args: argparse.Namespace,
    cap: Decimal,
    prices: Prices,
    reviews: ReviewLedger,
) -> list[ModelChoice | None]:
    """Everything that can refuse a run, before a database or a model is touched."""
    choices: list[ModelChoice | None] = []
    for target in targets:
        if not target.uses_model:
            choices.append(None)
            continue
        if not args.estimate_only:
            require_live(target, live=args.live)
        choice = resolve_model(target.route, args.model)
        context = RunContext(model=choice.model, reviews=reviews, limit=args.limit)
        result = check_estimate(target, context, model=choice.model, cap_usd=cap, prices=prices)
        _say(
            f"{target.name} ({target.mode.value}): model {choice.model}, {result.calls} calls, "
            f"worst case ${result.upper_bound_usd:.4f}, cap ${cap}"
        )
        choices.append(choice)
    return choices
