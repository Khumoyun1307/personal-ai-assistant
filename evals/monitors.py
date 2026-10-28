"""The `evaluate_monitor` target (P2 spec E3 §B, part 2 of E3T, P2-T43): the
product's `MonitorEvaluator` over the E3 monitor cases.

`python -m evals run monitors` measures semantic-monitor evaluation **exactly as
the product runs it** (S05.4 §3.5): the real `MonitorEvaluator` (its context
builder, the active `evaluate_monitor` prompt, the structured `MonitorVerdicts`
schema, no tools, the deterministic validation) and its real effects (each
match recorded by `automation.monitors.record_match`, which enqueues one
`monitor_match` notification). Nothing is delivered: the evaluator gets no
scheduler, so no delivery job is deferred, and nothing in this stack can reach
Telegram.

Per case, in a **throwaway database**, under its own synthetic account (no case
sees another's chat, monitors or alerts):

1. the account: `ai_control` and `ai_managed` consents naming the processor of
   the run's model (the evaluator's own consent check needs them), the owner
   language of the case's variant (`OWNER_LANGUAGE`) and the E3 owner timezone;
2. a contact and its chat in Observe mode, the burst stored the way ingestion
   stores messages (received when sent), and the chat's monitor flag
   (`pending_monitor_at`) raised when its first contact message arrived, as
   ingestion raises it;
3. the case's monitors as the owner's confirmed semantic monitors of that chat
   (`automation.monitors.create`: standing, action `alert`), confirmed an hour
   before the burst so that every message of it counts;
4. `MonitorEvaluator.evaluate` with the route's model (or `--model`), the
   product's budgets and a clock fixed a minute after the newest message;
5. the `monitor_match` notifications are read back (which monitors fired, on
   which messages); then the account is deleted (every row cascades).

Metrics (spec E3 §B), per monitor (item `<case>.<monitor id>`, confirmed with
its case in `evals/reviews/e3.json`):

- **accuracy** (>= 90 %): the monitor fired exactly when labelled matching;
- **false-match rate** (<= 10 %): of the monitors that fired, the share
  labelled not matching (the noise the owner feels; the triage false-alert
  rate's reading);
- informational: match recall, negatives matched (labelled not matching that
  fired), per variant.

The two thresholds are the **bar for the model choice** (a TL decision, spec
E3 §B: informational), compared on the pre-screen; they are not an
owner-confirmed release gate (`gated=False`), so every gate reads `info`.

A model failure (refusal, invalid or truncated output) fires nothing: a miss
wherever a match was needed, and listed. A provider outage (`OUTAGE_ERRORS`) is
run again from a fresh account with the agent mode's bounded attempts and
delays; a case that never gets through is *not evaluated* (the run is then
incomplete). A budget stop ends the run early (incomplete).

The model comparison (IMPLEMENTATION_PLAN §7, WORKFLOW §11): `--model` runs the
target on another tier; `python -m evals compare` of the full runs' JSON
reports applies part 1's rule (`evals.triage.model_choice`) with
`MONITOR_COMPARISON`: the `evaluate_monitor` route, the monitor cases' digest,
cost per 100 cases.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.accounts.service import CONSENT_AI_CONTROL, CONSENT_AI_MANAGED, grant_consent
from assistant.agent import prompt_registry
from assistant.agent.models import AgentRun
from assistant.agent.monitor_eval import (
    BURST_LIMIT,
    EARLIER_LIMIT,
    RUN_TYPE,
    MonitorEvalConfig,
    MonitorEvalStatus,
    MonitorEvaluator,
    build_context,
)
from assistant.automation import monitors
from assistant.automation.monitors import MonitorView
from assistant.automation.rules import MonitorCondition, MonitorRule, MonitorScope
from assistant.conversations.models import Contact
from assistant.conversations.models import Conversation as ConversationRow
from assistant.conversations.models import Message as MessageRow
from assistant.core.model_routes import processor_for_model
from assistant.db.engine import build_engine, build_session_factory
from assistant.inbox.models import Notification
from assistant.inbox.notification_types import NotificationKind
from assistant.memory.summaries import SourceMessage
from evals.agent_mode import MAX_ATTEMPTS, OUTAGE_ERRORS, RETRY_DELAYS
from evals.budget import BudgetExceededError
from evals.corpus import scratch_database, unload_corpus
from evals.metrics import ratio_metric
from evals.review import ReviewLedger
from evals.schema import BurstMessage, MonitorCase, MonitorUnderTest
from evals.targets import (
    DatasetName,
    Measure,
    MetricResult,
    PlannedCall,
    RunContext,
    TargetResult,
)
from evals.triage import EVAL_POLICY_VERSION, Comparison, message_values, rate_metric

#: The run type whose model this target measures (`LLM_ROUTE_EVALUATE_MONITOR_MODEL`).
ROUTE = RUN_TYPE
#: The model-call purpose, as the requests carry it (the plan uses the same name).
MONITOR_PURPOSE = RUN_TYPE
#: Reports are `docs/evals/<date>-monitors-<model>.md` (spec E3 §B).
SET_NAME = "monitors"
#: The owner's language for a case: its variant's; in E3 v1 the owner of a mixed
#: Uzbek-Russian chat writes the monitor descriptions in Russian.
OWNER_LANGUAGE: Mapping[str, str] = {
    "uz-Latn": "uz-Latn",
    "uz-Cyrl": "uz-Cyrl",
    "ru": "ru",
    "en": "en",
    "uz-ru-mix": "ru",
}
#: The E3 owner's timezone (the bursts'): local times in the context use it.
OWNER_TIMEZONE = "Asia/Tashkent"
#: Monitors are confirmed this long before a case's first message, so that
#: every message of the burst counts (`MonitorView.covers`).
CONFIRMED_BEFORE = timedelta(hours=1)
#: The evaluation runs this long after the newest message (the product runs it
#: after a short trailing debounce).
EVALUATED_AFTER = timedelta(minutes=1)
#: The bar for the model choice (spec E3 §B, TL decision; informational).
ACCURACY_THRESHOLD = 0.90
FALSE_MATCH_THRESHOLD = 0.10
#: The statuses that are the product's own answer: an evaluation, or nothing to
#: evaluate (no contact message in the burst makes no call).
ANSWERED = frozenset({MonitorEvalStatus.EVALUATED.value, MonitorEvalStatus.UP_TO_DATE.value})
#: Statuses that mean the harness is mis-built, not that the model missed.
_HARNESS_ERRORS = frozenset(
    {
        MonitorEvalStatus.NO_CONSENT,
        MonitorEvalStatus.NOT_FOUND,
        MonitorEvalStatus.SKIPPED,
        MonitorEvalStatus.OVER_BUDGET,
    }
)
#: Message and monitor ids in the real database have more digits than the
#: plan's: slack for the pre-run estimate only.
_ID_SLACK_CHARS = 300
_SYNTHETIC_OWNER_ID = 7_000_000_096
_SYNTHETIC_CONTACT_ID = 3_000_002
_BAR_NOTE = (
    "The bar for the model choice (spec E3 §B, informational): compared on the pre-screen, "
    "not an owner-confirmed gate."
)

#: The comparison `python -m evals compare` applies to full monitor runs: only
#: the monitor cases' digest must agree (the bursts do not matter here).
MONITOR_COMPARISON = Comparison(
    target=SET_NAME, route=ROUTE, datasets=("e3/monitor_cases.jsonl",), items="cases"
)


class _FixedClock:
    """A fixed moment: reproducible confirmations, runs and alerts."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


# ------------------------------------------------------------------ the case
def makes_a_call(case: MonitorCase) -> bool:
    """Whether the product calls the model for this case: the evaluator reads
    contact messages only, so a burst without one has nothing to check."""
    return any(m.direction == "contact" for m in case.burst)


def first_contact_at(case: MonitorCase) -> datetime | None:
    return min((m.sent_at for m in case.burst if m.direction == "contact"), default=None)


def confirmed_at(case: MonitorCase) -> datetime:
    return min(m.sent_at for m in case.burst) - CONFIRMED_BEFORE


def evaluated_at(case: MonitorCase) -> datetime:
    return max(m.sent_at for m in case.burst) + EVALUATED_AFTER


def monitor_rule(monitor: MonitorUnderTest, conversation_id: int) -> MonitorRule:
    """The case's monitor as the owner's confirmed rule for that chat."""
    return MonitorRule(
        scope=MonitorScope(conversation_ids=(conversation_id,)),
        condition=MonitorCondition(kind=monitor.kind, description=monitor.description),
        action="alert",
        lifecycle="standing",
    )


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
class MonitorRecord:
    case: MonitorCase
    #: The `MonitorEvalStatus` value.
    status: str
    #: The run's error class; None when it answered.
    error: str | None
    #: Model calls made (a truncated answer is retried once, larger).
    calls: int
    #: Dataset monitor id -> the dataset ids of the messages its alert cites,
    #: for the monitors that fired.
    fired: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    #: The provider was unavailable: neither a hit nor a miss.
    outage: bool = False
    #: Runs of this case, the first included (an outage is run again).
    attempts: int = 1
    #: The error class of each attempt that failed on the provider.
    failed_attempts: tuple[str, ...] = ()

    @property
    def answered(self) -> bool:
        return self.status in ANSWERED

    def verdicts(self) -> dict[str, tuple[bool, bool]]:
        """Per monitor (`<case>.<monitor id>`): (labelled matching, fired)."""
        labelled = set(self.case.labels.matched_monitor_ids)
        return {
            f"{self.case.id}.{m.id}": (m.id in labelled, m.id in self.fired)
            for m in self.case.monitors
        }

    def as_json(self) -> dict[str, Any]:
        """One record for the JSON report: ids, statuses and booleans only."""
        labelled = set(self.case.labels.matched_monitor_ids)
        return {
            "id": self.case.id,
            "variant": self.case.variant,
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "failed_attempts": list(self.failed_attempts),
            "calls": self.calls,
            "monitors": [
                {
                    "id": m.id,
                    "label_matched": m.id in labelled,
                    "fired": m.id in self.fired,
                    "cited": list(self.fired.get(m.id, ())),
                }
                for m in self.case.monitors
            ],
        }


# ------------------------------------------------------------------ metrics
class MonitorReport(NamedTuple):
    metrics: tuple[MetricResult, ...]
    breakdowns: dict[str, dict[str, Measure]]
    notes: tuple[str, ...]


def _by_variant(
    records: Sequence[MonitorRecord], outcome: Callable[[bool, bool], bool | None]
) -> dict[str, Measure]:
    """`outcome(labelled, fired)` per monitor; None leaves the monitor out."""
    grouped: dict[str, list[bool]] = {}
    for record in records:
        for labelled, fired in record.verdicts().values():
            value = outcome(labelled, fired)
            if value is not None:
                grouped.setdefault(record.case.variant, []).append(value)
    return {
        variant: Measure(sum(flags), len(flags))
        for variant, flags in sorted(grouped.items())
        if flags
    }


def monitor_metrics(
    records: Sequence[MonitorRecord], everything: Sequence[MonitorCase], ledger: ReviewLedger
) -> MonitorReport:
    """Every metric of spec E3 §B for `evaluate_monitor`, per monitor."""
    pairs = {pair: verdict for r in records for pair, verdict in r.verdicts().items()}
    metrics = (
        ratio_metric(
            key="monitor_accuracy",
            label="Monitor accuracy (fired exactly when labelled matching)",
            outcomes={pair: labelled == fired for pair, (labelled, fired) in pairs.items()},
            threshold=ACCURACY_THRESHOLD,
            ledger=ledger,
            set_size=sum(len(c.monitors) for c in everything),
            gated=False,
            note=_BAR_NOTE,
        ),
        rate_metric(
            key="false_match_rate",
            label="False-match rate (labelled not matching among the monitors that fired)",
            bad={pair: not labelled for pair, (labelled, fired) in pairs.items() if fired},
            threshold=FALSE_MATCH_THRESHOLD,
            ledger=ledger,
            gated=False,
            note=_BAR_NOTE,
        ),
        ratio_metric(
            key="match_recall",
            label="Match recall (labelled matching that fired)",
            outcomes={pair: fired for pair, (labelled, fired) in pairs.items() if labelled},
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
        rate_metric(
            key="negatives_matched",
            label="Negatives matched (labelled not matching that fired)",
            bad={pair: fired for pair, (labelled, fired) in pairs.items() if not labelled},
            threshold=None,
            ledger=ledger,
            gated=False,
        ),
    )
    breakdowns = {
        "monitor accuracy by variant": _by_variant(records, lambda label, fired: label == fired),
        "false matches (labelled not matching among fired) by variant": _by_variant(
            records, lambda label, fired: (not label) if fired else None
        ),
        "match recall by variant": _by_variant(
            records, lambda label, fired: fired if label else None
        ),
    }
    notes = [
        "Accuracy >= 90 % and false-match rate <= 10 % are the bar for the model choice (TL "
        "decision, spec E3 §B: informational), compared on the pre-screen; no gate. "
        "Owner-confirmed cases (`confirmed_items` = case ids) are counted for information.",
        "A monitor fired when the product's effects enqueued its `monitor_match` notification "
        "(one evaluate_monitor call per case for all its monitors); nothing was delivered.",
    ]
    failed = [f"{r.case.id} ({r.status}:{r.error})" for r in records if not r.answered]
    if failed:
        notes.append(
            f"{len(failed)} case(s) got no usable answer (nothing fired: a miss wherever a "
            "match was needed): " + ", ".join(failed)
        )
    settled = [r.case.id for r in records if r.status == MonitorEvalStatus.UP_TO_DATE.value]
    if settled:
        notes.append(
            "no model call (no contact message in the burst; the product's rule): "
            + ", ".join(settled)
        )
    return MonitorReport(metrics, breakdowns, tuple(notes))


# ------------------------------------------------------------------ the target
@dataclass(frozen=True, slots=True)
class _Loaded:
    account_id: int
    conversation_id: int
    #: Database message id -> dataset message id.
    refs: Mapping[int, str]
    #: Automation id -> dataset monitor id.
    monitors: Mapping[int, int]


class MonitorTarget:
    name = SET_NAME
    dataset: DatasetName = "e3"
    route = ROUTE
    uses_model = True

    def __init__(
        self,
        cases: Sequence[MonitorCase],
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cases = list(cases)
        self._factory = session_factory
        #: Waits before an outage retry (injected: tests do not sleep).
        self._sleep = sleep

    @property
    def label(self) -> str:
        """What the report calls this run's mode."""
        return "e3-monitor-cases"

    def bind(self, session_factory: async_sessionmaker[AsyncSession]) -> MonitorTarget:
        """The same target over a database (the runner refuses and estimates first)."""
        return MonitorTarget(self._cases, session_factory=session_factory, sleep=self._sleep)

    @property
    def _database(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            raise RuntimeError("bind() a database before running the monitor target")
        return self._factory

    def _selected(self, context: RunContext) -> list[MonitorCase]:
        if context.only is not None:
            return self.named(context.only, limit=context.limit)
        if context.limit is None:
            return self._cases
        if context.limit < 1:
            raise ValueError("limit must be at least 1")
        return self._cases[: context.limit]

    def named(self, ids: Sequence[str], *, limit: int | None = None) -> list[MonitorCase]:
        """The cases `--only` names, in set order: always a trial, so at least
        one and fewer than all, and never with a limit."""
        if limit is not None:
            raise ValueError("only and limit cannot be combined")
        wanted = set(ids)
        unknown = sorted(wanted - {c.id for c in self._cases})
        if unknown:
            raise ValueError(f"only names unknown cases: {', '.join(unknown)}")
        if not wanted or len(wanted) >= len(self._cases):
            raise ValueError("only must name at least one case and not the whole set")
        return [c for c in self._cases if c.id in wanted]

    # ------------------------------------------------------------- estimate
    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        """Every call a run can make, at its worst: the one evaluate_monitor
        call for all the case's monitors and its truncation retry at the
        larger budget (`MonitorEvaluator._call`), for each case the product
        calls the model for."""
        config = MonitorEvalConfig(model="")  # the product's budgets, no model needed
        larger = min(config.max_output_tokens * 2, config.max_output_tokens_ceiling)
        calls: list[PlannedCall] = []
        for case in self._selected(context):
            if not makes_a_call(case):
                continue
            chars = self._context_chars(case)
            calls.append(PlannedCall(MONITOR_PURPOSE, chars, config.max_output_tokens))
            if larger > config.max_output_tokens:
                calls.append(PlannedCall(MONITOR_PURPOSE, chars, larger))
        return calls

    def _context_chars(self, case: MonitorCase) -> int:
        """The product's context for the case, with stand-in ids: the
        messages before the first contact message are the earlier ones, the
        contact messages the burst (`monitor_eval.load_burst`)."""
        confirmed = confirmed_at(case)
        views = [
            MonitorView(
                automation_id=number,
                account_id=1,
                status="active",
                rule=monitor_rule(monitor, conversation_id=1),
                confirmed_at=confirmed,
                expires_at=None,
                created_at=confirmed,
            )
            for number, monitor in enumerate(case.monitors, start=1)
        ]
        sources = [_source(m, n) for n, m in enumerate(case.burst, start=1)]
        first = next(i for i, m in enumerate(case.burst) if m.direction == "contact")
        new = [s for s in sources if s.direction == "contact"]
        built = build_context(
            prompt=prompt_registry.active(RUN_TYPE),
            account=Account(timezone=OWNER_TIMEZONE, locale_pref=OWNER_LANGUAGE[case.variant]),
            contact_name=None,
            views=views,
            earlier=sources[:first][-EARLIER_LIMIT:],
            burst=new[-BURST_LIMIT:],
            omitted=max(0, len(new) - BURST_LIMIT),
            now=evaluated_at(case),
        )
        return (
            len(built.instructions) + sum(len(item.text) for item in built.items) + _ID_SLACK_CHARS
        )

    # ------------------------------------------------------------------ run
    async def run(self, context: RunContext) -> TargetResult:
        if context.llm is None or context.model is None:
            raise ValueError("the monitor target needs a model client and a model id")
        processor = processor_for_model(context.model)
        if processor is None:
            raise ValueError("the model has no known processor to name in the synthetic consent")
        selected = self._selected(context)
        records: list[MonitorRecord] = []
        not_evaluated: list[str] = []
        stopped: str | None = None
        for case in selected:
            try:
                record = await self._attempts(case, processor, context)
            except BudgetExceededError as stop:
                stopped = str(stop)
                break
            if record.outage:
                not_evaluated.append(case.id)  # the provider, not the model
            else:
                records.append(record)
        return self._result(
            records,
            planned=len(selected),
            ledger=context.reviews,
            stopped=stopped,
            not_evaluated=not_evaluated,
        )

    async def _attempts(
        self, case: MonitorCase, processor: str, context: RunContext
    ) -> MonitorRecord:
        """One case, run again after a provider outage like the agent mode
        (`MAX_ATTEMPTS`, `RETRY_DELAYS`): each attempt loads a fresh account
        and is billed. Still an outage after the last attempt: not evaluated."""
        failed: list[str] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            record = await self._evaluate(case, processor, context)
            if not record.outage:
                return replace(record, attempts=attempt, failed_attempts=tuple(failed))
            failed.append(record.error or "outage")
            if attempt < MAX_ATTEMPTS:
                await self._sleep(RETRY_DELAYS[attempt - 1])
        return replace(record, attempts=MAX_ATTEMPTS, failed_attempts=tuple(failed))

    async def _evaluate(
        self, case: MonitorCase, processor: str, context: RunContext
    ) -> MonitorRecord:
        assert context.llm is not None
        assert context.model is not None
        loaded = await self._load(case, processor)
        try:
            evaluator = MonitorEvaluator(
                session_factory=self._database,
                llm=context.llm,
                clock=_FixedClock(evaluated_at(case)),
                config=MonitorEvalConfig(model=context.model, effort=context.effort),
                scheduler=None,
            )
            outcome = await evaluator.evaluate(loaded.conversation_id)
            if outcome.status in _HARNESS_ERRORS:
                # The harness is mis-built, not the model's miss: nothing here is a result.
                raise RuntimeError(
                    f"the monitor evaluation could not start ({outcome.status.value})"
                )
            return await self._read_back(case, loaded, outcome.status, outcome.calls)
        finally:
            await unload_corpus(self._database, loaded.account_id)

    async def _read_back(
        self, case: MonitorCase, loaded: _Loaded, status: MonitorEvalStatus, calls: int
    ) -> MonitorRecord:
        """What the run left in the database: the run's status and the
        `monitor_match` alerts (ids only)."""
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
            alerts = (
                await session.execute(
                    select(Notification.refs)
                    .where(Notification.account_id == account_id)
                    .where(Notification.kind == NotificationKind.MONITOR_MATCH.value)
                    .order_by(Notification.id)
                )
            ).scalars()
            refs = [dict(r or {}) for r in alerts]
        error = None if status.value in ANSWERED or run is None else run.error_class
        fired: dict[int, tuple[str, ...]] = {}
        for alert in refs:
            cited = tuple(
                loaded.refs.get(int(i), f"unknown:{i}") for i in alert.get("message_ids", ())
            )
            for automation_id in alert.get("automation_ids", ()):
                fired[loaded.monitors[int(automation_id)]] = cited
        return MonitorRecord(
            case=case,
            status=status.value,
            error=error,
            calls=calls,
            fired=fired,
            outage=status is MonitorEvalStatus.FAILED and error in OUTAGE_ERRORS,
        )

    # -------------------------------------------------------------- the data
    async def _load(self, case: MonitorCase, processor: str) -> _Loaded:
        """The case's account, chat, messages and monitors (the module
        docstring, steps 1-3), in one transaction: a failure leaves nothing
        behind."""
        confirmed = confirmed_at(case)
        async with self._database() as session, session.begin():
            account = Account(
                telegram_user_id=_SYNTHETIC_OWNER_ID + secrets.randbelow(900_000_000),
                status="active",
                timezone=OWNER_TIMEZONE,
                locale_pref=OWNER_LANGUAGE[case.variant],
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
                    clock=_FixedClock(confirmed - timedelta(days=1)),
                )
            contact = Contact(account_id=account.id, telegram_user_id=_SYNTHETIC_CONTACT_ID)
            session.add(contact)
            await session.flush()
            conversation = ConversationRow(
                account_id=account.id,
                business_chat_id=_SYNTHETIC_CONTACT_ID,
                contact_id=contact.id,
                mode="observe",
                coverage_started_at=confirmed,
                # Ingestion raises the flag when the first contact message arrives.
                pending_monitor_at=first_contact_at(case),
            )
            session.add(conversation)
            await session.flush()
            refs: dict[int, str] = {}
            for index, message in enumerate(case.burst, start=1):
                stored = MessageRow(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    # Received when sent: the burst query reads receipt time.
                    created_at=message.sent_at,
                    **message_values(message, index),
                )
                session.add(stored)
                await session.flush()
                refs[stored.id] = message.id
            ids: dict[int, int] = {}
            for monitor in case.monitors:
                row, _ = await monitors.create(
                    session,
                    account_id=account.id,
                    rule=monitor_rule(monitor, conversation.id),
                    expires_at=None,
                    timezone=OWNER_TIMEZONE,
                    clock=_FixedClock(confirmed),
                    run_id=None,
                    tool_call_id=None,
                )
                ids[row.id] = monitor.id
            # Returned from inside the block: the transaction commits on the way out.
            return _Loaded(account.id, conversation.id, refs, ids)

    # --------------------------------------------------------------- result
    def _result(
        self,
        records: Sequence[MonitorRecord],
        *,
        planned: int,
        ledger: ReviewLedger,
        stopped: str | None,
        not_evaluated: Sequence[str],
    ) -> TargetResult:
        report = monitor_metrics(records, self._cases, ledger)
        notes = list(report.notes)
        retried = sum(r.calls > 1 for r in records)
        if retried:
            notes.append(
                f"{retried} evaluate_monitor call(s) were truncated once and retried larger."
            )
        rerun = [r.case.id for r in records if r.attempts > 1]
        if rerun:
            notes.append(
                f"{len(rerun)} case(s) were run again after a provider outage: " + ", ".join(rerun)
            )
        if not_evaluated:
            notes.append(
                f"INCOMPLETE: {len(not_evaluated)} case(s) not evaluated (the provider stayed "
                f"unavailable through {MAX_ATTEMPTS} attempts; neither hits nor misses): "
                + ", ".join(not_evaluated)
            )
        if stopped:
            notes.append(f"STOPPED EARLY: {stopped}; {len(records)} of {planned} items ran")
        notes.append(
            "Model choice (IMPLEMENTATION_PLAN §7): Luna first; a tier that misses the bar on "
            "the pre-screen is followed by the next (Sol, then Astra). "
            "`python -m evals compare <the route model's full monitors run, then the next tiers' "
            "in the order tried>` states the decision for the evaluate_monitor route."
        )
        return TargetResult(
            target=self.name,
            dataset=self.dataset,
            items=len(records),
            items_planned=planned,
            items_total=len(self._cases),
            metrics=report.metrics,
            details=tuple(r.as_json() for r in records),
            breakdowns=report.breakdowns,
            notes=tuple(notes),
            prompts=(prompt_registry.active(RUN_TYPE).label,),
            not_evaluated=tuple(not_evaluated),
        )


@asynccontextmanager
async def isolated_monitors(base_url: str, target: MonitorTarget) -> AsyncIterator[MonitorTarget]:
    """`target` over a migrated throwaway database (`assistant_eval_<random>` on
    the server of `base_url`), dropped on exit. Touches no existing database."""
    async with scratch_database(base_url) as url:
        engine = build_engine(url)
        try:
            yield target.bind(build_session_factory(engine))
        finally:
            await engine.dispose()


__all__ = [
    "MONITOR_COMPARISON",
    "MONITOR_PURPOSE",
    "OWNER_LANGUAGE",
    "ROUTE",
    "SET_NAME",
    "MonitorRecord",
    "MonitorTarget",
    "isolated_monitors",
    "makes_a_call",
    "monitor_metrics",
]
