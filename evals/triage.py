"""The triage target (P2 spec E3 §B): the product's `Triager` over the E3 bursts.

`python -m evals run triage` measures triage **exactly as the product runs it**
(S05.1): the real `Triager` (its context builder, the active `triage` prompt
from the prompt registry, the structured `TriageResult` schema, sanitizing and
the deterministic validation) and the real effects store (`TriageRepository`:
the verdict row, tasks created and closed, notifications enqueued). Nothing is
delivered: the store gets no scheduler, so no delivery job is deferred, and
nothing in this stack can reach Telegram.

Per burst, in a **throwaway database**, under its own synthetic account (no
burst sees another's chat, tasks, verdicts or alerts):

1. the account: `ai_control` and `ai_managed` consents naming the processor of
   the run's model (the triager's own consent check needs them), the burst's
   `owner_language` as `locale_pref` and `owner_timezone` as `timezone`;
2. the contact (with `contact_priority`) and its chat in Observe mode, the
   history and the burst stored the way ingestion stores messages;
3. the burst's open tasks as the agent's own open tasks (`created_by =
   'agent'`, first source message from `source`), and a `conversation_triage`
   row covering the history, so that the burst is exactly what is new;
4. `Triager.triage` with the route's model (or `--model`), the product's
   budgets and a clock fixed at the burst's `now`;
5. the verdict row, the tasks and the notifications are read back; then the
   account is deleted (every row cascades).

Metrics (spec E3 §B), pre-screen over every burst, gate over owner-confirmed
bursts only (`ReviewLedger.confirmed_items` = burst ids):

- **urgent/decision recall** (>= 90 %): positives (`alert: true`) whose run
  enqueued `triage_urgent` or `triage_decision`;
- **false-alert rate** (<= 20 %): of the bursts that got one of those two
  alerts, the share labelled `alert: false`;
- **false-closure rate** (<= 10 %): `unresolved_tasks` the run closed;
- **resolution recall** (>= 80 %): `resolved_tasks` the run closed;
- informational: negatives alerted, `needs_reply` agreement, commitment recall
  by side (a commitment task of that side citing the labelled message; and
  whether it carries the `values`), suspicious recall, and the owner-language
  and script share of the run's own words (reasons, task titles; P2-O11).

A model failure (refusal, invalid or truncated output, a discarded verdict)
stores nothing: it is a miss wherever a hit was needed, and listed. A provider
outage (`OUTAGE_ERRORS`: 429, 503, connection) is run again from a fresh
account with the agent mode's bounded attempts and delays; a burst that never
gets through is *not evaluated* (the run is then incomplete). A budget stop
ends the run early (incomplete, never a gate).

The model comparison (IMPLEMENTATION_PLAN §7, WORKFLOW §11): `--model` runs the
target on another tier; `python -m evals compare` of the full runs' JSON
reports, starting with the route's current model and then in the order the
tiers were tried, applies the rule in `model_choice` and states the decision
with each tier's cost per 100 bursts.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.accounts.service import CONSENT_AI_CONTROL, CONSENT_AI_MANAGED, grant_consent
from assistant.agent import prompt_registry
from assistant.agent.models import AgentRun
from assistant.agent.triage import RUN_TYPE, TriageConfig, Triager, TriageStatus
from assistant.agent.triage_context import build_triage
from assistant.conversations.models import Contact
from assistant.conversations.models import Conversation as ConversationRow
from assistant.conversations.models import Message as MessageRow
from assistant.core.model_routes import processor_for_model
from assistant.db.engine import build_engine, build_session_factory
from assistant.inbox import tasks as task_kinds
from assistant.inbox.models import ConversationTriage, Notification, Task
from assistant.inbox.notification_types import NotificationKind
from assistant.inbox.tasks import TaskView
from assistant.inbox.triage_store import BURST_LIMIT, EARLIER_LIMIT, TriageRepository
from assistant.memory.search import detect_script, guess_lang, normalize
from assistant.memory.summaries import SourceMessage
from evals.agent_mode import MAX_ATTEMPTS, OUTAGE_ERRORS, RETRY_DELAYS
from evals.budget import BudgetExceededError
from evals.corpus import scratch_database, unload_corpus
from evals.metrics import (
    SCRIPT_SHARE_MIN,
    check_language,
    contains_value,
    ratio_metric,
    word_key,
)
from evals.review import ReviewLedger
from evals.schema import Burst, BurstMessage
from evals.targets import (
    DatasetName,
    Measure,
    MetricResult,
    PlannedCall,
    RunContext,
    TargetResult,
)

#: The run type whose model this target measures (`LLM_ROUTE_TRIAGE_MODEL`).
ROUTE = "triage"
#: The model-call purpose, as the requests carry it (the plan uses the same name).
TRIAGE_PURPOSE = RUN_TYPE
#: Reports are `docs/evals/<date>-triage-<model>.md` (spec E3 §B).
SET_NAME = "triage"
#: A consent version label that cannot be mistaken for a real policy version.
EVAL_POLICY_VERSION = "eval-synthetic"
#: What the seeded `conversation_triage` row (the history, already triaged) names
#: as its prompt and model: never a real run's.
SEED_LABEL = "eval-seed"
#: The alerts that count as "the owner was alerted" (spec E3 §B, recall).
ALERT_KINDS = frozenset(
    {NotificationKind.TRIAGE_URGENT.value, NotificationKind.TRIAGE_DECISION.value}
)
#: Thresholds (IMPLEMENTATION_PLAN §7, §10.1; the false-alert rate is a TL decision).
ALERT_RECALL_THRESHOLD = 0.90
FALSE_ALERT_THRESHOLD = 0.20
FALSE_CLOSURE_THRESHOLD = 0.10
RESOLUTION_RECALL_THRESHOLD = 0.80
#: The statuses that are the product's own answer: a stored verdict, or nothing to
#: triage (an owner-only burst with no open task makes no call).
ANSWERED = frozenset({TriageStatus.TRIAGED.value, TriageStatus.UP_TO_DATE.value})
#: Statuses that mean the harness is mis-built, not that the model missed.
_HARNESS_ERRORS = frozenset(
    {
        TriageStatus.NO_CONSENT,
        TriageStatus.NOT_FOUND,
        TriageStatus.SKIPPED,
        TriageStatus.OVER_BUDGET,
        TriageStatus.BACKED_OFF,
    }
)
#: Message ids in the real database have more digits than the plan's: slack for
#: the pre-run estimate only.
_ID_SLACK_CHARS = 500
_SYNTHETIC_OWNER_ID = 7_000_000_095
_SYNTHETIC_CONTACT_ID = 3_000_001


class _FixedClock:
    """The burst's `now`: reproducible run, verdict, task and alert times."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


# ---------------------------------------------------------------- the burst
def message_values(message: BurstMessage, index: int) -> dict[str, Any]:
    """A dataset message as ingestion stores it (`index`: its Telegram id)."""
    values: dict[str, Any] = {
        "telegram_message_id": index,
        "direction": "contact" if message.direction == "contact" else "owner_manual",
        "sent_at": message.sent_at,
        "content_type": message.media or "text",
    }
    if message.media is not None:
        values["media"] = {"kind": message.media}
    if message.text:
        values["text"] = message.text
        values["search_key"] = normalize(message.text)
        values["script"] = detect_script(message.text)
        values["lang_guess"] = guess_lang(message.text)
    return values


def makes_a_call(burst: Burst) -> bool:
    """Whether the product calls the model for this burst (`Triager.triage`):
    a contact wrote, or the owner wrote while a task is open."""
    sides = {m.direction for m in burst.burst}
    return "contact" in sides or ("owner" in sides and bool(burst.open_tasks))


def _source(message: BurstMessage, number: int) -> SourceMessage:
    return SourceMessage(
        message_id=1000 + number,
        direction="contact" if message.direction == "contact" else "owner_manual",
        sent_at=message.sent_at,
        content_type=message.media or "text",
        text=message.text,
        media_kind=message.media,
        edited_at=None,
    )


# ------------------------------------------------------------------ records
@dataclass(frozen=True, slots=True)
class StoredVerdict:
    """The `conversation_triage` row the run stored (the product's verdict)."""

    needs_reply: bool
    contains_question: bool
    requests_decision: bool
    urgency: str
    deadline_at: datetime | None
    escalation_category: str
    reasons: tuple[str, ...]
    language: str | None
    script: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "needs_reply": self.needs_reply,
            "contains_question": self.contains_question,
            "requests_decision": self.requests_decision,
            "urgency": self.urgency,
            "deadline": None if self.deadline_at is None else self.deadline_at.isoformat(),
            "escalation_category": self.escalation_category,
            "reasons": list(self.reasons),
            "language": self.language,
            "script": self.script,
        }


@dataclass(frozen=True, slots=True)
class TaskTouch:
    """A task the run created, or an open task it updated."""

    kind: str
    #: Its source messages, as dataset ids.
    sources: tuple[str, ...]
    title: str
    due_at: datetime | None
    #: The dataset id of the open task it updated; None for a new task.
    updated: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sources": list(self.sources),
            "title": self.title,
            "due": None if self.due_at is None else self.due_at.isoformat(),
            "updated": self.updated,
        }


@dataclass(frozen=True, slots=True)
class LanguageCheck:
    """The run's own words (reasons, task titles) against the owner's language
    and script; `ok` None when there were none, or too few to name a language
    while the script is right (P2-O11, informational)."""

    ok: bool | None
    detected: str | None = None
    script_share: float = 0.0


@dataclass(frozen=True, slots=True)
class TriageRecord:
    burst: Burst
    #: The `TriageStatus` value.
    status: str
    #: The run's error class or discard reason; None when it answered.
    error: str | None
    #: Model calls made (a truncated answer is retried once, larger).
    calls: int
    verdict: StoredVerdict | None = None
    #: Kinds of the notifications enqueued, in order.
    alerts: tuple[str, ...] = ()
    tasks: tuple[TaskTouch, ...] = ()
    #: Dataset ids of the open tasks the run closed.
    closed: tuple[str, ...] = ()
    #: The provider was unavailable: neither a hit nor a miss.
    outage: bool = False
    #: Runs of this burst, the first included (an outage is run again).
    attempts: int = 1
    #: The error class of each attempt that failed on the provider.
    failed_attempts: tuple[str, ...] = ()

    @property
    def answered(self) -> bool:
        return self.status in ANSWERED

    @property
    def alerted(self) -> bool:
        return any(kind in ALERT_KINDS for kind in self.alerts)

    @property
    def needs_reply(self) -> bool:
        """What the product concluded; nothing stored means no reply flagged."""
        return self.verdict is not None and self.verdict.needs_reply

    @property
    def flagged_suspicious(self) -> bool:
        return self.verdict is not None and self.verdict.escalation_category == "suspicious"

    def own_words(self) -> str:
        reasons = self.verdict.reasons if self.verdict is not None else ()
        return "\n".join([*reasons, *(task.title for task in self.tasks)])

    def language(self) -> LanguageCheck:
        text = self.own_words()
        if not text.strip():
            return LanguageCheck(None)
        verdict = check_language(text, self.burst.owner_language)
        # M03's check fails on doubt; reasons and titles are short, so with the
        # right script and no language named the check is undetermined (as E2T).
        undetermined = verdict.detected is None and verdict.script_share >= SCRIPT_SHARE_MIN
        return LanguageCheck(
            None if undetermined else verdict.ok, verdict.detected, verdict.script_share
        )

    def recalled(self, label_id: str) -> TaskTouch | None:
        """The task that carries the labelled commitment: a commitment task of
        the same side, created or updated by this run, citing its message."""
        label = next(c for c in self.burst.labels.commitments if c.id == label_id)
        kind = f"{label.by}_commitment"
        return next((t for t in self.tasks if t.kind == kind and label.source in t.sources), None)

    def as_json(self) -> dict[str, Any]:
        """One record for the JSON report. Reasons and titles are model output
        over **synthetic** bursts: kept so the owner can review them; the
        Markdown report never prints them."""
        language = self.language()
        return {
            "id": self.burst.id,
            "variant": self.burst.variant,
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "failed_attempts": list(self.failed_attempts),
            "triage_calls": self.calls,
            "label_alert": self.burst.labels.alert,
            "alerted": self.alerted,
            "alerts": list(self.alerts),
            "verdict": None if self.verdict is None else self.verdict.as_json(),
            "tasks": [task.as_json() for task in self.tasks],
            "tasks_closed": list(self.closed),
            "language_ok": language.ok,
            "language_detected": language.detected,
            "script_share": round(language.script_share, 3),
        }


# ------------------------------------------------------------------ metrics
class TriageReport(NamedTuple):
    metrics: tuple[MetricResult, ...]
    breakdowns: dict[str, dict[str, Measure]]
    notes: tuple[str, ...]


def rate_metric(
    *,
    key: str,
    label: str,
    bad: Mapping[str, bool],
    threshold: float | None,
    ledger: ReviewLedger,
    set_size: int | None = None,
    pending: int = 0,
    note: str = "",
    gated: bool = True,
) -> MetricResult:
    """A rate to keep low (`bad`: item id -> counted against). The gate looks
    only at owner-confirmed items and needs all `set_size` of them."""
    confirmed = {item: hit for item, hit in bad.items() if ledger.is_confirmed(item)}
    return MetricResult(
        key=key,
        label=label,
        unit="ratio",
        goal="at_most",
        threshold=threshold,
        all_items=Measure(sum(bad.values()), len(bad)),
        confirmed=Measure(sum(confirmed.values()), len(confirmed)),
        failed_items=tuple(item for item, hit in bad.items() if hit),
        pending=pending,
        note=note,
        set_size=set_size,
        gated=gated,
    )


def _by_variant(
    records: Sequence[TriageRecord], outcome: Callable[[TriageRecord], list[bool]]
) -> dict[str, Measure]:
    grouped: dict[str, list[bool]] = {}
    for record in records:
        grouped.setdefault(record.burst.variant, []).extend(outcome(record))
    return {
        variant: Measure(sum(flags), len(flags))
        for variant, flags in sorted(grouped.items())
        if flags
    }


def _carries_values(touch: TaskTouch, values: Sequence[str], timezone: str) -> bool:
    """Every labelled value in the task's title or its owner-local due time,
    compared as E1 compares values (`word_key`, Uzbek endings allowed)."""
    due = ""
    if touch.due_at is not None:
        due = touch.due_at.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
    haystack = word_key(f"{touch.title} {due}")
    return all(contains_value(haystack, word_key(value)) for value in values)


def triage_metrics(
    records: Sequence[TriageRecord], everything: Sequence[Burst], ledger: ReviewLedger
) -> TriageReport:
    """Every metric of spec E3 §B over the evaluated records; set sizes from
    `everything` (the whole set), so a trial or stopped run can never gate."""
    positives = [r for r in records if r.burst.labels.alert]
    negatives = [r for r in records if not r.burst.labels.alert]
    alerted = [r for r in records if r.alerted]
    unresolved = {
        task: task in r.closed for r in records for task in r.burst.labels.unresolved_tasks
    }
    resolved = {task: task in r.closed for r in records for task in r.burst.labels.resolved_tasks}
    run_and_confirmed = sum(ledger.is_confirmed(r.burst.id) for r in records)
    commitments = {
        label.id: r.recalled(label.id) is not None
        for r in records
        for label in r.burst.labels.commitments
    }
    with_values = {
        label.id: _carries_values(touch, label.values, r.burst.owner_timezone)
        for r in records
        for label in r.burst.labels.commitments
        if label.values and (touch := r.recalled(label.id)) is not None
    }
    languages = {r.burst.id: r.language() for r in records}
    metrics = [
        ratio_metric(
            key="alert_recall",
            label="Urgent/decision recall (alert: true that got an urgent or decision alert)",
            outcomes={r.burst.id: r.alerted for r in positives},
            threshold=ALERT_RECALL_THRESHOLD,
            ledger=ledger,
            set_size=sum(b.labels.alert for b in everything),
        ),
        rate_metric(
            key="false_alert_rate",
            label="False-alert rate (alert: false among bursts that got an urgent/decision alert)",
            bad={r.burst.id: not r.burst.labels.alert for r in alerted},
            threshold=FALSE_ALERT_THRESHOLD,
            ledger=ledger,
            pending=len(everything) - run_and_confirmed,
            note=(
                "The gate also waits for every burst of the set to be run and owner-confirmed "
                "(an unconfirmed burst could be an alert); 'pending' counts those not yet."
            ),
        ),
        rate_metric(
            key="false_closure_rate",
            label="False-closure rate (unresolved tasks the run closed)",
            bad=unresolved,
            threshold=FALSE_CLOSURE_THRESHOLD,
            ledger=ledger,
            set_size=sum(len(b.labels.unresolved_tasks) for b in everything),
        ),
        ratio_metric(
            key="resolution_recall",
            label="Resolution recall (resolved tasks the run closed)",
            outcomes=resolved,
            threshold=RESOLUTION_RECALL_THRESHOLD,
            ledger=ledger,
            set_size=sum(len(b.labels.resolved_tasks) for b in everything),
        ),
        rate_metric(
            key="negatives_alerted",
            label="Negatives alerted (alert: false that got an urgent/decision alert)",
            bad={r.burst.id: r.alerted for r in negatives},
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
        ratio_metric(
            key="needs_reply_agreement",
            label="needs_reply agrees with the label",
            outcomes={r.burst.id: r.needs_reply == r.burst.labels.needs_reply for r in records},
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
        ratio_metric(
            key="commitment_recall",
            label="Commitment recall (a task of the right side citing the labelled message)",
            outcomes=commitments,
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
        ratio_metric(
            key="commitment_values",
            label="Recalled commitments carrying their values (title or due)",
            outcomes=with_values,
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
        ratio_metric(
            key="suspicious_recall",
            label="Suspicious recall (suspicious: true flagged suspicious)",
            outcomes={
                r.burst.id: r.flagged_suspicious for r in records if r.burst.labels.suspicious
            },
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
        ratio_metric(
            key="owner_language",
            label="Reasons and task titles in the owner's language and script (P2-O11)",
            outcomes={
                burst: bool(check.ok) for burst, check in languages.items() if check.ok is not None
            },
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
    ]
    # A run over part of the set is never a gate, even when the part happens to
    # hold every item of a metric's own set (a trial of all the positives, say).
    missing = len(everything) - len(records)
    metrics = [
        replace(m, pending=max(m.pending, missing)) if m.gated and missing else m for m in metrics
    ]
    breakdowns = {
        "urgent/decision recall by variant": _by_variant(positives, lambda r: [r.alerted]),
        "false alerts (alert: false among alerted) by variant": _by_variant(
            alerted, lambda r: [not r.burst.labels.alert]
        ),
        "resolution recall by variant": _by_variant(
            records, lambda r: [t in r.closed for t in r.burst.labels.resolved_tasks]
        ),
        "false closures by variant": _by_variant(
            records, lambda r: [t in r.closed for t in r.burst.labels.unresolved_tasks]
        ),
        "needs_reply agreement by variant": _by_variant(
            records, lambda r: [r.needs_reply == r.burst.labels.needs_reply]
        ),
        "commitment recall by side": {
            side: Measure(sum(flags), len(flags))
            for side in ("owner", "contact")
            if (
                flags := [
                    r.recalled(label.id) is not None
                    for r in records
                    for label in r.burst.labels.commitments
                    if label.by == side
                ]
            )
        },
        "owner language by variant": _by_variant(
            records,
            lambda r: [bool(c.ok)] if (c := languages[r.burst.id]).ok is not None else [],
        ),
    }
    notes = [
        "Pre-screen only until the owner confirms the bursts' labels (PRD R2 #5): the gate "
        "counts owner-confirmed bursts (`confirmed_items` = burst ids) and needs the whole set.",
        "Alerts are what the product's effects store enqueued (`notifications` rows, kinds "
        "`triage_urgent`/`triage_decision`); nothing was delivered.",
    ]
    failed = [f"{r.burst.id} ({r.status}:{r.error})" for r in records if not r.answered]
    if failed:
        notes.append(
            f"{len(failed)} burst(s) stored no verdict (a miss wherever a hit was needed): "
            + ", ".join(failed)
        )
    settled = [r.burst.id for r in records if r.status == TriageStatus.UP_TO_DATE.value]
    if settled:
        notes.append(
            "no model call (only the owner wrote and no task was open; the product's rule): "
            + ", ".join(settled)
        )
    undetermined = [
        burst
        for burst, check in languages.items()
        if check.ok is None and next(r for r in records if r.burst.id == burst).own_words()
    ]
    if undetermined:
        notes.append(
            "owner language not determined (too short to name; script right): "
            + ", ".join(undetermined)
        )
    return TriageReport(tuple(metrics), breakdowns, tuple(notes))


# ------------------------------------------------------------------ the target
@dataclass(frozen=True, slots=True)
class _SeededTask:
    dataset_id: str
    title: str
    sources: frozenset[int]


@dataclass(frozen=True, slots=True)
class _Loaded:
    account_id: int
    conversation_id: int
    #: Database message id -> dataset message id.
    refs: Mapping[int, str]
    #: Database task id -> the open task it seeds.
    tasks: Mapping[int, _SeededTask] = field(default_factory=dict)


class TriageTarget:
    name = "triage"
    dataset: DatasetName = "e3"
    route = ROUTE
    uses_model = True

    def __init__(
        self,
        bursts: Sequence[Burst],
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bursts = list(bursts)
        self._factory = session_factory
        #: Waits before an outage retry (injected: tests do not sleep).
        self._sleep = sleep

    @property
    def label(self) -> str:
        """What the report calls this run's mode."""
        return "e3-bursts"

    def bind(self, session_factory: async_sessionmaker[AsyncSession]) -> TriageTarget:
        """The same target over a database (the runner refuses and estimates first)."""
        return TriageTarget(self._bursts, session_factory=session_factory, sleep=self._sleep)

    @property
    def _database(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            raise RuntimeError("bind() a database before running the triage target")
        return self._factory

    def _selected(self, context: RunContext) -> list[Burst]:
        if context.only is not None:
            return self.named(context.only, limit=context.limit)
        if context.limit is None:
            return self._bursts
        if context.limit < 1:
            raise ValueError("limit must be at least 1")
        return self._bursts[: context.limit]

    def named(self, ids: Sequence[str], *, limit: int | None = None) -> list[Burst]:
        """The bursts `--only` names, in set order: always a trial, so at least
        one and fewer than all, and never with a limit."""
        if limit is not None:
            raise ValueError("only and limit cannot be combined")
        wanted = set(ids)
        unknown = sorted(wanted - {b.id for b in self._bursts})
        if unknown:
            raise ValueError(f"only names unknown bursts: {', '.join(unknown)}")
        if not wanted or len(wanted) >= len(self._bursts):
            raise ValueError("only must name at least one burst and not the whole set")
        return [b for b in self._bursts if b.id in wanted]

    # ------------------------------------------------------------- estimate
    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        """Every call a run can make, at its worst: the triage call and its one
        truncation retry at the larger budget (`Triager._call`), for each burst
        the product calls the model for."""
        config = TriageConfig(model="")  # the product's budgets, no model needed
        larger = min(config.max_output_tokens * 2, config.max_output_tokens_ceiling)
        calls: list[PlannedCall] = []
        for burst in self._selected(context):
            if not makes_a_call(burst):
                continue
            chars = self._triage_chars(burst)
            calls.append(PlannedCall(TRIAGE_PURPOSE, chars, config.max_output_tokens))
            if larger > config.max_output_tokens:
                calls.append(PlannedCall(TRIAGE_PURPOSE, chars, larger))
        return calls

    def _triage_chars(self, burst: Burst) -> int:
        numbered = list(enumerate(burst.messages(), start=1))
        sources = {message.id: _source(message, number) for number, message in numbered}
        history = [sources[m.id] for m in burst.history][-EARLIER_LIMIT:]
        new = [sources[m.id] for m in burst.burst]
        tasks = [
            TaskView(
                task_id=number,
                conversation_id=1,
                kind=task.kind,
                title=task.title,
                due_at=task.due,
                status="open",
                source_message_ids=(sources[task.source].message_id,),
                created_at=burst.now,
            )
            for number, task in enumerate(burst.open_tasks, start=1)
        ]
        built = build_triage(
            prompt=prompt_registry.active(RUN_TYPE),
            account=Account(timezone=burst.owner_timezone, locale_pref=burst.owner_language),
            contact_name=burst.contact.display_name,
            contact_priority=burst.contact_priority,
            notes=[],
            summary_overview=None,
            open_tasks=tasks,
            earlier=history,
            burst=new[-BURST_LIMIT:],
            omitted=max(0, len(new) - BURST_LIMIT),
            now=burst.now,
        )
        return (
            len(built.instructions) + sum(len(item.text) for item in built.items) + _ID_SLACK_CHARS
        )

    # ------------------------------------------------------------------ run
    async def run(self, context: RunContext) -> TargetResult:
        if context.llm is None or context.model is None:
            raise ValueError("the triage target needs a model client and a model id")
        processor = processor_for_model(context.model)
        if processor is None:
            raise ValueError("the model has no known processor to name in the synthetic consent")
        selected = self._selected(context)
        records: list[TriageRecord] = []
        not_evaluated: list[str] = []
        stopped: str | None = None
        for burst in selected:
            try:
                record = await self._attempts(burst, processor, context)
            except BudgetExceededError as stop:
                stopped = str(stop)
                break
            if record.outage:
                not_evaluated.append(burst.id)  # the provider, not the model
            else:
                records.append(record)
        return self._result(
            records,
            planned=len(selected),
            ledger=context.reviews,
            stopped=stopped,
            not_evaluated=not_evaluated,
        )

    async def _attempts(self, burst: Burst, processor: str, context: RunContext) -> TriageRecord:
        """One burst, run again after a provider outage like the agent mode
        (`MAX_ATTEMPTS`, `RETRY_DELAYS`): each attempt loads a fresh account
        and is billed. Still an outage after the last attempt: not evaluated."""
        failed: list[str] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            record = await self._evaluate(burst, processor, context)
            if not record.outage:
                return replace(record, attempts=attempt, failed_attempts=tuple(failed))
            failed.append(record.error or "outage")
            if attempt < MAX_ATTEMPTS:
                await self._sleep(RETRY_DELAYS[attempt - 1])
        return replace(record, attempts=MAX_ATTEMPTS, failed_attempts=tuple(failed))

    async def _evaluate(self, burst: Burst, processor: str, context: RunContext) -> TriageRecord:
        assert context.llm is not None
        assert context.model is not None
        loaded = await self._load(burst, processor)
        try:
            clock = _FixedClock(burst.now)
            triager = Triager(
                session_factory=self._database,
                llm=context.llm,
                clock=clock,
                config=TriageConfig(model=context.model, effort=context.effort),
                store=TriageRepository(clock=clock, scheduler=None),
            )
            outcome = await triager.triage(loaded.conversation_id)
            if outcome.status in _HARNESS_ERRORS:
                # The harness is mis-built, not the model's miss: nothing here is a result.
                raise RuntimeError(f"the triage run could not start ({outcome.status.value})")
            return await self._read_back(burst, loaded, outcome.status, outcome.calls)
        finally:
            await unload_corpus(self._database, loaded.account_id)

    async def _read_back(
        self, burst: Burst, loaded: _Loaded, status: TriageStatus, calls: int
    ) -> TriageRecord:
        """What the run left in the database: ids, enums, times, and the run's
        own short texts (reasons, titles)."""
        account_id = loaded.account_id
        async with self._database() as session:
            run = (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.account_id == account_id)
                    .where(AgentRun.run_type == RUN_TYPE)
                    .order_by(AgentRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            row = (
                await session.execute(
                    select(ConversationTriage)
                    .where(ConversationTriage.account_id == account_id)
                    .where(ConversationTriage.conversation_id == loaded.conversation_id)
                )
            ).scalar_one_or_none()
            alerts = (
                await session.execute(
                    select(Notification.kind)
                    .where(Notification.account_id == account_id)
                    .order_by(Notification.id)
                )
            ).scalars()
            alert_kinds = tuple(str(kind) for kind in alerts)
            task_rows = list(
                (
                    await session.execute(
                        select(Task).where(Task.account_id == account_id).order_by(Task.id)
                    )
                ).scalars()
            )
        error = None if status.value in ANSWERED or run is None else run.error_class
        stored = row is not None and run is not None and row.run_id == run.id
        verdict = (
            StoredVerdict(
                needs_reply=row.needs_reply,
                contains_question=row.contains_question,
                requests_decision=row.requests_decision,
                urgency=row.urgency,
                deadline_at=row.deadline_at,
                escalation_category=row.escalation_category,
                reasons=tuple(r for r in row.reasons or () if isinstance(r, str)),
                language=row.language,
                script=row.script,
            )
            if stored and row is not None
            else None
        )
        touched: list[TaskTouch] = []
        closed: list[str] = []
        for task in task_rows:
            sources = frozenset(int(i) for i in task.source_message_ids or ())
            seeded = loaded.tasks.get(task.id)
            if seeded is not None and task.closed_reason == task_kinds.RESOLVED_BY_TRIAGE:
                closed.append(seeded.dataset_id)
            if seeded is not None and task.title == seeded.title and sources == seeded.sources:
                continue  # untouched (or only closed)
            touched.append(
                TaskTouch(
                    kind=task.kind,
                    sources=tuple(loaded.refs.get(i, f"unknown:{i}") for i in sorted(sources)),
                    title=task.title,
                    due_at=task.due_at,
                    updated=None if seeded is None else seeded.dataset_id,
                )
            )
        return TriageRecord(
            burst=burst,
            status=status.value,
            error=error,
            calls=calls,
            verdict=verdict,
            alerts=alert_kinds,
            tasks=tuple(touched),
            closed=tuple(closed),
            outage=status is TriageStatus.FAILED and error in OUTAGE_ERRORS,
        )

    # -------------------------------------------------------------- the data
    async def _load(self, burst: Burst, processor: str) -> _Loaded:
        """The burst's account, chat, messages, open tasks and seed verdict
        (the module docstring, steps 1-3), in one transaction: a failure
        leaves nothing behind."""
        first = burst.messages()[0].sent_at
        async with self._database() as session, session.begin():
            account = Account(
                telegram_user_id=_SYNTHETIC_OWNER_ID + secrets.randbelow(900_000_000),
                status="active",
                timezone=burst.owner_timezone,
                locale_pref=burst.owner_language,
            )
            session.add(account)
            await session.flush()
            for kind in (CONSENT_AI_CONTROL, CONSENT_AI_MANAGED):
                await grant_consent(
                    session,
                    account,
                    kind=kind,
                    policy_version=EVAL_POLICY_VERSION,
                    processors=[processor],
                    clock=_FixedClock(first - timedelta(days=1)),
                )
            contact = Contact(
                account_id=account.id,
                telegram_user_id=_SYNTHETIC_CONTACT_ID,
                display_name=burst.contact.display_name,
                priority=burst.contact_priority,
            )
            session.add(contact)
            await session.flush()
            conversation = ConversationRow(
                account_id=account.id,
                business_chat_id=_SYNTHETIC_CONTACT_ID,
                contact_id=contact.id,
                mode="observe",
                coverage_started_at=first,
            )
            session.add(conversation)
            await session.flush()
            refs: dict[int, str] = {}
            ids: dict[str, int] = {}
            for index, message in enumerate(burst.messages(), start=1):
                stored = MessageRow(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    **message_values(message, index),
                )
                session.add(stored)
                await session.flush()
                refs[stored.id] = message.id
                ids[message.id] = stored.id
            # The history is already triaged: the burst is exactly what is new.
            seed_at = burst.burst[0].sent_at
            covers = ids[burst.history[-1].id] if burst.history else 0
            session.add(
                ConversationTriage(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    covers_through_message_id=covers,
                    needs_reply=False,
                    contains_question=False,
                    requests_decision=False,
                    urgency="normal",
                    escalation_category="none",
                    reasons=[],
                    prompt_version=SEED_LABEL,
                    model=SEED_LABEL,
                    created_at=seed_at,
                    updated_at=seed_at,
                )
            )
            seeded: dict[int, _SeededTask] = {}
            for task in burst.open_tasks:
                source = ids[task.source]
                created = burst.message(task.source).sent_at
                row = Task(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    kind=task.kind,
                    title=task.title,
                    due_at=task.due,
                    status="open",
                    source_message_ids=[source],
                    first_source_message_id=source,
                    created_by="agent",
                    created_at=created,
                    updated_at=created,
                )
                session.add(row)
                await session.flush()
                seeded[row.id] = _SeededTask(task.id, task.title, frozenset({source}))
            # Returned from inside the block: the transaction commits on the way out.
            return _Loaded(account.id, conversation.id, refs, seeded)

    # --------------------------------------------------------------- result
    def _result(
        self,
        records: Sequence[TriageRecord],
        *,
        planned: int,
        ledger: ReviewLedger,
        stopped: str | None,
        not_evaluated: Sequence[str],
    ) -> TargetResult:
        report = triage_metrics(records, self._bursts, ledger)
        notes = list(report.notes)
        retried = sum(r.calls > 1 for r in records)
        if retried:
            notes.append(f"{retried} triage call(s) were truncated once and retried larger.")
        rerun = [r.burst.id for r in records if r.attempts > 1]
        if rerun:
            notes.append(
                f"{len(rerun)} burst(s) were run again after a provider outage: " + ", ".join(rerun)
            )
        if not_evaluated:
            notes.append(
                f"INCOMPLETE: {len(not_evaluated)} burst(s) not evaluated (the provider stayed "
                f"unavailable through {MAX_ATTEMPTS} attempts; neither hits nor misses): "
                + ", ".join(not_evaluated)
            )
        if stopped:
            notes.append(f"STOPPED EARLY: {stopped}; {len(records)} of {planned} items ran")
        notes.append(
            "Model choice (IMPLEMENTATION_PLAN §7): Luna first; a tier that misses a threshold "
            "on the pre-screen is followed by the next (Sol, then Astra). "
            "`python -m evals compare <the route model's full run, then the next tiers' "
            "in the order tried>` states the decision."
        )
        return TargetResult(
            target=self.name,
            dataset=self.dataset,
            items=len(records),
            items_planned=planned,
            items_total=len(self._bursts),
            metrics=report.metrics,
            details=tuple(r.as_json() for r in records),
            breakdowns=report.breakdowns,
            notes=tuple(notes),
            prompts=(prompt_registry.active(RUN_TYPE).label,),
            not_evaluated=tuple(not_evaluated),
        )


@asynccontextmanager
async def isolated_triage(base_url: str, target: TriageTarget) -> AsyncIterator[TriageTarget]:
    """`target` over a migrated throwaway database (`assistant_eval_<random>` on
    the server of `base_url`), dropped on exit. Touches no existing database."""
    async with scratch_database(base_url) as url:
        engine = build_engine(url)
        try:
            yield target.bind(build_session_factory(engine))
        finally:
            await engine.dispose()


# ------------------------------------------------------------- model choice
@dataclass(frozen=True, slots=True)
class Comparison:
    """What one model comparison compares: the full runs of one target, whose
    route's model is `LLM_ROUTE_<ROUTE>_MODEL`, over the dataset files whose
    digests must agree (IMPLEMENTATION_PLAN §7: the `triage` and the
    `evaluate_monitor` routes)."""

    target: str
    route: str
    #: Manifest names of the files the runs measured; a name ending in "/" is a folder.
    datasets: tuple[str, ...]
    #: What the cost is given per 100 of.
    items: str

    @property
    def setting(self) -> str:
        """The setting a route change would use."""
        return f"LLM_ROUTE_{self.route.upper()}_MODEL"

    def measured(self, name: str) -> bool:
        return any(name.startswith(d) if d.endswith("/") else name == d for d in self.datasets)


#: The triage comparison: every E3 file counts (part 1, P2-T43).
TRIAGE_COMPARISON = Comparison(
    target=TriageTarget.name, route=ROUTE, datasets=("e3/",), items="bursts"
)


@dataclass(frozen=True, slots=True)
class TierResult:
    model: str
    #: Labels of the metrics with a threshold whose pre-screen did not pass.
    misses: tuple[str, ...]
    cost_per_100: Decimal | None

    @property
    def meets(self) -> bool:
        return not self.misses


@dataclass(frozen=True, slots=True)
class ModelChoice:
    tiers: tuple[TierResult, ...]
    decision: str
    comparison: Comparison = TRIAGE_COMPARISON


def _tier(report: Mapping[str, Any]) -> TierResult:
    """A tier misses every threshold its pre-screen did not pass: a gated one
    (triage) or a bar that is only the model choice's (the monitors')."""
    misses = tuple(
        str(m["label"])
        for m in report["metrics"]
        if m["threshold"] is not None and m["prescreen_status"] != "pass"
    )
    costs = [
        Decimal(row["cost_per_100_items_usd"])
        for row in report["cost"]["per_model"].values()
        if row["cost_per_100_items_usd"] is not None
    ]
    return TierResult(str(report["model"]), misses, sum(costs, Decimal(0)) if costs else None)


def _e3_files(report: Mapping[str, Any], comparison: Comparison) -> dict[str, str]:
    """The digests of the E3 files the comparison measured: only they decide
    whether two runs measured the same thing (another set landing between
    them does not)."""
    files = report.get("dataset_files") or {}
    e3 = {name: str(digest) for name, digest in files.items() if comparison.measured(name)}
    if not e3:
        raise ValueError(f"the {report.get('model')} run names no E3 dataset file")
    return e3


def _prompts(report: Mapping[str, Any]) -> list[str]:
    prompts = report.get("prompts")
    if not prompts:
        raise ValueError(f"the {report.get('model')} run names no prompt version")
    return sorted(str(p) for p in prompts)


def _comparable(reports: Sequence[Mapping[str, Any]], comparison: Comparison) -> None:
    """Runs that differ in anything but the model would credit that difference
    to the model: refuse them (`ValueError`)."""
    checks: tuple[tuple[str, Callable[[Mapping[str, Any]], object]], ...] = (
        ("E3 dataset versions", lambda r: _e3_files(r, comparison)),
        ("prompt versions", _prompts),
        ("reasoning efforts", lambda r: r.get("effort")),
    )
    for what, key in checks:
        if len({json.dumps(key(r), sort_keys=True) for r in reports}) > 1:
            raise ValueError(f"the runs were made with different {what}")


def model_choice(
    reports: Sequence[Mapping[str, Any]],
    *,
    route_model: str,
    tiers_total: int = 3,
    comparison: Comparison = TRIAGE_COMPARISON,
) -> ModelChoice:
    """The model-choice rule (IMPLEMENTATION_PLAN §7, WORKFLOW §11) over the
    full runs' JSON reports of the comparison's target (triage by default):
    the first is the run on `route_model`, **the route's current model**
    (from the configuration or named explicitly, never from the argument
    order), the rest the tiers tried after it, in order (Luna, Sol, Astra).
    The first tier that meets every threshold on the pre-screen is the
    choice; a tier that misses one calls for the next. Refuses (`ValueError`)
    a trial, an incomplete run, a run of another target, runs that differ in
    the measured E3 files, prompt versions or effort, a first run on another
    model than the route's, and a model run twice. The decision is a
    proposal: a route moves only with the owner's approval."""
    if not reports:
        raise ValueError("no report to decide on")
    for report in reports:
        if report.get("target") != comparison.target:
            raise ValueError(f"{report.get('target')!r} is not a {comparison.target} run")
        if not report.get("complete") or report.get("trial"):
            raise ValueError(f"the {report.get('model')} run is not a complete full run")
    _comparable(reports, comparison)
    if reports[0].get("model") != route_model:
        raise ValueError(
            f"the first report must be the run on the route's current model {route_model} "
            f"(got {reports[0].get('model')}); then the tiers in the order tried"
        )
    models = [str(report.get("model")) for report in reports]
    twice = sorted({model for model in models if models.count(model) > 1})
    if twice:
        raise ValueError(f"the runs name {', '.join(twice)} more than once")
    tiers = tuple(_tier(report) for report in reports)
    chosen = next((tier for tier in tiers if tier.meets), None)
    if chosen is not None and chosen is tiers[0]:
        decision = f"route stays on {chosen.model} (it meets every threshold on the pre-screen)"
    elif chosen is not None:
        decision = (
            f"propose {comparison.setting}={chosen.model}: the first tier tried that meets every "
            "threshold on the pre-screen; applied on the server only with the owner's approval"
        )
    elif len(tiers) < tiers_total:
        decision = (
            f"{tiers[-1].model} misses a threshold on the pre-screen: run the next tier "
            "(IMPLEMENTATION_PLAN §7: Sol after Luna, Astra after Sol)"
        )
    else:
        decision = "no tier tried meets every threshold: the owner decides"
    return ModelChoice(tiers, decision, comparison)


def render_choice(choice: ModelChoice) -> str:
    """The decision with each tier's quality and cost per 100 items."""
    comparison = choice.comparison
    lines = [
        "",
        f"Model choice ({comparison.route} route, pre-screen; the route's current model first, "
        "then the tiers in the order tried):",
        "",
    ]
    for tier in choice.tiers:
        cost = "n/a" if tier.cost_per_100 is None else f"${tier.cost_per_100:.4f}"
        result = "meets every threshold" if tier.meets else "misses " + "; ".join(tier.misses)
        lines.append(f"- {tier.model}: {result}; cost per 100 {comparison.items} {cost}")
    lines += ["", f"Decision: {choice.decision}", ""]
    return "\n".join(lines)


__all__ = [
    "ALERT_KINDS",
    "EVAL_POLICY_VERSION",
    "ROUTE",
    "SET_NAME",
    "TRIAGE_COMPARISON",
    "TRIAGE_PURPOSE",
    "Comparison",
    "ModelChoice",
    "StoredVerdict",
    "TaskTouch",
    "TierResult",
    "TriageRecord",
    "TriageTarget",
    "isolated_triage",
    "makes_a_call",
    "message_values",
    "model_choice",
    "rate_metric",
    "render_choice",
    "triage_metrics",
]
