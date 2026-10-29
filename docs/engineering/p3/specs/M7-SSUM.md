# M7-SSUM — Scheduled summaries: fire handler → `automation.summarize`

| | |
|---|---|
| **Unit** | M7-SSUM — the `scheduled_summary` fire handler and skip reporter, and the `automation.summarize` job: consent and budget re-checked when it runs, one `summarize_period` run per chat, an at-most-once claim before the send, a truthful "could not summarize" |
| **Team / slot** | Edvard / E2 (only after the gate candidate is frozen on D+4 and M7-MEX; README §4 slots it on D+5) |
| **Size** | S+ (≈ 0.75 engineer-day, one fix round included) |
| **Depends on** | F0-R (and through it F0-S: `SCHEDULED_SUMMARY_TASK`, `scheduled_summary_queueing_lock`, `SCHEDULED_SUMMARIES_RUN`, `NotificationKind.OCCURRENCE_SKIPPED`; F0-R: the fire registry, `ScheduledSummaryRule`, `SummaryJobHandlers`, `_wire_automation`). Creating summaries is M7-AUTO's, but this unit's tests seed automations directly and need nothing from it |
| **Branch → base** | `p3/m7-ssum` → `int/p3`; if it lands after R1, `p3/m7-ssum` → `main` as a flagged PR the TL merges (OD-2), shipped in deploy #2 |
| **Peer reviewer** | J3 (Claude; the summary isolation and consent rules mirror M6-PLAN's contact-triggered runs) |
| **QA** | QE1, before merge |
| **Tier** | 1 (high, safety-adjacent: managed content reaches the provider; at-most-once control send) |
| **Normative inputs** | [P3-interfaces §7.1, §7.2, §8.1, §8.2, §9.1, §11.2, §13, §15](../contracts/P3-interfaces.md); [specs/F0-R.md §3.1 step 5](F0-R.md) (the savepoint defer of `DigestFireHandler`); [specs/GW-ESC.md §3.3](GW-ESC.md); decisions P3-T45, P3-T62; TL rulings R68, R70, R88 (own occurrence re-check); IMPLEMENTATION_PLAN §9 S07.3; PRD FR-FUP-04, FR-FUP-08, §14; AGENTS §6 invariants 3a, 4, 6, 9, 11; ARCHITECTURE §7.1 (`summarize`), §12.2, §12.4, §14.3; P2 F0-interfaces §6.1 (queueing locks and transactions) |

## 1. Goal

At the owner-chosen time a scheduled summary arrives in the control chat: one model-written period summary per chat in its scope, combined deterministically, with every coverage caveat. Consent and budget are checked again when the job runs (a revoked `ai_managed` means no model call and a plain "could not summarize" note). Each chat is summarized in its own tool-less run. The message is sent at most once per occurrence, even if the job runs twice or a worker dies mid-way; a late summary says it is late, and a summary too late to matter is skipped and reported.

## 2. Scope

**In:**
1. `automation/scheduled_summaries.py`: `ScheduledSummaries` (the fire handler `fire_summary`, the skip reporter, the `automation.summarize` body), the automation-scoped occurrence re-check, the period rule, the deterministic message renderer, the `PeriodSummaries` port and its value types, the claim and the result record.
2. `jobs/tasks_summaries.py` (owned after CP1): `SummarizerPeriods`, the adapter that implements the port with `agent/summarize.py::Summarizer`; `SummaryJob`, the job-layer wrapper that runs the body and then the budget threshold alerts; the `SummaryJobHandlers.summarize` field set to `SummaryJob.summarize`.
3. Wiring lines in `_wire_automation`.

**Out:** creating, pausing, rescheduling or deleting summaries (M7-AUTO); the `summarize` prompt and run (unchanged; `agent/summarize.py` has no P3 owner); the digest (M05); the `occurrence_skipped` renderer (M7-FUP); "Open chat" buttons on the summary; storing the summary text anywhere new (period summaries stay in M03's 30-day cache; the message itself lives in Telegram).

## 3. Design

### 3.1 Flow

```
automation.fire (queue outbound; F0-R fire path, one transaction)
  └─ ScheduledSummaries.fire_summary: defer automation.summarize inside a SAVEPOINT → occurrence marked fired
automation.summarize(automation_id, occurrence_at)   (queue llm; qlock ssum:{a}:{o}; no retry)
  SummaryJob.summarize (jobs layer) → ScheduledSummaries.summarize (domain):
  Tx 1  re-check: automation runs, account active, this automation's occurrence and its deadline,
        no claim yet; period, chats in scope, coverage caveats
  ----  model calls, no transaction open: one summarize_period run per chat (port)
        compose the message (pure)
  Tx 2  claim: lock the automation row, re-check it runs, no claim yet → audit scheduled_summary_claimed
  ----  send through ControlChannel (never resent)
  Tx 3  record: audit scheduled_summary_sent (outcome sent | uncertain | not_delivered)
  SummaryJob: budget threshold alerts if any model call was made (agent/budget.py, jobs layer only)
```

### 3.2 Fire handler and skip reporter (`ScheduledSummaries.fire_summary`, `ScheduledSummaries.on_skipped`)

Registered as `register_fire_handler("scheduled_summary", summaries.fire_summary, flag=SCHEDULED_SUMMARIES_RUN, on_skipped=summaries.on_skipped)` (default `skip_policy` `SKIP_WITH_NOTE`; P3-interfaces §8.1). `fire_summary` is the contract's `automation/scheduled_summaries.py::fire_summary`, a method because it needs the injected scheduler; bootstrap builds the one instance in `_wire_automation` (no module-level instance). The registry has locked the occurrence and checked the account, the automation's status, the deadlines (`max_lateness` 120 min, OD-17) and the flag; it marks the occurrence `fired` in the same transaction after the handler returns.

**`fire_summary(session, ctx)`** defers `SCHEDULED_SUMMARY_TASK` (`automation.summarize`) with args `{"automation_id": ctx.automation_id, "occurrence_at": epoch_of(ctx.occurrence_at)}` and `queueing_lock=scheduled_summary_queueing_lock(automation_id, epoch)` **inside the fire transaction**, exactly as F0-R's `DigestFireHandler` does (F0-R spec §3.1 step 5; P2 F0-interfaces §6.1): inside `session.begin_nested()`, `scheduler.defer(..., connection=<the session's raw driver connection>)` (obtained as `telegram/control/dispatcher.py` does: `(await (await session.connection()).get_raw_connection()).driver_connection`); when `defer` returns `None` (the queueing lock already holds a job), raise a private exception inside the savepoint block and catch it outside, so only the savepoint rolls back. The job and the `fired` mark therefore commit together or not at all: a crash before the commit leaves the occurrence `claimed` for M05's `lost_fires` recovery, and no reconciler of its own is needed (P3-interfaces §7.1's row says "deferred after commit"; the §7.1 footnote allows a savepoint defer, and this one needs no domain flag or reconciler; the contract adopts this, §9). No model call, no Telegram call, no commit.

**`on_skipped(session, skipped)`** (called by the registry in the same transaction for `skipped_obsolete`, and for `skipped_restore` where F0-R passes the registry) enqueues `occurrence_skipped` with `inbox.notifications.enqueue(…, kind=NotificationKind.OCCURRENCE_SKIPPED, priority=Priority.HIGH, dedupe_key=f"skipped:{automation_id}:{epoch_of(skipped.occurrence_at)}", refs=NotificationRefs(automation_ids=(automation_id,), conversation_ids=…, contact_ids=… from the automation's refs), clock=…, scheduler=…)` (P3-interfaces §11.2; FR-FUP-08, OD-17: every skip is reported). Its renderer is M7-FUP's; until M7-FUP merges the notice is suppressed at delivery (fail safe).

### 3.3 The `automation.summarize` body (`ScheduledSummaries.summarize(automation_id, occurrence_at) -> SummaryRun`)

`ScheduledSummaries` is built with `session_factory`, `clock`, `scheduler`, `channel: ControlChannel` (`core/channel.py`) and `periods: PeriodSummaries | None`. It holds no budget or alert dependency: `agent/budget.py` may not be imported from `automation` (AGENTS §5 contract 1), so the threshold alerts run in `SummaryJob` (§3.5). `SummaryRun` (frozen) records `status` (a fixed code), `account_id`, `calls`, `outcome`. Every statement carries the account id (RG-21); the job reloads everything (AGENTS §13) and is safe to run twice.

**Tx 1 — re-check and plan** (short; commits at the end, because the re-check may settle the occurrence):
1. Load the automation by id, then its account. Missing automation ⇒ `SummaryRun("gone")`. Account not `active` or `control_chat_id` NULL ⇒ `"no_control_chat"`.
2. **Still runs:** `status = 'active'`, or `status = 'completed'` with no occurrence row after this one (a one-off's last occurrence, the `_still_runs` rule). `paused`, `cancelled`, `expired` ⇒ `"not_running"` (a pause or delete after the fire cancels the queued summary — the S07.3 "handler re-checks status" rule).
3. **This automation's occurrence and its deadline** (`_recheck`, a private function in this module): `SELECT … FROM automation_occurrences WHERE account_id = :a AND automation_id = :id AND occurrence_at >= :o AND occurrence_at < :o + 1 second FOR UPDATE` (`:o = from_epoch(occurrence_at)`; the one-second window `plan_fire` uses). **Do not call `schedules.recheck_occurrence`**: it matches by account, kind and instant, not by automation, so two scheduled summaries due at the same minute would read (and settle) each other's occurrence (contract gap reported). Then:
   - no row ⇒ `"not_scheduled"`;
   - row already `skipped` (a previous run of this job settled it) ⇒ `"obsolete"` or `"expired"` per its note, with no new report (the first run reported it);
   - otherwise `t = schedules.timing(automation, occurrence_at, now)` (public, M05). `Timing.OBSOLETE` (the job waited past 2 h) ⇒ settle the row with the values M05's `_settle` writes (`status = 'skipped'`, `note_code = 'skipped_obsolete'`, `fired_at = NULL`), call `self.on_skipped(session, SkippedOccurrence(account_id, automation_id, "scheduled_summary", occurrence_at, Note.SKIPPED_OBSOLETE))` and return `"obsolete"`: the registry never reports a skip made here, so this unit does (one report; the dedupe key holds on a re-run). `Timing.EXPIRED` ⇒ settle with `note_code = 'expired'`, return `"expired"` (no notice, as for every `expired` note). `LATE` ⇒ continue with `late = True`; `ON_TIME` ⇒ continue.
4. **Already claimed** (cheap early exit; Tx 2 is the authority): a `scheduled_summary_claimed` audit row for this automation and epoch (§3.7) ⇒ `"already_claimed"`.
5. **Plan:** the rule (`ScheduledSummaryRule` via `load_schedule_rule`; invalid ⇒ `"invalid_rule"`), the period (§3.4), the chats `catch_up.active_in_period(session, account_id, start=start, end=query_end, conversation_ids=scope.conversation_ids, contact_ids=scope.contact_ids)` (an `all_observed` scope passes both empty; Ignore-mode and bot chats are never returned), `chosen = chats[:MAX_CHATS]` (`MAX_CHATS = 15`, as `summarize_period`), `more = len(chats) - len(chosen)`, and the caveats `catch_up.caveats(catch_up.period_coverage(session, account_id, start=start, end=query_end, conversation_ids=<scope ids or None>), timezone)`.

**Model calls** (no transaction open; AGENTS §13): `summaries = await self.periods.summarize(account_id=…, chats=chosen, start=start, end=query_end)` (§3.5). Each chat is a separate `summarize` run on one conversation (invariant 4), with no tools (invariant 3a), consent (`ai_control` and `ai_managed` naming the route's processor) and budget checked by `Summarizer` before its run row (invariant 9: a job queued before a revocation stops when it runs). With `periods` `None` (no LLM in this process) the job is not wired at all (§3.8).

**Compose** with the pure `render_scheduled_summary(...)` (§3.6) → `ComposedSummary(text, content, chats_summarized)`:
- `content = "could_not_summarize"` when `chosen` is non-empty and **no** chat in it ended `SUMMARIZED` or `EMPTY`. The reason, in this order: any chat `NO_CONSENT` ⇒ "AI processing of your chats is off; turn it on in /privacy"; else any `OVER_BUDGET` ⇒ "this month's AI budget is used up"; else "the summaries could not be made". Otherwise `content = "summary"`, including an empty period ("No messages were stored in this period.", plus every caveat — never "nothing happened").

**Tx 2 — claim** (short): `SELECT … FROM automations WHERE account_id = :a AND id = :id FOR UPDATE` (serializes two runs of the job for this automation); re-check steps 1–2 (deleted or paused meanwhile ⇒ `"not_running"`, nothing sent); a claim row for this automation and epoch ⇒ `"already_claimed"`, nothing sent; otherwise insert audit `scheduled_summary_claimed` (actor `system`, `subject_type = 'automation'`, `subject_id`, details `{"occurrence_at": epoch}`) and commit. This is the at-most-once claim, committed before the send (the P2 pattern of notification and digest claims, and of the approval-card presentation claim).

**Send:** `delivery = await self.channel.send(account_id=…, chat_id=account.control_chat_id, text=composed.text)` with no buttons. The gateway splits a long text into chunks; since P2 #133 a later chunk refused after earlier ones arrived comes back as an uncertain delivery (`partial_delivery`), never as a failure. An exception from the channel counts as uncertain (log the exception class only). **Nothing is ever resent.**

**Tx 3 — record:** audit `scheduled_summary_sent` (actor `system`) with details `{"occurrence_at": epoch, "claim_id": <claim audit id>, "chats": composed.chats_summarized, "content": "summary" | "could_not_summarize", "outcome": "sent" | "uncertain" | "not_delivered"}` (`sent` = `delivery.ok`; `uncertain` = `delivery.uncertain` or an exception; `not_delivered` = a definite failure). One log line `scheduled_summary_finished` with ids, counts and the outcome. The body returns `SummaryRun`; `SummaryJob` then runs the budget threshold alerts when `run.calls > 0` (§3.5).

**Crash windows** (all truthful, none duplicates):

| Crash | Next run (`system.retry_stalled_jobs`) | Result |
|---|---|---|
| During the model calls | Repeats them (cached period summaries make this cheap), claims, sends | One message |
| After the claim commit, before the send | Finds the claim ⇒ sends nothing | No message; the claim without a result reads as "uncertain" |
| After the send, before Tx 3 | Finds the claim ⇒ sends nothing | One message; the claim without a result reads as "uncertain" |

### 3.4 Period and scope

- `end = now` (the job's time); `query_end = end + CLOCK_SKEW` (5 minutes, the "until now" rule of `summarize_period`, so messages Telegram dated slightly after our clock are included); `open_ended = True`.
- `since_last`: `start` = the latest `fired` occurrence of this automation before this one (`SELECT max(occurrence_at) FROM automation_occurrences WHERE account_id = :a AND automation_id = :id AND status = 'fired' AND occurrence_at < :this`), else `automation.confirmed_at`, else `created_at`. A skipped occurrence in between is covered, because `start` comes from the last fired one.
- `last_24h`: `end − 24 h`; `last_7d`: `end − 7 days`.
- Every period is capped: `start >= end − MAX_SCHEDULED_PERIOD` (`MAX_SCHEDULED_PERIOD = timedelta(days=7)`). `start >= end` ⇒ an empty period.
- The scope's ids come from the stored rule; ids of chats or contacts deleted since are simply not found by `active_in_period` (and a deletion of a named chat deletes the automation itself through `refs`, `purge_p2`).

### 3.5 Port and adapter (layering)

`automation` is a domain package: it **must not import `assistant.agent`**, `assistant.llm` or `assistant.jobs` (AGENTS §5 contract 1; `lint-imports` enforces it). So the domain module defines the port and the value types, and the job layer implements them.

```python
# automation/scheduled_summaries.py
class ChatSummaryStatus(StrEnum):
    SUMMARIZED = "summarized"
    EMPTY = "empty"
    NO_CONSENT = "no_consent"
    OVER_BUDGET = "over_budget"
    REFUSED = "refused"
    FAILED = "failed"
    CHANGED = "changed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ChatSummary:
    conversation_id: int
    name: str  # preferred_name(display_name, username) or "a contact"
    messages_in_period: int
    status: ChatSummaryStatus
    importance: str | None = None  # a catch_up.IMPORTANCE_ORDER value (SUMMARIZED only)
    overview: str | None = None  # plain text (unescaped)
    points: tuple[str, ...] = ()  # plain text lines, "who" already rendered (≤ 6)
    covered: tuple[int, int] | None = None  # (summarized, total) when only the newest were read
    calls: int = 0


class PeriodSummaries(Protocol):
    async def summarize(
        self,
        *,
        account_id: int,
        chats: Sequence[ActiveConversation],
        start: datetime,
        end: datetime,
    ) -> Sequence[ChatSummary]: ...
```

**`jobs/tasks_summaries.py::SummarizerPeriods(summarizer: Summarizer, session_factory)`** implements it with exactly the per-chat logic of `agent/tools/summaries.py::summarize_period_tool`:
- `summarizer.summarize_period(account_id=…, conversation_id=…, start=start, end=end, open_ended=True, parent_run_id=None)` for each chat, at most `SUMMARY_CONCURRENCY` (4) in flight, retried once at once when the result was discarded because the chat changed or expired during the call (`CHANGED_RETRIES`, `_RETRYABLE`); an exception for one chat ⇒ `FAILED` for that chat only (log the exception class).
- Status mapping: `SUMMARIZED`, `CACHED` ⇒ `SUMMARIZED`; `EMPTY`; `NO_CONSENT`; `OVER_BUDGET`; `REFUSED`; `FAILED`, `NOT_FOUND` ⇒ `FAILED`; `SKIPPED`; `DISCARDED` ⇒ `CHANGED`.
- For a summarized chat, in one read transaction: `authors = summary_authors(session, account_id, conversation_id, summary)`; `checked, _ = check_attribution(summary, authors)` (S03.7: who promised what is rendered by the application, never taken from model text); `overview = checked["overview"]`; points: up to two each from `open_questions_to_owner`, `decisions_requested` and `commitments`, each "{who}: {text}" with `agent/attribution.py::render_who(key, item, contact_name, locale)` (commitments add `effective_status` and the stated `due`); `importance = catch_up.importance(checked, awaiting_owner_reply=chat.awaiting_owner_reply, asked_question=False)`; `covered` when `summarized_count < message_count`.
- `parent_run_id=None` makes each run's `trigger_ref` `catch_up:run:None:conv:{id}`: `summarize_period` has no trigger parameter and `agent/summarize.py` has no P3 owner. Accepted (metering is per run type); noted for M07.md.

**`jobs/tasks_summaries.py::SummaryJob(*, summaries: ScheduledSummaries, session_factory, clock, alerts: OwnerAlerts | None, monthly_budget_usd: Decimal | None)`** is the job-layer wrapper, the pattern of `jobs/tasks_inbox.py` and `MonitorJobs.evaluate`: `async def summarize(self, automation_id: int, occurrence_at: int) -> SummaryRun` awaits `summaries.summarize(...)`, then, when `run.calls > 0`, `run.account_id` is set and `alerts` is set, `agent.budget.check_budget_thresholds(session_factory, clock, alerts, account_id=run.account_id, monthly_budget_usd=monthly_budget_usd)`, and returns the run. It is the `SummaryJobHandlers.summarize` callable (F0-R's `Callable[[int, int], Awaitable[object]]`).

### 3.6 The message (`render_scheduled_summary`, pure)

English fixed texts (P2-O11); application markup raw (`<b>` only); every untrusted field — contact names, overviews, points — escaped exactly once with `html.escape(value, quote=False)` (GW-ESC §3.3; domain code never imports `telegram.render`). Caveats are application text from `catch_up.caveats` and carry no markup; they are escaped once too (harmless, and safe if a caveat ever quotes a name).

```
🗓️ <b>Scheduled summary</b> — {from} to {to} ({timezone})
Due at {HH:MM}; this summary is late.                        ← only when late

<b>{name}</b> · {n} messages
{overview}
• {point}
Only the latest {k} of {n} messages were summarized.          ← only when covered

Not summarized: {name} ({reason}); {name} ({reason})
{m} more chats with messages were not included.               ← only when more > 0

{caveat}                                                      ← every caveat, one per line
```

- Order: by importance group (`catch_up.IMPORTANCE_ORDER`), then messages in the period, most first.
- Clipping (characters): overview ≤ 400, point ≤ 200, at most 6 points per chat.
- Fixed reasons: `no_consent` "AI for your chats is off"; `over_budget` "the monthly AI budget is used up"; `refused` "the AI declined"; `failed` "it could not be summarized"; `changed` "it changed while being summarized"; `skipped` "Ignore mode or a bot chat". `empty` chats are not listed.
- Could-not note: "🗓️ <b>Scheduled summary</b> — {from} to {to} ({timezone})\nI could not make it: {reason}." Never worded as a summary (RG-20).
- Times are owner-local (`account.timezone`) as "Thu 30 Oct, 18:00", each instant converted on its own (`astimezone`), so a period across a DST change shows the right wall times. `last_24h` is 24 real hours, which is 23 or 25 local hours across a change; the header shows both ends, so it stays truthful.

### 3.7 The claim and its audit events

| Event | When | `details` |
|---|---|---|
| `scheduled_summary_claimed` (new; accepted, R70) | Tx 2, before the send | `occurrence_at` |
| `scheduled_summary_sent` | Tx 3, after the send | `occurrence_at`, `claim_id`, `chats`, `content`, `outcome` |

The claim lookup: `account_id = :a AND event = 'scheduled_summary_claimed' AND subject_type = 'automation' AND subject_id = :id AND details->>'occurrence_at' = :epoch` (`AuditLogEntry.details["occurrence_at"].astext == str(epoch)`, the pattern of `inbox/notify.py`). It runs under the automation row lock, so two jobs cannot both claim. Audit rows are append-only; a claim without a `scheduled_summary_sent` row means "sent or not, unknown" (OPS-VIEWS shows it so). Both rows are content-free (P3-interfaces §13).

### 3.8 Wiring (`_wire_automation` in `bootstrap.py`; function-local imports)

```python
channel = GatewayControlChannel(ctx.gateway)
summaries = ScheduledSummaries(
    session_factory=ctx.session_factory,
    clock=ctx.clock,
    scheduler=ctx.scheduler,
    channel=channel,
    periods=(
        SummarizerPeriods(
            build_summarizer(ctx.settings, ctx.session_factory, ctx.clock, ctx.llm),
            ctx.session_factory,
        )
        if ctx.llm is not None
        else None
    ),
)
ctx.fire_registry.register_fire_handler(
    "scheduled_summary",
    summaries.fire_summary,
    flag=SCHEDULED_SUMMARIES_RUN,
    on_skipped=summaries.on_skipped,
)
summary_job = SummaryJob(
    summaries=summaries,
    session_factory=ctx.session_factory,
    clock=ctx.clock,
    alerts=OwnerAlerts(ctx.session_factory, channel, ctx.clock),
    monthly_budget_usd=ctx.settings.llm_monthly_budget_usd,
)
# fragment: summaries=SummaryJobHandlers(summarize=summary_job.summarize) only when
#           SCHEDULED_SUMMARIES_RUN is in ctx.settings.feature_flags and periods is not None
```

`build_summarizer` already exists in `bootstrap.py`; `GatewayControlChannel` comes from `telegram/control/agent_channel.py` and `OwnerAlerts` from `inbox/notify.py` (function-local imports are allowed in `bootstrap.py`). `ctx.llm` is the consent-gated `LLMClient` wrapper, so invariant 9 holds at the client as well as in `Summarizer`. The fire handler is registered whatever the flags (the registry settles `disabled` when the flag is off). With the group unwired, F0-R's registration logs `task_not_wired` and returns. Flags are process settings: if the owner turns `scheduled_summaries.run` off while a job is queued, the restarted worker leaves the group unwired, the job logs `task_not_wired`, and that occurrence sends nothing (the owner switched the feature off; nothing claims it was sent).

### 3.9 Failure modes

| Case | Behaviour | Test |
|---|---|---|
| `ai_managed` revoked after the fire | No model call; the could-not note is sent once | `test_scheduled_summary_fires_and_rechecks_consent` |
| Budget used up | No model call; could-not note with the budget reason | `test_over_budget_gives_a_truthful_could_not_summarize_note` |
| One chat fails or the model refuses | That chat is listed with its reason; the rest are summarized | `test_one_failed_chat_does_not_stop_the_others` |
| The job waited past 2 h | Occurrence settled `skipped_obsolete`; one `occurrence_skipped` | `test_a_job_that_waited_past_max_lateness_skips_and_reports_once` |
| Two scheduled summaries due at the same minute | Each job re-checks and settles only its own automation's occurrence | `test_two_summaries_due_at_the_same_minute_recheck_their_own_occurrences` |
| A period across a DST change | Owner-local times on both ends are right | `test_period_times_are_owner_local_across_a_dst_change` |
| Paused or deleted after the fire | Nothing sent | `test_a_paused_or_deleted_automation_sends_nothing` |
| The job runs twice (stalled-job retry) | One message | `test_a_summary_is_sent_at_most_once_when_the_job_runs_twice` |
| Uncertain send (timeout, partial chunks) | Recorded `uncertain`; never resent | `test_an_uncertain_send_is_recorded_and_never_resent` |
| Definite send failure (bot blocked) | Recorded `not_delivered`; never resent | `test_a_definite_send_failure_is_recorded_not_delivered` |
| No messages in the period | "No messages were stored in this period." plus caveats | `test_an_empty_period_says_no_messages_were_stored` |
| A chat set to Ignore during the call | Discarded by `Summarizer`; listed "changed while being summarized" | (adapter test) |
| Another account's chats | Never read: every query is account-scoped | `test_rg21_a_summary_covers_only_its_own_accounts_chats` |

## 4. Files

**Owned:**

| File | Change |
|---|---|
| `src/assistant/automation/scheduled_summaries.py` (new) | §3.2–§3.7 |
| `src/assistant/jobs/tasks_summaries.py` (F0-R created it; M7-SSUM owns it after CP1) | `SummarizerPeriods`, `SummaryJob`; the group field set through wiring; the registration F0-R wrote stays as it is |
| `src/assistant/bootstrap.py` | lines inside `_wire_automation` only (§3.8) |
| the test files in §5 | new |

**Named touch points in files you do not own:** none. You call, never edit: `automation/schedules.py` (`timing`, `epoch_of`, `from_epoch`, `Note`, `Timing`, the registry types; not `recheck_occurrence`, §3.3 step 3), `automation/rules.py` (`ScheduledSummaryRule`, `load_schedule_rule`), `memory/catch_up.py` (`active_in_period`, `period_coverage`, `caveats`, `importance`, `IMPORTANCE_ORDER`), `memory/summaries.py::preferred_name`, `inbox/notifications.py::enqueue`, `agent/summarize.py`, `agent/attribution.py`, `agent/tools/common.py::summary_authors`, `agent/tools/summaries.py` (constants), `agent/budget.py`.

**Never edit without an ICR** (P3-interfaces §15.2): `policy/*`; the send-path `actions/*` files; `telegram/*`; `conversations/service.py`; migrations, ORM models and `privacy/*`; `llm/consent.py`; `config.py`, `core/*`; `agent/{runtime,context,schemas}.py`, `agent/tools/executor.py`; `agent/prompts/*` (the `summarize` prompt is not this unit's); `automation/schedules.py`, `automation/rules.py`, `automation/repository.py`; `jobs/tasks.py`, `jobs/reconcile.py`; `inbox/notification_types.py`.

## 5. Tests (red first)

Synthetic data only; `FakeClock`; `FakeLLM` that records each request (messages and tools) or raises; `FakeChannel` with scripted `Delivery` results; real PostgreSQL 18; `p2_factories.seed_automation(kind="scheduled_summary", spec=ScheduledSummaryRule(...).model_dump(mode="json"), rrule=…, next_run_at=…)`, `p2_factories.seed_conversation`, consent rows as M03's tests seed them. Run only these files locally.

**IMPLEMENTATION_PLAN §9 S07.3 / proposed M07 named tests assigned here** (`tests/integration/test_scheduled_summaries.py`):
- `test_scheduled_summary_fires_and_rechecks_consent` — the occurrence fires and the job is queued; `ai_managed` is revoked; the job runs: zero `FakeLLM` calls, the could-not note sent once, `scheduled_summary_sent` with `content = "could_not_summarize"`.
- `test_scheduled_summary_isolates_each_conversation` — two chats with distinct canary words; each recorded `FakeLLM` request holds one chat's canary only.

**RG tests added or extended** (in `tests/integration/test_scheduled_summaries.py` unless named otherwise):
- `test_rg08_no_managed_content_reaches_the_model_without_consent_at_run_time` (the consent case above, asserting on the recorded `FakeLLM` requests; RG-08 extension).
- `test_rg17_scheduled_summary_runs_are_given_no_tools` (every recorded request has `tools=()`).
- `test_rg20_a_could_not_summarize_note_never_claims_a_summary` (wording and `content`).
- `test_rg21_a_summary_covers_only_its_own_accounts_chats` (an `all_observed` summary never reads another account's active chat).
- `tests/integration/test_log_hygiene_m7ssum.py::test_rg10_scheduled_summary_logs_no_names_or_summary_text` (canary names and summary text; no log line or audit `details` contains them).

**The unit's own tests:**
- `tests/integration/test_scheduled_summaries.py`:
  - `test_the_fire_handler_defers_one_summarize_job_with_the_fire` (the job row and the `fired` mark commit together; with a job already queued under the lock the occurrence is still fired and one job exists)
  - `test_the_fire_handler_calls_no_model_and_sends_nothing`
  - `test_a_summary_is_sent_at_most_once_when_the_job_runs_twice` (sequential reruns, and two concurrent runs racing for the claim)
  - `test_a_retried_job_after_a_claim_never_resends`
  - `test_an_uncertain_send_is_recorded_and_never_resent`
  - `test_a_definite_send_failure_is_recorded_not_delivered`
  - `test_over_budget_gives_a_truthful_could_not_summarize_note`
  - `test_one_failed_chat_does_not_stop_the_others`
  - `test_a_paused_or_deleted_automation_sends_nothing`
  - `test_a_job_that_waited_past_max_lateness_skips_and_reports_once` (OD-17; a re-run of the same job reports nothing more)
  - `test_two_summaries_due_at_the_same_minute_recheck_their_own_occurrences` (one is obsolete by its own rule, the other on time: only the first is settled and reported; the second sends)
  - `test_period_times_are_owner_local_across_a_dst_change` (Europe/Berlin, a `last_24h` summary at 18:00 on 2026-10-25)
  - `test_budget_threshold_alerts_run_after_a_job_with_model_calls` (`SummaryJob`; none when no call was made)
  - `test_could_not_summarize_reason_follows_the_fixed_order` (a mix of `NO_CONSENT`, `OVER_BUDGET` and `FAILED` with none summarized)
  - `test_a_skipped_scheduled_summary_occurrence_is_reported` (through `reconcile_due` and `on_skipped`)
  - `test_since_last_starts_at_the_previous_fired_occurrence_and_is_capped_at_seven_days`
  - `test_a_late_summary_says_when_it_was_due`
  - `test_an_empty_period_says_no_messages_were_stored`
  - `test_summarizer_periods_retries_a_changed_chat_once`
- `tests/unit/test_scheduled_summary_text.py`:
  - `test_render_scheduled_summary_escapes_untrusted_text_once` (GW-ESC §3.3 rule 6 string — `&`, `<b>x</b>`, a literal `&amp;` — in a name, an overview and a point, through `telegram.render.sanitize`)
  - `test_every_coverage_caveat_is_kept`
  - `test_chats_not_summarized_are_listed_with_fixed_reasons`
  - `test_chats_are_ordered_by_importance_then_volume`
  - `test_overviews_and_points_are_clipped`

No mutation table: this unit is Tier 1, not Tier-1A (WORKFLOW §4.3); QE1 reviews before merge.

**Shared lists you must update:** none by hand (`scheduled_summaries.run` is in F0-S's `P3_FEATURE_FLAGS`). Tell QE1 if a strict-xfail QA-S7 scenario passes with this merge.

## 6. Acceptance criteria

1. A due scheduled summary defers exactly one `automation.summarize` job atomically with the fire; the job sends one message per occurrence, whatever reruns or crashes happen.
2. Each chat is summarized in its own tool-less run; no prompt holds another chat's messages; consent and budget are re-checked when the job runs, and without consent no model call is made and a truthful note is sent.
3. Late summaries say so; summaries too late are skipped and reported once; paused or deleted automations send nothing.
4. Every outcome is recorded (`sent`, `uncertain`, `not_delivered`) and nothing is ever resent.
5. `automation/scheduled_summaries.py` imports nothing from `assistant.agent`, `assistant.llm`, `assistant.jobs` or `assistant.telegram` (`lint-imports` green); the budget alerts run from `jobs/tasks_summaries.py`.
6. Each job re-checks only its own automation's occurrence (never `schedules.recheck_occurrence`).
7. All §5 tests green in CI; `mypy --strict` clean.

## 7. Docs updated in the same PR

- ARCHITECTURE §12.2: the `automation.summarize` row as built (queue, queueing lock, no retry, the savepoint defer from the fire, the three phases, the claim) and the `scheduled_summary` fire-handler line.
- PR description: the claim event and the `outcome`/`content` values as built (R70), the `catch_up:run:None` trigger note, the decisions and open items for M07.md.

## 8. Owner decisions

- **OD-19** (default assumed): scheduled summaries are model-written from per-chat period summaries, with consent re-checked when they fire. If the owner wants them deterministic (no model), `PeriodSummaries` gets a rolling-summary-only implementation and the could-not paths disappear; the fire handler, claim and renderer stay.
- **OD-17** (lateness and skips): 2 h tolerance (F0-R's rule default) and every skip reported. A different tolerance changes only that default.
- **OD-2** (one `int/p3`, flagged PRs to `main` after R1): if this lands after R1 it goes to `main` behind `scheduled_summaries.run` and ships in deploy #2; enable 1 then leaves the flag off until it is deployed (M7-AUTO's `create_automation` refuses while the flag is off).

## 9. Risks and notes

- **Contract points** (the claim is the at-most-once mechanism, so these were ruled before E2 starts; the contract adopts the spec's text, R32):
  - The `scheduled_summary_claimed` event and the sent/content split are accepted (R70): P3-interfaces §13 records delivery (`sent`, `uncertain`, `not_delivered`) and content (`summary`, `could_not_summarize`) separately, plus `claim_id`.
  - §7.1's `automation.summarize` row reads "deferred inside the fire transaction in a savepoint" (the §7.1 footnote's other allowed way, as F0-R's `DigestFireHandler`), so no reconciler is needed.
  - `schedules.recheck_occurrence` filters by account, kind and instant, not by automation; this unit re-checks in its own module (§3.3 step 3) and makes no edit to `recheck_occurrence` (ruled, R88).
  - §8.1 names `automation/scheduled_summaries.py::fire_summary`; it is a bound method of the one injected instance.
- **At most once can mean "lost"** after a crash between the claim and the send. That is the deliberate trade (invariant 6's spirit for control sends); the claim row makes the gap visible.
- **Cost:** up to 15 `summarize` runs per occurrence, served from the period cache where the messages did not change. The monthly budget stops runs (`OVER_BUDGET`) and `SummaryJob` runs the threshold alerts after the job.
- **E2 timing:** E2 does nothing but DELEG-DRAFT, EV-AUT and the gate iterations until the candidate is frozen (D+4), then M7-MEX, then this unit (README §4: D+5); it may land after R1 (OD-2).
- **Reschedule interplay:** M7-AUTO's `reschedule` settles a still-claimed occurrence before its fire; a summary already fired and queued is not stopped by a reschedule (it was due), only by a pause or a delete (step 2).
