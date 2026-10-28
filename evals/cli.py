"""`python -m evals`: verify datasets, run a target, compare runs.

    python -m evals verify
    python -m evals run retrieval [--mode deterministic|variants|both] [--live] ...
    python -m evals run retrieval --mode replay --replay-from docs/evals/<variants run>.json
    python -m evals run retrieval --mode agent --live --limit 1   # a trial of the real agent loop
    python -m evals run retrieval --mode agent --live --calibrate-from docs/evals/<agent trial>.json
    python -m evals run summary [--owner-language uz-Latn|uz-Cyrl|ru|en] [--live] ...
    python -m evals run summary --live --only c36,c28,c15   # a trial of chosen chats
    python -m evals run triage [--live] [--model ID] ...      # the product's triage over E3
    python -m evals compare docs/evals/<run>.json [...]
    python -m evals compare <route model>.json <next tier>.json [--route-model ID]  # triage choice
    python -m evals cleanup [--yes]        # list (or drop) throwaway databases a killed run left

Safe by default: `run retrieval` is the deterministic, model-free baseline, and
`run summary` and `run triage` (which always call a model) refuse without `--live`. Any run
that calls a model refuses without `--live`, refuses when the worst-case cost
exceeds the cap, and stops mid-run before overspending. See `evals/README.md`
"Running". Exit codes: 0 done, 2 refused or misconfigured, 4 stopped early or
items not evaluated.
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
from typing import get_args

from assistant.core.clock import SystemClock
from evals.agent_mode import AgentModeTarget, Calibration, check_agent_estimate, load_calibration
from evals.budget import (
    DEFAULT_BUDGET_USD,
    BudgetedLLMClient,
    CallLedger,
    Prices,
    check_overrides,
    parse_price,
)
from evals.corpus import (
    drop_leftover_databases,
    eval_database_url,
    isolated_corpus,
    leftover_databases,
)
from evals.errors import EvalsError
from evals.live import (
    LiveSetupError,
    ModelChoice,
    build_client,
    load_live_config,
    resolve_model,
)
from evals.metrics import OwnerLanguage
from evals.report import RunMeta, fmt_measure, render_comparison
from evals.retrieval import QueryVariants, RetrievalMode, RetrievalTarget, load_recorded
from evals.review import ReviewLedger
from evals.runner import (
    RefusedError,
    RunOutcome,
    check_estimate,
    ensure_frozen,
    require_live,
    run_target,
)
from evals.schema import E3_PATH, Burst, load_e1, load_e3, load_queries
from evals.summary import SummaryTarget, isolated_summary
from evals.targets import EvalTarget, RunContext, TargetResult
from evals.triage import SET_NAME as TRIAGE_SET_NAME
from evals.triage import TriageTarget, isolated_triage, model_choice, render_choice

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / "docs" / "evals"
REVIEWS_DIR = Path(__file__).resolve().parent / "reviews"
EXIT_REFUSED = 2
#: A run that stopped before its planned items (the budget cap), or with items it
#: could not evaluate (the provider stayed unavailable): its report was written,
#: but the run failed.
EXIT_INCOMPLETE = 4


def exit_code(result: TargetResult) -> int:
    """Non-zero for an incomplete run (stopped early, or an item not evaluated).
    A deliberate `--limit` or `--only` trial is not a failure."""
    return EXIT_INCOMPLETE if result.incomplete else 0


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got '{value}'") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _ids(value: str) -> tuple[str, ...]:
    """`--only c36,c28,c15`: comma-separated item ids, at least one."""
    ids = tuple(part.strip() for part in value.split(",") if part.strip())
    if not ids:
        raise argparse.ArgumentTypeError("expected comma-separated ids, e.g. c36,c28")
    return ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="check the datasets against the frozen manifest")

    run = sub.add_parser("run", help="run a target and record the result")
    run.add_argument("target", choices=["retrieval", "summary", "triage"])
    run.add_argument(
        "--mode",
        choices=[m.value for m in RetrievalMode] + ["both"],
        default=RetrievalMode.DETERMINISTIC.value,
        help=(
            "retrieval only: deterministic (no model, default), variants (a model), both, "
            "replay (no model: a recorded variants run searched again, see --replay-from), "
            "agent (the real owner_request runtime per query, a model)"
        ),
    )
    run.add_argument(
        "--calibrate-from",
        type=Path,
        metavar="TRIAL.json",
        help=(
            "retrieval --mode agent: the JSON report of a --limit 1 agent trial; the run then "
            "starts on its expected cost under the in-flight cap (E-ab)"
        ),
    )
    run.add_argument(
        "--replay-from",
        type=Path,
        metavar="REPORT.json",
        help="retrieval --mode replay: the JSON report of the variants run to replay",
    )
    run.add_argument(
        "--owner-language",
        choices=get_args(OwnerLanguage),
        default="uz-Latn",
        help=(
            "summary and retrieval --mode agent: the owner's language and script "
            "(summaries are written in it; the agent's owner preference)"
        ),
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
    run.add_argument(
        "--limit",
        type=_positive,
        help="evaluate only the first N items (N >= 1): a trial run, never a gate",
    )
    run.add_argument(
        "--only",
        type=_ids,
        metavar="ID,ID,...",
        help=(
            "summary and triage: evaluate only these conversations or bursts (e.g. "
            "c36,c28,c15 or b001,b002), in set order: a trial run, never a gate"
        ),
    )
    run.add_argument("--estimate-only", action="store_true", help="print the cost and stop")
    run.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="where to write reports")
    run.add_argument("--no-write", action="store_true", help="do not write report files")
    run.add_argument("--reviews", type=Path, help="owner review ledger (JSON)")
    run.add_argument("--date", type=date.fromisoformat, help="run date (default: today, UTC)")

    cleanup = sub.add_parser(
        "cleanup", help="list (or, with --yes, drop) throwaway databases a killed run left behind"
    )
    cleanup.add_argument("--yes", action="store_true", help="drop them (default: only list)")

    compare = sub.add_parser("compare", help="compare recorded runs (quality and cost)")
    compare.add_argument("reports", nargs="+", type=Path, help="JSON reports from docs/evals/")
    compare.add_argument(
        "--route-model",
        help="triage: the route's current model, whose run comes first (default: the "
        "configured LLM_ROUTE_TRIAGE_MODEL, else LLM_DEFAULT_MODEL; name the server's "
        "when this machine's settings differ or do not load)",
    )
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
        prices = dict(parse_price(spec) for spec in specs)
        check_overrides(prices)
    except ValueError as error:
        raise RefusedError(str(error)) from None
    return prices


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            return _verify()
        if args.command == "cleanup":
            return _cleanup(drop=args.yes)
        if args.command == "compare":
            return _compare(args.reports, route_model=args.route_model)
        if args.target == "summary":
            return asyncio.run(_summary(args))
        if args.target == "triage":
            return asyncio.run(_triage(args))
        return asyncio.run(_retrieval(args))
    except EvalsError as error:  # the tool's own, explainable failures; a bug keeps its traceback
        return _refuse(str(error))


def _verify() -> int:
    digests = ensure_frozen()
    _say("datasets match evals/datasets/MANIFEST.sha256:")
    for name, digest in digests.items():
        _say(f"  {digest}  {name}")
    return 0


def _cleanup(*, drop: bool) -> int:
    """A run drops its throwaway database when it ends, fails or is interrupted; a
    process that is killed cannot. This lists such leftovers on the evaluation
    server (`assistant_eval_` and 8 hex digits, nothing else) and drops them with
    `--yes`. A database a run is using (an open connection) is never listed or
    dropped."""
    base = eval_database_url()
    names = leftover_databases(base)
    if not names:
        _say("no leftover evaluation databases")
        return 0
    for name in names:
        _say(name)
    if not drop:
        _say(f"{len(names)} leftover database(s); run again with --yes to drop them")
        return 0
    dropped = drop_leftover_databases(base, names)
    _say(f"dropped {len(dropped)} database(s)")
    return 0


def _compare(paths: Sequence[Path], *, route_model: str | None = None) -> int:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    sys.stdout.write(render_comparison(reports))
    if reports and all(report.get("target") == TriageTarget.name for report in reports):
        # The E3 model comparison: the route's current model comes from the
        # configuration (or is named), never from the order of the reports.
        try:
            current = resolve_model(TriageTarget.route, route_model).model
        except LiveSetupError:
            raise RefusedError(
                "no model-choice decision: the triage route's current model is unknown "
                "(the settings do not load); name it with --route-model"
            ) from None
        try:
            choice = model_choice(reports, route_model=current)
        except ValueError as error:
            raise RefusedError(f"no model-choice decision: {error}") from None
        sys.stdout.write(render_choice(choice))
    return 0


def _require_recorded(args: argparse.Namespace) -> None:
    if args.live and args.no_write:
        raise RefusedError(
            "refusing --live with --no-write: a model run must leave its report (and, if it "
            "fails, its spend ledger) so money spent is never unrecorded"
        )


def _print_outcome(
    label: str, outcome: RunOutcome, *, ledger: CallLedger, cap: Decimal, spent: bool
) -> None:
    result = outcome.result
    suffix = " (TRIAL: a --limit or --only run is never a gate)" if result.trial else ""
    _say(f"{label}: {result.items} items{suffix}")
    for metric in result.metrics:
        confirmed, of = metric.confirmation()
        _say(
            f"  {metric.label}: {fmt_measure(metric.all_items, unit=metric.unit)} "
            f"pre-screen; gate {metric.gate_status().value} "
            f"({confirmed} of {of} confirmed)"
        )
    for note in result.notes:
        _say(f"  note: {note}")
    if outcome.markdown is not None:
        _say(f"  report: {outcome.markdown}")
    if spent:
        _say(f"  spent ${ledger.spent:.4f} of ${cap}")


async def _summary(args: argparse.Namespace) -> int:
    """The summary target: the real S03.2 catch-up path on the E1 set, in a
    throwaway database. It always calls a model, so every guard applies."""
    _require_recorded(args)
    if args.calibrate_from is not None:
        raise RefusedError("--calibrate-from is only for retrieval --mode agent")
    digests = ensure_frozen()
    cap, prices = _cap(args.budget_usd), _prices(args.price)
    reviews = _reviews(args.reviews, "e1")
    target = SummaryTarget(load_e1(), owner_language=args.owner_language)
    if args.only is not None:
        try:
            target.named(args.only, limit=args.limit)
        except ValueError as error:
            raise RefusedError(f"--only: {error}") from None
    (choice,) = _guard([target], [target.label], args, cap, prices, reviews)
    if args.estimate_only:
        return 0
    assert choice is not None  # the summary target always uses a model
    live_client = build_client(load_live_config(target.route, args.model))
    run_date = args.date or SystemClock().now().date()
    out_dir = None if args.no_write else args.out_dir
    ledger = CallLedger()
    llm = BudgetedLLMClient(live_client, cap_usd=cap, overrides=prices, ledger=ledger)
    context = RunContext(
        llm=llm,
        model=choice.model,
        effort=choice.effort,
        reviews=reviews,
        limit=args.limit,
        only=args.only,
    )
    meta = RunMeta(
        run_date=run_date,
        set_name=target.name,
        mode=target.label,
        model=choice.model,
        live=True,
        effort=choice.effort,
        dataset_files=digests,
        cap_usd=cap,
        reviews=reviews,
    )
    async with isolated_summary(eval_database_url(), target) as bound:
        try:
            outcome = await run_target(bound, context, meta=meta, ledger=ledger, out_dir=out_dir)
        except BaseException:
            if ledger.entries:
                _refuse(f"the run failed after spending ${ledger.spent:.4f} of ${cap}")
            raise
    _print_outcome(f"summary ({target.label})", outcome, ledger=ledger, cap=cap, spent=True)
    return exit_code(outcome.result)


def _bursts() -> list[Burst]:
    """E3, or a clean refusal while it does not load."""
    try:
        return load_e3()
    except FileNotFoundError:
        raise RefusedError(f"E3 is not authored: {E3_PATH.name} does not exist") from None
    except ValueError:
        # Not the message: a validation error can echo dataset text.
        raise RefusedError(
            "E3 does not pass its integrity checks; run `just test tests/unit/test_e3_dataset.py`"
        ) from None


async def _triage(args: argparse.Namespace) -> int:
    """The triage target: the product's `Triager` and effects store over E3,
    one throwaway account per burst in a throwaway database. It always calls
    a model, so every guard applies. Each burst carries its own owner language
    and timezone (`--owner-language` does not apply)."""
    _require_recorded(args)
    if args.calibrate_from is not None:
        raise RefusedError("--calibrate-from is only for retrieval --mode agent")
    digests = ensure_frozen()
    cap, prices = _cap(args.budget_usd), _prices(args.price)
    reviews = _reviews(args.reviews, "e3")
    target = TriageTarget(_bursts())
    if args.only is not None:
        try:
            target.named(args.only, limit=args.limit)
        except ValueError as error:
            raise RefusedError(f"--only: {error}") from None
    (choice,) = _guard([target], [target.label], args, cap, prices, reviews)
    if args.estimate_only:
        return 0
    assert choice is not None  # the triage target always uses a model
    live_client = build_client(load_live_config(target.route, args.model))
    run_date = args.date or SystemClock().now().date()
    out_dir = None if args.no_write else args.out_dir
    ledger = CallLedger()
    llm = BudgetedLLMClient(live_client, cap_usd=cap, overrides=prices, ledger=ledger)
    context = RunContext(
        llm=llm,
        model=choice.model,
        effort=choice.effort,
        reviews=reviews,
        limit=args.limit,
        only=args.only,
    )
    meta = RunMeta(
        run_date=run_date,
        set_name=TRIAGE_SET_NAME,
        mode=target.label,
        model=choice.model,
        live=True,
        effort=choice.effort,
        dataset_files=digests,
        cap_usd=cap,
        reviews=reviews,
    )
    async with isolated_triage(eval_database_url(), target) as bound:
        try:
            outcome = await run_target(bound, context, meta=meta, ledger=ledger, out_dir=out_dir)
        except BaseException:
            if ledger.entries:
                _refuse(f"the run failed after spending ${ledger.spent:.4f} of ${cap}")
            raise
    _print_outcome(f"triage ({target.label})", outcome, ledger=ledger, cap=cap, spent=True)
    return exit_code(outcome.result)


async def _retrieval(args: argparse.Namespace) -> int:
    _require_recorded(args)
    if args.only is not None:
        raise RefusedError("--only is only for the summary and triage targets")
    digests = ensure_frozen()
    cap, prices = _cap(args.budget_usd), _prices(args.price)
    reviews = _reviews(args.reviews, "retrieval")
    modes = (
        [RetrievalMode.DETERMINISTIC, RetrievalMode.VARIANTS]
        if args.mode == "both"
        else [RetrievalMode(args.mode)]
    )
    if args.calibrate_from is not None and modes != [RetrievalMode.AGENT]:
        raise RefusedError("--calibrate-from is only for --mode agent")
    queries = load_queries()
    recorded = _recorded(args)
    targets: list[RetrievalTarget | AgentModeTarget] = [
        AgentModeTarget(queries, owner_language=args.owner_language)
        if mode is RetrievalMode.AGENT
        else RetrievalTarget(
            queries,
            mode=mode,
            recorded=recorded if mode is RetrievalMode.REPLAY else None,
            replayed_from=args.replay_from.name if args.replay_from is not None else None,
        )
        for mode in modes
    ]
    choices = _guard(targets, [t.mode.value for t in targets], args, cap, prices, reviews)
    if args.estimate_only:
        return 0
    live_client = None
    if any(t.uses_model for t in targets):
        live_client = build_client(load_live_config(targets[-1].route, args.model))
    run_date = args.date or SystemClock().now().date()
    out_dir = None if args.no_write else args.out_dir
    code = 0
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
                set_name=_set_name(target),
                mode=target.mode.value,
                model=None if choice is None else choice.model,
                live=choice is not None,
                effort=None if choice is None else choice.effort,
                dataset_files=digests,
                cap_usd=None if choice is None else cap,
                reviews=reviews,
            )
            try:
                outcome = await run_target(
                    target.bind(corpus), context, meta=meta, ledger=ledger, out_dir=out_dir
                )
            except BaseException:
                if ledger.entries:
                    _refuse(f"the run failed after spending ${ledger.spent:.4f} of ${cap}")
                raise
            _print_outcome(
                f"retrieval ({target.mode.value})",
                outcome,
                ledger=ledger,
                cap=cap,
                spent=choice is not None,
            )
            code = max(code, exit_code(outcome.result))
    return code


def _recorded(args: argparse.Namespace) -> dict[str, QueryVariants | None] | None:
    """The recorded variants run a replay searches again; refused before any database."""
    if args.mode != RetrievalMode.REPLAY.value:
        if args.replay_from is not None:
            raise RefusedError("--replay-from is only for --mode replay")
        return None
    if args.replay_from is None:
        raise RefusedError("--mode replay needs --replay-from <the variants run's JSON report>")
    return load_recorded(args.replay_from)


def _set_name(target: RetrievalTarget | AgentModeTarget) -> str:
    """A replay's report must not take the name of the deterministic baseline's,
    nor an agent run's the name of a variants run with the same model."""
    if target.mode in (RetrievalMode.REPLAY, RetrievalMode.AGENT):
        return f"{target.name}-{target.mode.value}"
    return target.name


def _guard(
    targets: Sequence[EvalTarget],
    labels: Sequence[str],
    args: argparse.Namespace,
    cap: Decimal,
    prices: Prices,
    reviews: ReviewLedger,
) -> list[ModelChoice | None]:
    """Everything that can refuse a run, before a database or a model is touched."""
    choices: list[ModelChoice | None] = []
    for target, label in zip(targets, labels, strict=True):
        if not target.uses_model:
            choices.append(None)
            continue
        if not args.estimate_only:
            require_live(target, live=args.live)
        choice = resolve_model(target.route, args.model)
        context = RunContext(model=choice.model, reviews=reviews, limit=args.limit, only=args.only)
        expected_text = ""
        if isinstance(target, AgentModeTarget):
            calibration = _calibration(args, choice.model)
            result, expected = check_agent_estimate(
                target,
                context,
                model=choice.model,
                cap_usd=cap,
                prices=prices,
                calibration=calibration,
            )
            if expected is not None and calibration is not None:
                expected_text = f", expected ${expected:.4f} (calibrated from {calibration.source})"
        else:
            result = check_estimate(target, context, model=choice.model, cap_usd=cap, prices=prices)
        _say(
            f"{target.name} ({label}): model {choice.model}, {result.calls} calls, "
            f"worst case ${result.upper_bound_usd:.4f}{expected_text}, cap ${cap}"
        )
        choices.append(choice)
    return choices


def _calibration(args: argparse.Namespace, model: str) -> Calibration | None:
    """The agent trial a full agent run is calibrated from (E-ab), if given."""
    if args.calibrate_from is None:
        return None
    return load_calibration(args.calibrate_from, model=model)
