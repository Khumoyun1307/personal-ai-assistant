"""The summary target (S03.4): the real S03.2 catch-up path over the E1 set.

For each E1 conversation, in a **throwaway database**, under its own synthetic
account (so one chat's coverage facts can never colour another's):

1. the conversation is stored as the product stores messages (`corpus.load_corpus`:
   current text, tombstones for deletions, never `original_text`/`purged_text`);
2. synthetic rows the product needs are added: an `ai_control` and an
   `ai_managed` consent naming the processor of the run's model, an active
   business connection (dated at `coverage_started_at` where the dataset has one),
   and a `coverage_gaps` row for every gap the dataset labels (the `delayed` one
   included: it must not be reported);
3. the **real** `Summarizer.summarize_period` runs over the chat's whole range:
   the product's active summarize prompt (`summarize.vN` from the prompt registry),
   schema, output guard and validation. The consent wrapper is not in the
   evaluation stack (E-j), but the summarizer's own consent check is, which is why
   the consent rows exist;
4. the product's coverage renderer (`memory.catch_up.period_coverage` +
   `caveats`) is asked about the same range and the same chat. Which gap ids and
   whether a pre-connection note it carried become the **structured** caveats of
   the metric (E-d). They come from the application, not from the model (E-p);
5. the metrics of `evals/metrics.py` and the judges of `evals/judge.py` score the
   result. Only messages, never labels, reach the summarizer; the judges see the
   labels' traps by design (E-c).

The catch-up window is fixed (E-n): from one hour before the earliest of the
first message, the coverage start and the first gap, until the evaluation instant
(the last message plus a day, as in the retrieval target).

Model failures (provider error, refusal, truncation twice, invalid output) make
the conversation a **miss** on open-loop recall (every loop, whatever the ledger
says) and on the language check, and leave it **unscreened** for fabrication (that
gate stays open); the conversation is listed. Its caveats are still reported, being
deterministic. The two caveat metrics are an **ungated** wiring check (E-p): they do
not depend on the model. A budget stop ends the run early and the result is marked
incomplete (its gate is never a pass).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, BusinessConnection
from assistant.accounts.service import CONSENT_AI_CONTROL, CONSENT_AI_MANAGED, grant_consent
from assistant.agent import prompt_registry
from assistant.agent.attribution import (
    MAX_NAME,
    NO_CHANGES,
    WHO_TEMPLATES,
    AttributionCounts,
    render_who,
)
from assistant.agent.context import build_summarize
from assistant.agent.models import AgentRun
from assistant.agent.schemas import (
    MAX_DUE,
    MAX_ITEM_TEXT,
    MAX_ITEMS,
    MAX_OVERVIEW,
    MAX_REASON,
)
from assistant.agent.summarize import PeriodOutcome, SummarizeConfig, Summarizer, SummaryStatus
from assistant.conversations.models import Conversation as ConversationRow
from assistant.conversations.models import CoverageGap
from assistant.core.model_routes import processor_for_model
from assistant.db.engine import build_engine, build_session_factory
from assistant.memory.catch_up import GapFact, PeriodCoverage, caveats, period_coverage
from assistant.memory.summaries import SourceMessage
from evals.budget import BudgetExceededError
from evals.corpus import (
    NOW_AFTER_CORPUS,
    OWNER_TIMEZONE,
    load_corpus,
    scratch_database,
    unload_corpus,
)
from evals.judge import FABRICATION_LABEL as FABRICATION_RUBRIC
from evals.judge import LOOPS_RUBRIC_ID as LOOPS_RUBRIC
from evals.judge import MAX_OUTPUT_TOKENS as JUDGE_MAX_OUTPUT_TOKENS
from evals.judge import (
    LLMFabricationJudge,
    LLMOpenLoopJudge,
    ScreenResult,
    fabrication_metric,
    loops_input_chars,
    screen_input_chars,
)
from evals.metrics import (
    CaveatCheck,
    LanguageVerdict,
    OwnerLanguage,
    SummaryOutput,
    caveat_metrics,
    check_caveats,
    check_language,
    language_metric,
    open_loop_coverage,
    open_loops,
    recall_metric,
)
from evals.review import ReviewLedger, coverage_key
from evals.schema import Conversation
from evals.targets import (
    DatasetName,
    Measure,
    PlannedCall,
    RunContext,
    TargetResult,
)

#: The run type whose model this target measures (`LLM_ROUTE_SUMMARIZE_MODEL`).
ROUTE = "summarize"
#: Model-call purposes, as the requests carry them (the plan uses the same names).
SUMMARIZE_PURPOSE = "summarize"
FABRICATION_PURPOSE = "eval_fabrication_screen"
LOOPS_PURPOSE = "eval_open_loop_screen"
#: The catch-up starts this long before the earliest known moment (E-n).
WINDOW_LEAD = timedelta(hours=1)
#: A consent version label that cannot be mistaken for a real policy version.
EVAL_POLICY_VERSION = "eval-synthetic"
_EVAL_OWNER_CHAT_ID = 7_000_000_098
#: Message ids in the real database have more digits than the plan's: slack for
#: the pre-run estimate only.
_ID_SLACK_CHARS = 500

#: The longest `who` line `labelled_text` can write: the longest English template
#: with the longest name, in every slot it has.
_MAX_WHO_CHARS = max(
    len(template.replace("{name}", "")) + template.count("{name}") * MAX_NAME
    for template in WHO_TEMPLATES["en"].values()
)
#: The longest summary text `sanitize` can store (every list at its cap, every
#: text at its limit) plus the labels of `labelled_text`: an upper bound on what a
#: judge can be asked to read, for the pre-run estimate.
MAX_SUMMARY_CHARS = (
    MAX_OVERVIEW
    + 3 * MAX_ITEMS * (MAX_ITEM_TEXT + 4)
    + MAX_ITEMS * (MAX_ITEM_TEXT + MAX_DUE + 40)
    + 3 * MAX_ITEMS * (_MAX_WHO_CHARS + 2)
    + MAX_REASON
    + 300
)

#: Why the caveat metrics carry no gate (E-p).
CAVEAT_NOTE = (
    "Ungated wiring check: the plan's caveat threshold (the final reply keeps every "
    "caveat, D64) is not measured by this target; see E-p."
)

GapKey = tuple[str, str, datetime, datetime, int | None]
CoverageReader = Callable[[AsyncSession, int, int, datetime, datetime], Awaitable[PeriodCoverage]]


class _FixedClock:
    """The evaluation instant: reproducible `agent_runs` and summary times."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


# ------------------------------------------------------------- the conversation
def evaluation_instant(conversation: Conversation) -> datetime:
    """ "Now" for this chat: a day after its last message."""
    return conversation.messages[-1].sent_at + NOW_AFTER_CORPUS


def catch_up_window(conversation: Conversation) -> tuple[datetime, datetime]:
    """The range the owner is taken to ask about (E-n): everything the chat and
    its coverage facts mention, with an hour of lead so that a connection
    starting inside the chat's story falls inside the range."""
    moments = [conversation.messages[0].sent_at]
    if conversation.coverage_started_at is not None:
        moments.append(conversation.coverage_started_at)
    moments += [gap.at_risk_from for gap in conversation.coverage_gaps]
    return min(moments) - WINDOW_LEAD, evaluation_instant(conversation)


def _source_messages(conversation: Conversation) -> list[SourceMessage]:
    """The retained messages as the summarizer reads them, for the estimate only
    (the run reads the database). A deleted message is a tombstone: not read."""
    return [
        SourceMessage(
            message_id=1000 + number,
            direction="contact" if message.direction == "contact" else "owner_manual",
            sent_at=message.sent_at,
            content_type=message.media or "text",
            text=message.text,
            media_kind=message.media,
            edited_at=message.edited_at,
        )
        for number, message in enumerate(conversation.messages, start=1)
        if not message.deleted
    ]


# ------------------------------------------------------------ summary rendering
def plain_text(summary: Mapping[str, Any]) -> str:
    """The summary's own words, nothing else: what the deterministic checks
    (values, owner language and script) read. Application-written text is not
    in it."""
    parts: list[str] = [str(summary.get("overview") or "")]
    for point in summary.get("open_questions_to_owner") or []:
        parts.append(point["text"])
    for item in summary.get("commitments") or []:
        parts.append(f"{item['text']} {item['due']}" if item.get("due") else item["text"])
    for key in ("decisions_requested", "key_facts"):
        for point in summary.get(key) or []:
            parts.append(point["text"])
    if summary.get("needs_reply_reason"):
        parts.append(str(summary["needs_reply_reason"]))
    return "\n".join(part for part in parts if part)


def labelled_text(summary: Mapping[str, Any], *, contact_name: str | None) -> str:
    """The summary with its structure named, for the judges (who must know that
    an item is a commitment of the owner, say). Who asked, promised or agreed is
    written exactly as the product words it for the owner (`render_who`, English,
    with the contact's name; `judge.LABELS_VERSION`). English labels: never used
    for the language check."""

    def line(key: str, item: Mapping[str, Any]) -> str:
        who = render_who(key, item, contact_name, "en")
        return f"- {who}: {item['text']}" if who else f"- {item['text']}"

    def points(title: str, key: str) -> list[str]:
        items = summary.get(key) or []
        return [title, *(line(key, item) for item in items)] if items else [title, "- (none)"]

    commitments = summary.get("commitments") or []
    lines = [f"Overview: {summary.get('overview') or ''}"]
    lines += points("Open questions to the owner:", "open_questions_to_owner")
    lines.append("Commitments not yet done:")
    lines += [
        line("commitments", item) + (f" (due: {item['due']})" if item.get("due") else "")
        for item in commitments
    ] or ["- (none)"]
    lines += points("Decisions requested from the owner:", "decisions_requested")
    lines += points("Key facts:", "key_facts")
    reply = "yes" if summary.get("needs_reply") else "no"
    reason = f" - {summary['needs_reply_reason']}" if summary.get("needs_reply_reason") else ""
    lines.append(f"Needs a reply from the owner: {reply}{reason}")
    return "\n".join(lines)


def _item_counts(summary: Mapping[str, Any]) -> dict[str, int]:
    return {
        key: len(summary.get(key) or [])
        for key in ("open_questions_to_owner", "commitments", "decisions_requested", "key_facts")
    }


# --------------------------------------------------------------------- caveats
@dataclass(frozen=True, slots=True)
class CaveatFacts:
    """Which caveats the product's renderer carried for one chat's catch-up."""

    gap_ids: frozenset[str]
    pre_connection: bool
    #: Every caveat line the renderer produced (gaps, coverage start and the
    #: counts of deleted/expired messages, paused/ignored chats).
    lines: int


def gap_key(fact: GapFact) -> GapKey:
    return (fact.kind, fact.certainty, fact.at_risk_from, fact.at_risk_to, fact.conversation_id)


def carried_caveats(
    coverage: PeriodCoverage, gap_ids: Mapping[GapKey, str], timezone: str
) -> CaveatFacts:
    """Which dataset gaps (by id) and whether the pre-connection note appear in
    `caveats(coverage)`, read from the renderer's own output: a gap counts only
    if the line it alone produces is among the lines rendered (so a `delayed`
    gap, which `caveats` skips, is never carried)."""
    rendered = Counter(caveats(coverage, timezone))
    base = Counter(caveats(replace(coverage, gaps=()), timezone))
    carried: set[str] = set()
    for number, fact in enumerate(coverage.gaps, start=1):
        own = Counter(caveats(replace(coverage, gaps=(fact,)), timezone)) - base
        if own and not own - rendered:
            carried.add(gap_ids.get(gap_key(fact)) or f"unknown-gap-{number}")
    only_start = replace(
        coverage,
        gaps=(),
        connection_inactive=None,
        connection_inactive_since=None,
        ignored_in_scope=0,
        possibly_paused=0,
        ignored_with_activity=0,
        expired_in_period=0,
        deleted_in_period=0,
        deleted_on_request=0,
        deleted_on_request_last=None,
        not_stored_in_period=0,
    )
    start_lines = Counter(caveats(only_start, timezone))
    pre_connection = bool(start_lines) and not start_lines - rendered
    return CaveatFacts(frozenset(carried), pre_connection, sum(rendered.values()))


async def product_coverage(
    session: AsyncSession, account_id: int, conversation_id: int, start: datetime, end: datetime
) -> PeriodCoverage:
    """The coverage facts of the product's catch-up for ONE chat."""
    return await period_coverage(
        session, account_id, start=start, end=end, conversation_ids=[conversation_id]
    )


# ------------------------------------------------------------------- the target
@dataclass(frozen=True, slots=True)
class _Loaded:
    account_id: int
    conversation_id: int
    start: datetime
    end: datetime
    gap_ids: Mapping[GapKey, str]


@dataclass(frozen=True, slots=True)
class _Record:
    conversation: Conversation
    status: str
    error: str | None
    calls: int
    output: SummaryOutput
    plain: str
    counts: Mapping[str, int]
    language: LanguageVerdict
    coverage: Mapping[str, bool | None]
    judged: Mapping[str, bool]
    #: The summary line(s) the loop judge quoted for each loop it was asked about.
    evidence: Mapping[str, str]
    check: CaveatCheck
    caveat_lines: int
    screen: ScreenResult
    #: What the product's attribution check changed in this summary (S03.7).
    attribution: AttributionCounts = NO_CHANGES
    #: Loops the judge called surfaced with a quote that is not in the summary:
    #: counted as missed in the pre-screen, listed for the owner (S03.7 A2).
    unquoted: tuple[str, ...] = ()
    #: Loops judged surfaced whose quote is only a near copy of a summary line (a
    #: letter in another script, a dropped parenthesis): counted, listed for the
    #: owner (S03.7 round 3).
    near_quotes: tuple[str, ...] = ()

    @property
    def summarized(self) -> bool:
        return self.error is None

    def loop_outcomes(self) -> dict[str, bool]:
        """Open loop id -> surfaced, by the deterministic match or else the judge
        (a loop nobody could judge counts as missed): the pre-screen."""
        return {
            loop: checked if checked is not None else self.judged.get(loop, False)
            for loop, checked in self.coverage.items()
        }

    def as_json(self) -> dict[str, Any]:
        """One record for the JSON report. The summary text and the flagged
        statements are model output over **synthetic** conversations: they are kept
        so the owner can review them (E-o); the Markdown report never prints them."""
        return {
            "id": self.conversation.id,
            "variant": self.conversation.variant,
            "status": self.status,
            "error": self.error,
            "summary_calls": self.calls,
            "items": dict(self.counts),
            # Items the attribution check demoted (a "promise" citing only the
            # other side became a request) or dropped (no cited message of the
            # side it names): counts, written before the judges read the summary.
            "attribution_demoted": self.attribution.demoted,
            "attribution_dropped": self.attribution.dropped,
            "open_loops": dict(self.coverage),
            # The ledger key of each loop awaiting the owner's verdict: it names the
            # summary the verdict is about (a re-run's summary has other keys).
            "open_loop_keys": {
                loop: coverage_key(loop, self.plain)
                for loop, checked in self.coverage.items()
                if checked is None
            },
            "open_loops_judged": dict(self.judged),
            "open_loops_evidence": dict(self.evidence),
            # Judged surfaced, but the quote is not in the summary: no evidence.
            "open_loops_unquoted": list(self.unquoted),
            # Judged surfaced on a near copy of a summary line: counted, listed.
            "open_loops_near_quotes": list(self.near_quotes),
            "caveats_expected": self.check.expected,
            "caveats_missing": list(self.check.missing),
            "caveats_spurious": list(self.check.spurious),
            "caveat_gap_ids": sorted(self.output.caveat_gap_ids),
            "caveat_pre_connection": self.output.pre_connection_caveat,
            "caveat_lines": self.caveat_lines,
            "language_ok": self.language.ok,
            "language_detected": self.language.detected,
            "script_share": round(self.language.script_share, 3),
            "screened": self.screen.screened,
            "screen_error": self.screen.error,
            "flags": [
                {
                    "id": finding.finding_id,
                    "reason": finding.reason,
                    "evidence": list(finding.evidence_message_ids),
                    "statement": finding.statement,
                    "status": finding.status,
                }
                for finding in self.screen.findings
            ],
            "summary_text": self.plain,
            # The same summary with its structure (who promised what, due dates) as
            # the judges read it: attribution cannot be reviewed from the plain text.
            "summary_labelled": self.output.text,
        }


class SummaryTarget:
    name = "summary"
    dataset: DatasetName = "e1"
    route = ROUTE
    uses_model = True

    def __init__(
        self,
        conversations: Sequence[Conversation],
        *,
        owner_language: OwnerLanguage,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        coverage_reader: CoverageReader = product_coverage,
    ) -> None:
        self._conversations = list(conversations)
        self.owner_language: OwnerLanguage = owner_language
        self._factory = session_factory
        self._coverage_reader = coverage_reader

    @property
    def label(self) -> str:
        """What the report calls this run's variant: the owner's language."""
        return f"period-{self.owner_language}"

    def bind(self, session_factory: async_sessionmaker[AsyncSession]) -> SummaryTarget:
        """The same target over a database. The runner builds a target first (to
        refuse and estimate), and only then pays for a database."""
        return SummaryTarget(
            self._conversations,
            owner_language=self.owner_language,
            session_factory=session_factory,
            coverage_reader=self._coverage_reader,
        )

    @property
    def _database(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            raise RuntimeError("bind() a database before running the summary target")
        return self._factory

    def _selected(self, context: RunContext) -> list[Conversation]:
        if context.only is not None:
            return self.named(context.only, limit=context.limit)
        if context.limit is None:
            return self._conversations
        if context.limit < 1:
            raise ValueError("limit must be at least 1")
        return self._conversations[: context.limit]

    def named(self, ids: Sequence[str], *, limit: int | None = None) -> list[Conversation]:
        """The conversations `--only` names, in set order: a cheap trial of chosen
        chats (S03.7 round 3). A selection is always a trial, so it must name at
        least one chat and fewer than all of them, and cannot be combined with a
        limit."""
        if limit is not None:
            raise ValueError("only and limit cannot be combined")
        wanted = set(ids)
        unknown = sorted(wanted - {c.id for c in self._conversations})
        if unknown:
            raise ValueError(f"only names unknown conversations: {', '.join(unknown)}")
        if not wanted or len(wanted) >= len(self._conversations):
            raise ValueError("only must name at least one conversation and not the whole set")
        return [c for c in self._conversations if c.id in wanted]

    # ------------------------------------------------------------- estimate
    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        """Every call a run can make, at its worst: the summarize call and its
        one truncation retry at the larger budget (`Summarizer._call`), the
        fabrication judge, and the open-loop judge where the conversation has an
        open loop without `values`. The judges read a summary of the largest size
        `sanitize` allows."""
        config = SummarizeConfig(model="")  # the product's budgets, no model needed
        larger = min(config.max_output_tokens * 2, config.max_output_tokens_ceiling)
        calls: list[PlannedCall] = []
        for conversation in self._selected(context):
            chars = self._summarize_chars(conversation)
            calls.append(PlannedCall(SUMMARIZE_PURPOSE, chars, config.max_output_tokens))
            if larger > config.max_output_tokens:
                calls.append(PlannedCall(SUMMARIZE_PURPOSE, chars, larger))
            calls.append(
                PlannedCall(
                    FABRICATION_PURPOSE,
                    screen_input_chars(conversation, MAX_SUMMARY_CHARS),
                    JUDGE_MAX_OUTPUT_TOKENS,
                )
            )
            loops = loops_input_chars(conversation, MAX_SUMMARY_CHARS)
            if loops is not None:
                calls.append(PlannedCall(LOOPS_PURPOSE, loops, JUDGE_MAX_OUTPUT_TOKENS))
        return calls

    def _summarize_chars(self, conversation: Conversation) -> int:
        built = build_summarize(
            prompt=prompt_registry.active(SUMMARIZE_PURPOSE),
            account=Account(timezone=OWNER_TIMEZONE, locale_pref=self.owner_language),
            messages=_source_messages(conversation),
            period=catch_up_window(conversation),
            contact_name=conversation.contact.display_name,
        )
        return (
            len(built.instructions) + sum(len(item.text) for item in built.items) + _ID_SLACK_CHARS
        )

    # ------------------------------------------------------------------ run
    async def run(self, context: RunContext) -> TargetResult:
        if context.llm is None or context.model is None:
            raise ValueError("the summary target needs a model client and a model id")
        selected = self._selected(context)
        records: list[_Record] = []
        stopped: str | None = None
        for conversation in selected:
            try:
                records.append(await self._evaluate(conversation, context))
            except BudgetExceededError as stop:
                stopped = str(stop)
                break
        return self._result(records, planned=len(selected), ledger=context.reviews, stopped=stopped)

    async def _evaluate(self, conversation: Conversation, context: RunContext) -> _Record:
        assert context.model is not None  # checked in run()
        processor = processor_for_model(context.model)
        if processor is None:
            raise ValueError("the model has no known processor to name in the synthetic consent")
        # `load_corpus` commits the account and its messages before anything else is
        # added, so the cleanup must cover everything from here on.
        corpus = await load_corpus(self._database, [conversation])
        try:
            loaded = await self._prepare(corpus.account_id, conversation, processor)
            return await self._evaluate_loaded(conversation, loaded, context)
        finally:
            await unload_corpus(self._database, corpus.account_id)

    async def _evaluate_loaded(
        self, conversation: Conversation, loaded: _Loaded, context: RunContext
    ) -> _Record:
        assert context.llm is not None
        assert context.model is not None
        summarizer = Summarizer(
            session_factory=self._database,
            llm=context.llm,
            clock=_FixedClock(loaded.end),
            config=SummarizeConfig(model=context.model, effort=context.effort),
        )
        outcome = await summarizer.summarize_period(
            account_id=loaded.account_id,
            conversation_id=loaded.conversation_id,
            start=loaded.start,
            end=loaded.end,
            open_ended=True,
            parent_run_id=None,
        )
        if outcome.status in (SummaryStatus.NO_CONSENT, SummaryStatus.NOT_FOUND):
            # The harness is mis-built, not the model's miss: nothing here is a result.
            raise RuntimeError(f"the summary run could not start ({outcome.status.value})")
        async with self._database() as session:
            coverage = await self._coverage_reader(
                session, loaded.account_id, loaded.conversation_id, loaded.start, loaded.end
            )
        facts = carried_caveats(coverage, loaded.gap_ids, OWNER_TIMEZONE)

        summary = outcome.summary if outcome.status is SummaryStatus.SUMMARIZED else None
        error: str | None = None
        text = plain = ""
        counts: dict[str, int] = {}
        if summary is None:
            error = await self._failure_code(loaded, outcome)
        else:
            text = labelled_text(summary, contact_name=conversation.contact.display_name)
            plain, counts = plain_text(summary), _item_counts(summary)
        output = SummaryOutput(
            conversation.id, text, self.owner_language, facts.gap_ids, facts.pre_connection
        )
        loops = _loop_coverage(conversation, plain, summarized=summary is not None)
        if summary is None:
            screen = ScreenResult(conversation.id, (), screened=False, error="summary_failed")
            judged: dict[str, bool] = {}
            evidence: dict[str, str] = {}
            unquoted: tuple[str, ...] = ()
            near_quotes: tuple[str, ...] = ()
            language = LanguageVerdict(False, None, 0.0)
        else:
            screen = await LLMFabricationJudge(
                context.llm,
                model=context.model,
                ledger=context.reviews,
                effort=context.effort,
                account_id=loaded.account_id,
            ).screen(conversation, output)
            verdicts = await LLMOpenLoopJudge(
                context.llm,
                model=context.model,
                effort=context.effort,
                account_id=loaded.account_id,
            ).verdicts(
                conversation,
                output,
                [loop for loop, checked in loops.items() if checked is None],
            )
            judged = {loop: verdict.surfaced for loop, verdict in verdicts.items()}
            evidence = {loop: verdict.evidence for loop, verdict in verdicts.items()}
            unquoted = tuple(
                sorted(
                    loop
                    for loop, verdict in verdicts.items()
                    if verdict.judge_surfaced and not verdict.quoted
                )
            )
            near_quotes = tuple(
                sorted(
                    loop for loop, verdict in verdicts.items() if verdict.surfaced and verdict.near
                )
            )
            language = check_language(plain, self.owner_language)
        return _Record(
            conversation=conversation,
            status=outcome.status.value,
            error=error,
            calls=outcome.calls,
            output=output,
            plain=plain,
            counts=counts,
            language=language,
            coverage=loops,
            judged=judged,
            evidence=evidence,
            unquoted=unquoted,
            near_quotes=near_quotes,
            check=check_caveats(conversation, output),
            caveat_lines=facts.lines,
            screen=screen,
            attribution=outcome.attribution,
        )

    async def _failure_code(self, loaded: _Loaded, outcome: PeriodOutcome) -> str:
        """`summary:<status>[:<reason or error class>]`: the discard reason, else the
        run row's own class; a short code, never content. Every status but
        `summarized` is a failure (E-r): refused, failed, discarded (the chat changed,
        expired, went Ignore or was purged during the call), skipped (Ignore or bot
        chat), cached, empty, over budget, backed off."""
        status = outcome.status
        if outcome.discard_reason is not None:
            return f"summary:{status.value}:{outcome.discard_reason.value}"
        async with self._database() as session:
            error_class = (
                await session.execute(
                    select(AgentRun.error_class)
                    .where(AgentRun.account_id == loaded.account_id)
                    .order_by(AgentRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return f"summary:{status.value}" + (f":{error_class}" if error_class else "")

    # -------------------------------------------------------------- the data
    async def _prepare(
        self, account_id: int, conversation: Conversation, processor: str
    ) -> _Loaded:
        """Add to the stored conversation what the product needs to summarize it
        (the module docstring, step 2). The caller removes the account on any failure."""
        start, end = catch_up_window(conversation)
        clock = _FixedClock(end)
        async with self._database() as session, session.begin():
            account = await session.get(Account, account_id)
            assert account is not None  # just created
            account.locale_pref = self.owner_language
            for kind in (CONSENT_AI_CONTROL, CONSENT_AI_MANAGED):
                await grant_consent(
                    session,
                    account,
                    kind=kind,
                    policy_version=EVAL_POLICY_VERSION,
                    processors=[processor],
                    clock=clock,
                )
            session.add(
                BusinessConnection(
                    account_id=account.id,
                    connection_id="eval-connection",
                    is_enabled=True,
                    rights=["can_reply", "can_read_messages"],
                    capability_tier="can_reply",
                    user_chat_id=_EVAL_OWNER_CHAT_ID,
                    status="active",
                    connected_at=conversation.coverage_started_at or start,
                )
            )
            row = (
                await session.execute(
                    select(ConversationRow).where(ConversationRow.account_id == account.id)
                )
            ).scalar_one()
            gap_ids: dict[GapKey, str] = {}
            for gap in conversation.coverage_gaps:
                session.add(
                    CoverageGap(
                        account_id=account.id,
                        conversation_id=row.id,
                        kind=gap.kind,
                        certainty=gap.certainty,
                        at_risk_from=gap.at_risk_from,
                        at_risk_to=gap.at_risk_to,
                    )
                )
                key = (gap.kind, gap.certainty, gap.at_risk_from, gap.at_risk_to, row.id)
                gap_ids[key] = gap.id
            conversation_row_id = row.id
        return _Loaded(account_id, conversation_row_id, start, end, gap_ids)

    # --------------------------------------------------------------- metrics
    def _result(
        self,
        records: Sequence[_Record],
        *,
        planned: int,
        ledger: ReviewLedger,
        stopped: str | None,
    ) -> TargetResult:
        everything = self._conversations
        coverage: dict[str, bool | None] = {}
        texts: dict[str, str] = {}
        judged: dict[str, bool] = {}
        for record in records:
            coverage.update(record.coverage)
            texts.update(dict.fromkeys(record.coverage, record.plain))
            judged.update(record.judged)
        loops_total = sum(len(open_loops(c)) for c in everything)
        with_caveats = sum(bool(c.labels.expected_caveats) for c in everything)
        metrics = [
            recall_metric(coverage, ledger, judged, set_size=loops_total, summary_texts=texts),
            *caveat_metrics(
                [r.check for r in records],
                ledger,
                presence_set_size=with_caveats,
                spurious_set_size=len(everything),
                gated=False,
                note=CAVEAT_NOTE,
            ),
            language_metric(
                {r.conversation.id: r.language for r in records}, ledger, set_size=len(everything)
            ),
            fabrication_metric([r.screen for r in records], ledger, set_size=len(everything)),
        ]
        notes = [
            "Caveats come from the product's deterministic renderer over the harness's own "
            "synthetic gap rows, not from the model (E-d, E-p): the two caveat metrics are an "
            "ungated wiring regression check. Recall and language are measured on the "
            "per-chat summary (the summarize output), not on the owner-facing reply.",
        ]
        failed = sorted(r.conversation.id for r in records if not r.summarized)
        if failed:
            notes.append(
                f"{len(failed)} conversation(s) failed at the model and count as misses: "
                + ", ".join(failed)
            )
        retried = sum(r.calls > 1 for r in records)
        if retried:
            notes.append(f"{retried} summary call(s) were truncated once and retried larger.")
        if stopped:
            notes.append(f"STOPPED EARLY: {stopped}; {len(records)} of {planned} items ran")
        return TargetResult(
            target=self.name,
            dataset=self.dataset,
            items=len(records),
            items_planned=planned,
            items_total=len(everything),
            metrics=tuple(metrics),
            details=tuple(r.as_json() for r in records),
            breakdowns={
                "open-loop recall (pre-screen) by variant": _by_variant(
                    records, lambda r: list(r.loop_outcomes().values())
                ),
                "owner-language check by variant": _by_variant(records, lambda r: [r.language.ok]),
            },
            notes=tuple(notes),
            prompts=(
                prompt_registry.active(SUMMARIZE_PURPOSE).label,
                FABRICATION_RUBRIC,
                LOOPS_RUBRIC,
            ),
        )


def _loop_coverage(
    conversation: Conversation, plain: str, *, summarized: bool
) -> dict[str, bool | None]:
    """Open loop id -> surfaced, missed, or None (needs a verdict). A conversation
    that was not summarized surfaced nothing: every loop is a definite miss, so no
    verdict can ever turn it into a pass."""
    if not summarized:
        return {loop.id: False for loop in open_loops(conversation)}
    return open_loop_coverage(conversation, plain)


def _by_variant(
    records: Sequence[_Record], outcomes: Callable[[_Record], list[bool]]
) -> dict[str, Measure]:
    grouped: dict[str, list[bool]] = {}
    for record in records:
        grouped.setdefault(record.conversation.variant, []).extend(outcomes(record))
    return {variant: Measure(sum(flags), len(flags)) for variant, flags in sorted(grouped.items())}


@asynccontextmanager
async def isolated_summary(base_url: str, target: SummaryTarget) -> AsyncIterator[SummaryTarget]:
    """`target` over a migrated throwaway database (`assistant_eval_<random>` on
    the server of `base_url`), dropped on exit. Touches no existing database."""
    async with scratch_database(base_url) as url:
        engine = build_engine(url)
        try:
            yield target.bind(build_session_factory(engine))
        finally:
            await engine.dispose()
