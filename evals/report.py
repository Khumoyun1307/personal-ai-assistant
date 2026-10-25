"""Result recording: a dated Markdown report and a machine-readable JSON next to it
(docs/evals/README.md "Naming").

`docs/evals/<YYYY-MM-DD>-<set>-<model>.md` and `.json`. A rerun the same day
gets a `-2` suffix. Reports hold ids, numbers and statuses only: synthetic
data, but no chat content beyond what the dataset itself holds.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.budget import CallLedger
from evals.review import ReviewLedger
from evals.targets import Measure, MetricResult, Status, TargetResult

SCHEMA_VERSION = 1
#: The label used in file names and reports for a run that called no model.
DETERMINISTIC_LABEL = "deterministic"


@dataclass(frozen=True, slots=True)
class RunMeta:
    run_date: date
    #: The evaluated set, as in file names: `retrieval`, `e1`.
    set_name: str
    mode: str | None
    #: The configured model id, or None for a model-free run.
    model: str | None
    live: bool
    effort: str | None
    #: Dataset file -> SHA-256 (the frozen manifest, verified before the run).
    dataset_files: Mapping[str, str]
    cap_usd: Decimal | None = None
    reviews: ReviewLedger = field(default_factory=ReviewLedger.empty)

    @property
    def model_label(self) -> str:
        return self.model or DETERMINISTIC_LABEL


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")


def stem_for(meta: RunMeta) -> str:
    return f"{meta.run_date.isoformat()}-{_safe(meta.set_name)}-{_safe(meta.model_label)}"


def unique_stem(out_dir: Path, stem: str) -> str:
    """`stem`, or `stem-2`, `stem-3`… when a report with that name exists."""
    candidate, number = stem, 1
    while (out_dir / f"{candidate}.md").exists() or (out_dir / f"{candidate}.json").exists():
        number += 1
        candidate = f"{stem}-{number}"
    return candidate


# ------------------------------------------------------------------- JSON
def _measure_json(measure: Measure) -> dict[str, Any]:
    return {
        "numerator": measure.numerator,
        "denominator": measure.denominator,
        "ratio": measure.ratio,
    }


def _metric_json(metric: MetricResult, complete: bool) -> dict[str, Any]:
    return {
        "key": metric.key,
        "label": metric.label,
        "unit": metric.unit,
        "goal": metric.goal,
        "threshold": metric.threshold,
        "all_items": _measure_json(metric.all_items),
        "confirmed": _measure_json(metric.confirmed),
        "prescreen_status": metric.prescreen_status().value,
        "gate_status": metric.gate_status(complete=complete).value,
        "failed_items": list(metric.failed_items),
        "pending": metric.pending,
        "note": metric.note,
    }


def to_json(meta: RunMeta, result: TargetResult, ledger: CallLedger) -> dict[str, Any]:
    items = result.items
    per_model: dict[str, Any] = {}
    for model, totals in ledger.by_model().items():
        per_100 = totals.per_100_items(items)
        per_model[model] = {
            "calls": totals.calls,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cost_usd": str(totals.cost_usd),
            "cost_per_100_items_usd": None if per_100 is None else str(per_100),
        }
    return {
        "schema": SCHEMA_VERSION,
        "run_date": meta.run_date.isoformat(),
        "set": meta.set_name,
        "target": result.target,
        "dataset": result.dataset,
        "mode": meta.mode,
        "model": meta.model,
        "live": meta.live,
        "effort": meta.effort,
        "complete": result.complete,
        "items": items,
        "items_planned": result.items_planned,
        "dataset_files": dict(meta.dataset_files),
        "prompts": list(result.prompts),
        "reviews": {
            "source": meta.reviews.source,
            "confirmed_items": len(meta.reviews.confirmed_items),
        },
        "metrics": [_metric_json(m, result.complete) for m in result.metrics],
        "breakdowns": {
            name: {key: _measure_json(m) for key, m in slices.items()}
            for name, slices in result.breakdowns.items()
        },
        "cost": {
            "cap_usd": None if meta.cap_usd is None else str(meta.cap_usd),
            "spent_usd": str(ledger.spent),
            "calls": len(ledger.entries),
            "estimated_calls": sum(entry.estimated for entry in ledger.entries),
            "per_model": per_model,
        },
        "notes": list(result.notes),
        "details": [dict(d) for d in result.details],
    }


# --------------------------------------------------------------- Markdown
def fmt_measure(measure: Measure, *, unit: str = "ratio") -> str:
    if measure.denominator == 0:
        return "n/a (no items)"
    if unit == "count":
        return f"{measure.numerator} (of {measure.denominator} items)"
    ratio = measure.ratio or 0.0
    return f"{measure.numerator}/{measure.denominator} ({ratio * 100:.1f} %)"


def _threshold(metric: MetricResult) -> str:
    if metric.threshold is None:
        return "informational"
    if metric.unit == "count":
        return f"{'<=' if metric.goal == 'at_most' else '>='} {metric.threshold:g}"
    return f"{'>=' if metric.goal == 'at_least' else '<='} {metric.threshold * 100:g} %"


def _usd(value: Decimal) -> str:
    return f"${value:.4f}"


def render_markdown(meta: RunMeta, result: TargetResult, ledger: CallLedger) -> str:
    data = to_json(meta, result, ledger)
    title = f"{meta.set_name} ({result.target}) - {meta.model_label} - {meta.run_date.isoformat()}"
    lines = [f"# Evaluation: {title}", ""]
    if not result.complete:
        lines += [
            "> **INCOMPLETE RUN.** "
            f"{result.items} of {result.items_planned} items ran; no gate can pass.",
            "",
        ]
    lines += [
        "| | |",
        "|---|---|",
        f"| Run date | {meta.run_date.isoformat()} |",
        f"| Target / mode | {result.target} / {meta.mode or '-'} |",
        f"| Model (configured) | {meta.model or 'none (deterministic, no model)'} |",
        f"| Real model called | {'yes (`--live`)' if meta.live else 'no'} |",
        f"| Reasoning effort | {meta.effort or '-'} |",
        f"| Prompts / rubrics | {', '.join(result.prompts) or '-'} |",
        f"| Items evaluated | {result.items} of {result.items_planned} |",
        f"| Owner review ledger | {_ledger_text(meta.reviews)} |",
        "",
        "Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; "
        "the version is in the dataset log of `docs/evals/README.md`):",
        "",
    ]
    lines += [f"- `{name}`: `{digest}`" for name, digest in meta.dataset_files.items()]
    lines += ["", "## Results", ""]
    lines += [
        "Pre-screen = every item (deterministic checks and judge flags only pre-screen). "
        "Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1); with none, the gate "
        "is *not evaluable*, never a pass.",
        "",
        "| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |",
        "|---|---|---|---|---|",
    ]
    for metric in result.metrics:
        status = metric.gate_status(complete=result.complete)
        lines.append(
            f"| {metric.label} | {_threshold(metric)} "
            f"| {fmt_measure(metric.all_items, unit=metric.unit)} "
            f"({metric.prescreen_status().value}) "
            f"| {fmt_measure(metric.confirmed, unit=metric.unit)} "
            f"| **{_status_text(status)}** |"
        )
    for metric in result.metrics:
        if metric.failed_items:
            lines += ["", f"Failed items, {metric.label}: " + ", ".join(metric.failed_items)]
        if metric.note:
            lines += ["", f"Note, {metric.label}: {metric.note}"]
    for name, slices in result.breakdowns.items():
        lines += ["", f"### {name}", "", "| Slice | Result |", "|---|---|"]
        lines += [f"| {key} | {fmt_measure(m)} |" for key, m in slices.items()]
    lines += ["", "## Cost", ""]
    lines += _cost_lines(meta, data, result)
    if result.notes:
        lines += ["", "## Notes", ""] + [f"- {note}" for note in result.notes]
    lines += [
        "",
        "## Model-choice decision",
        "",
        _decision_text(meta),
        "",
    ]
    return "\n".join(lines)


def _ledger_text(ledger: ReviewLedger) -> str:
    if ledger.source is None:
        return "none (no item is owner-confirmed)"
    return f"`{ledger.source}` ({len(ledger.confirmed_items)} confirmed items)"


def _status_text(status: Status) -> str:
    return status.value.upper()


def _cost_lines(meta: RunMeta, data: Mapping[str, Any], result: TargetResult) -> list[str]:
    cost = data["cost"]
    if not meta.live and cost["calls"] == 0:
        return ["No model was called: cost USD 0."]
    lines = [
        f"Cap USD {cost['cap_usd']}; spent {_usd(Decimal(cost['spent_usd']))} over "
        f"{cost['calls']} calls ({cost['estimated_calls']} billed at their worst case "
        f"because their usage was unknown).",
        "",
        "| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |",
        "|---|---|---|---|---|---|",
    ]
    for model, row in cost["per_model"].items():
        per_100 = row["cost_per_100_items_usd"]
        lines.append(
            f"| {model} | {row['calls']} | {row['input_tokens']} | {row['output_tokens']} "
            f"| {_usd(Decimal(row['cost_usd']))} "
            f"| {'-' if per_100 is None else _usd(Decimal(per_100))} |"
        )
    lines += ["", f"Cost per 100 items is over the {result.items} items evaluated."]
    return lines


def _decision_text(meta: RunMeta) -> str:
    if meta.model is None:
        return "Not applicable: a model-free baseline."
    return (
        "None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on "
        "the default model unless it misses a threshold *on owner-confirmed items*; any move up "
        "is decided with the quality and cost per 100 items of each model tried (see "
        "`python -m evals compare`) and written down by the owner."
    )


# ------------------------------------------------------------------- write
def write_report(
    out_dir: Path, meta: RunMeta, result: TargetResult, ledger: CallLedger
) -> tuple[Path, Path]:
    """Write the Markdown and JSON reports; return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = unique_stem(out_dir, stem_for(meta))
    markdown = out_dir / f"{stem}.md"
    machine = out_dir / f"{stem}.json"
    markdown.write_text(render_markdown(meta, result, ledger), encoding="utf-8", newline="\n")
    machine.write_text(
        json.dumps(to_json(meta, result, ledger), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return markdown, machine


# ----------------------------------------------------------------- compare
def render_comparison(reports: Sequence[Mapping[str, Any]]) -> str:
    """Quality and cost per 100 items, one row per run, for the model-choice
    rule. Takes the parsed JSON reports."""
    lines = [
        "| Run | Model | Metric | Pre-screen | Gate | Cost per 100 items |",
        "|---|---|---|---|---|---|",
    ]
    for report in reports:
        per_model = report["cost"]["per_model"]
        costs = [row["cost_per_100_items_usd"] for row in per_model.values()]
        cost_text = (
            "USD 0" if not per_model else ", ".join(f"${Decimal(c):.4f}" for c in costs if c)
        )
        for metric in report["metrics"]:
            all_items = metric["all_items"]
            ratio = all_items["ratio"]
            shown = (
                "n/a"
                if ratio is None
                else (
                    f"{all_items['numerator']} of {all_items['denominator']}"
                    if metric["unit"] == "count"
                    else f"{ratio * 100:.1f} %"
                )
            )
            lines.append(
                f"| {report['run_date']} {report['set']} ({report['mode']}) "
                f"| {report['model'] or 'deterministic'} | {metric['label']} | {shown} "
                f"| {metric['gate_status']} | {cost_text} |"
            )
    return "\n".join(lines) + "\n"
