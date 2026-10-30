# M7-NUDGE — The `followups.nudge` job: a draft nudge as an approval card, or copy-ready text when the window is closed

| | |
|---|---|
| **Unit** | M7-NUDGE (S07.2 nudge; J7 step 4 "reminder with a draft nudge") |
| **Team / slot** | Edvard / E3 |
| **Size** | M (≈ 1 eng-day, one fix round included) |
| **Depends on** | M7-FUP (the deadline handler that defers this job in the reminder's transaction, the `follow_up_missed` renderer that shows this job's outcome, `FollowUps(nudge=…)`), NUDGE-POL (`propose(..., trigger=AutomationTrigger)`, the `no_takeover_since` reference from the action's creation, live-card reuse), F0-S (`NUDGE_TASK`, `nudge_queueing_lock`, `DraftReason.FOLLOW_UP_NUDGE`, `FOLLOWUPS_NUDGE`, `actions.trigger_automation_id`, `AutomationTrigger`), F0-R (`jobs/tasks_followups.py` with `FollowUpJobHandlers`). Merges last in the J7 chain NUDGE-POL → M7-FUP → M7-NUDGE (P3-interfaces §15.5) |
| **Branch → base** | `p3/m7-nudge` → `int/p3` (stack on `p3/m7-fup` until it merges) |
| **Peer reviewer** | J1 (Claude; the NUDGE-POL author), dispatched by the TL |
| **QA slot and lane** | QE1, before merge (nudge window paths) |
| **Tier** | 1 (TL scope check) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §2.3, §4.3, §7.1, §7.2, §9.1, §9.2, §10.2, §11.2, §13, §15; [NUDGE-POL](NUDGE-POL.md) §3.3; [M7-FUP](M7-FUP.md) §3.6 step 8, §3.8; ARCHITECTURE §7.1, §7.3, §9.3, §12.2; PRD J7, FR-FUP-06, §11.4, §15; IMPLEMENTATION_PLAN §9 S07.2, §10 (RG-08, RG-17, RG-18, RG-20); decisions OD-9, OD-17, OD-18 (and decisions.md conflict 1); rulings R22 (the correction line reads this job's rows), R47 (QA removes strict-xfail markers), R68 (provenance baseline: a card-confirmed follow-up is stored tainted), R71 (this unit takes no automation, occurrence or task lock), R96 (so the guidance is None in production) |

## 1. Goal

When a follow-up's deadline passes with nothing received, the owner already has the reminder; this job adds a draft nudge for the contact. Inside the reply window it is a normal approval card; outside it the card is `blocked` with the exact text ready to copy. The draft is never sent by itself, under any delegation, and it uses Assist's unchanged `draft_reply.v1`, with the owner's own words as guidance only when the follow-up's stored provenance says untainted. Under R68 every follow-up made by `create_follow_up` is stored tainted (a card-confirmed call counts as tainted), so in P3 production the guidance is always `None` and the draft sees only the chat; the guidance path is built, fail closed and tested with seeded rows, ready if the TL later narrows the baseline (§9). If no draft can be made (not needed, no consent, no budget, refused, stale), the owner is told so in plain words.

## 2. Scope

**In:**
1. `automation/nudges.py` (new): the domain half — the pre-draft checks and plan (`prepare`), the guidance text, the post-draft settle (`settle`: re-checks, `propose(..., trigger=…)`, audit, the "no draft" notice).
2. `jobs/tasks_followups.py` (pre-created by F0-R; owned by this unit after CP1): `NudgeJob`, the job body that calls `prepare`, then the M04 `Drafter`, then `settle`; the registration stays F0-R's.
3. `_wire_automation` lines: the `Drafter` and `NudgeJob` instances, the `followups` job group, and flipping M7-FUP's `FollowUps(nudge=…)`.
4. Tests; ARCHITECTURE §12.2.

**Out:**
- The deadline handler, the reminder and its renderer (M7-FUP); this unit only writes the rows M7-FUP's renderer reads (§3.5).
- The trigger path in `propose()` and the takeover reference (NUDGE-POL).
- Any prompt change: `draft_reply` stays v1 (P3-interfaces §9.2; OD-9). No new run type.
- Any autonomous or delegation-authorized send (FR-FUP-06, OD-18): there is none in P3.
- The approval card's rendering, approval, edit, copy-ready button and execution (M04 S04.3/S04.4, unchanged).

## 3. Design

### 3.1 Layering

`automation` may not import `agent` (`.importlinter`, AGENTS §5), so the job is split the way M05's `MonitorJobs` is: `automation/nudges.py` holds domain logic only and receives the draft as a plain value; `jobs/tasks_followups.py::NudgeJob` (jobs may import `agent`, `automation` and `actions`) runs `prepare` → `Drafter.draft` → `settle`, with no transaction open across the model call (AGENTS §13).

```python
# automation/nudges.py
@dataclass(frozen=True, slots=True)
class NudgePlan:
    account_id: int
    automation_id: int
    occurrence_at: int  # epoch seconds (the job argument)
    deadline: datetime  # automations.run_at
    conversation_id: int
    contact_id: int
    task_id: int
    guidance: str | None  # owner words only from an untainted follow-up (§3.3)


@dataclass(frozen=True, slots=True)
class NudgeDraft:  # a domain value: no agent or pydantic types cross into automation
    status: str  # a DraftStatus value
    text: str | None = None
    run_id: int | None = None
    base_message_id: int | None = None
    shown_through_message_id: int | None = None
    draft_flags: Mapping[str, Any] | None = None  # agent.drafting.draft_flags(result): content-free


class Skip(StrEnum):  # prepare's outcomes that draft nothing
    GONE = "gone"
    NOT_CURRENT = "not_current"
    TASK_CLOSED = "task_closed"
    ALREADY = "already"
    HELD = "held"
    NO_REMINDER = "no_reminder"
    DISABLED = "disabled"


async def prepare(
    session, *, automation_id: int, occurrence_at: int, nudges_on: bool, now: datetime
) -> NudgePlan | tuple[Skip, datetime | None]: ...
async def settle(
    session,
    plan: NudgePlan,
    draft: NudgeDraft,
    *,
    clock: Clock,
    config: ActionsConfig,
    presenter: ApprovalCardPresenter | None,
    scheduler: JobScheduler | None,
) -> str: ...
def guidance(rule: FollowUpRule, *, deadline_local: str) -> str: ...
```

### 3.2 `NudgeJob.run(automation_id, occurrence_at) -> str` (`jobs/tasks_followups.py`)

`NudgeJob(*, session_factory, drafter: Drafter, clock, scheduler, actions_config, presenter, nudges_on: bool)`; wired as `FollowUpJobHandlers(nudge=job.run)`, so the F0-R task `followups.nudge(automation_id, occurrence_at)` (queue `llm`, queueing lock `nudge:{a}:{o}`, no `lock`, no retry; P3-interfaces §7.1) calls it. It returns a content-free code, also logged with the automation id (`nudge_job_finished`). Safe to run twice (§3.6).

1. **Transaction 1:** `result = nudges.prepare(...)`.
   - `(HELD, at)`: after the commit, re-defer itself: `scheduler.defer(NUDGE_TASK, args={...same...}, queueing_lock=nudge_queueing_lock(a, o), schedule_at=at)` (allowed while this job is `doing`: a queueing lock blocks only a second `todo` job; AGENTS §13 trailing re-defer). Return `"held"`.
   - Any other `Skip`: return its value (nothing drafted, nothing written except as §3.3 says).
2. **Draft**, outside any transaction: `outcome = await drafter.draft(account_id=plan.account_id, conversation_id=plan.conversation_id, reason=DraftReason.FOLLOW_UP_NUDGE, guidance=plan.guidance)` (`run_tainted` stays `False`; guidance is `None` unless §3.3 allows it). The M04 `Drafter` checks, before any `agent_runs` row: Ignore mode or a bot chat (`NOT_DRAFTABLE`), `ai_control` and `ai_managed` naming the processor of the `draft_reply` route's model (`NO_CONSENT`, invariant 9), the monthly budget (`OVER_BUDGET`); then one tool-less structured call (`tools=()`, invariant 3a), the stop reason first, one truncation retry with a doubled budget, never a prefill, re-validation, the run row closed on every path, metered (AGENTS §8). Its trigger is `draft:conv:{conversation_id}:follow_up_nudge`. `NotInAccountError` (the chat was deleted between `prepare` and the call) ends the job with the code `"gone"`: no card, no audit, nothing to tell (the chat's data is gone).
3. Map `outcome` to `NudgeDraft(status=outcome.status.value, text=outcome.result.text if DRAFTED else None, run_id, base_message_id, shown_through_message_id, draft_flags=agent.drafting.draft_flags(outcome.result) if DRAFTED else None)`.
4. **Transaction 2:** `code = nudges.settle(...)`. Return `code`.

An infrastructure exception (a database error) propagates; the job has no retry, so the owner keeps the reminder and its "I'm drafting a nudge" line (risk, §9).

### 3.3 `prepare` (transaction 1; reads only)

Transaction 1 takes no row lock and writes nothing, with one exception: the `disabled` branch of step 3, which writes the audit row and may enqueue the §3.5 notice, so it takes that section's locks (account, then the reminder row). It never locks an `automations`, `automation_occurrences` or `tasks` row (R71): M7-FUP's listener and deadline handler own those locks, and a plain read here can neither deadlock with them nor delay a reminder.

1. Load the automation by id (the job carries ids only; the row gives `account_id`; every later statement is scoped by it, RG-21). Missing ⇒ `GONE`. `kind <> 'follow_up'` ⇒ `GONE` (log `nudge_not_follow_up`). Account not `active` ⇒ `GONE`.
2. `epoch_of(automation.run_at) != occurrence_at` ⇒ `NOT_CURRENT` (a follow-up's deadline never moves; defensive).
3. `nudges_on` false (the process's `followups.nudge` flag) ⇒ write the audit row `nudge_not_drafted` `{"status": "disabled"}` (so the reminder can say why) and run the §3.5 notice check ⇒ `DISABLED`.
4. The reminder row: `notifications` with this account and `dedupe_key = f"auto:{automation_id}:{occurrence_at}"`. Missing ⇒ `NO_REMINDER` (its data was deleted). **Quiet-hours follow (OD-17):** status `queued` with `deliver_after > now` (delivery held it to the end of quiet hours) ⇒ `(HELD, deliver_after)`: the card follows the reminder instead of arriving at night. M7-FUP defers this job 60 s after the reminder's commit, so the delivery decision is normally made first.
5. Task (`automation.task_id`, account-scoped): missing or not `open` ⇒ `TASK_CLOSED` (it arrived, or the owner closed it: no draft; the reminder renderer then suppresses or the met notice tells the owner).
6. **Idempotency:** an action with `trigger_automation_id = automation_id AND created_at >= run_at`, or an audit row `nudge_not_drafted` with `subject_type = 'automation'`, `subject_id = automation_id`, `at >= run_at` ⇒ `ALREADY` (a second run never makes a second model call or card).
7. Rule (`load_schedule_rule("follow_up", spec)`; invalid ⇒ `GONE`), `conversation_id = rule.conversation_id`, `contact_id = rule.contact_id`.
8. **Guidance** (the follow-up's owner-authored expectation, only when it was created untainted; **None in P3 production, R96**): only when `automation.provenance` holds `"tainted": false` explicitly (a missing key or `{}` counts as tainted here, so anything unexpected fails closed). M7-FUP writes `tainted: true` for every follow-up its tool creates (R68: `run_origin` counts a held call as tainted, and `create_follow_up` always runs held), so in P3 production this step yields `guidance = None` for every nudge; only a row written some other way (a test factory, or a later narrower baseline the TL approves) reaches the template below. Then `guidance = nudges.guidance(rule, deadline_local=<run_at in the account timezone, "%a %d %b %H:%M">)`:

```text
Write a short, polite follow-up message to this contact. They were expected to {expectation}
by {deadline_local}. The owner's note about it: {description}
```

`expectation`: `contact_replied` "reply"; `keywords` "send a message about “{t1}”, “{t2}”" (the owner's terms as stored); `media_received` "send {a document | a photo | a voice message | a video | any media, joined with "or"}". `description` = `rule.expected.description` (the owner's words, M7-FUP §3.1), omitted with its sentence when empty. At most 600 characters (the description is cut at a word boundary). No contact name (contact-chosen text is never guidance). The template is application text in the guidance input that `draft_reply.v1` already takes ("Owner instruction for this draft", `agent/draft_context.py`); it is not a prompt change. A tainted follow-up gets `None`: the draft then sees only the chat, and the model may well answer `NOT_NEEDED` (told truthfully, §3.5). Because that is every follow-up in P3, expect more "no draft needed" outcomes than the guidance design implies; the trial's usefulness rating and the `nudge_not_drafted` counts show it (§9).

### 3.4 `settle` (transaction 2)

Locks in the ARCHITECTURE §9.4 order: account `FOR SHARE`, then the conversation `FOR UPDATE` (the `agent/drafting.py::_lock_for_proposal` order; `propose` re-takes the same locks harmlessly). Holding the conversation lock serializes with M7-FUP's listener, which closes tasks under the ingestion transaction's conversation lock.

1. Task still `open` (re-read)? No ⇒ return `"task_closed"` (no card, no audit: the met notice tells the owner).
2. **Not drafted** (`draft.status != "drafted"`): audit `nudge_not_drafted` `{"status": draft.status}`; the §3.5 notice check; return `"not_drafted:<status>"`.
3. **Stale:** the chat's newest contact message id (`max(messages.id)` with `direction = 'contact'`) is above `draft.shown_through_message_id` (the contact wrote while the model ran; `propose_draft`'s freshness rule) ⇒ audit `nudge_not_drafted` `{"status": "stale"}`, the §3.5 notice check, return `"stale"`. If that message matched the follow-up, the listener has already closed the task (step 1).
4. **Propose** (NUDGE-POL §3.3):

```python
result = await propose(
    session,
    ProposalRequest(
        account_id=plan.account_id,
        conversation_id=plan.conversation_id,
        origin=ActionOrigin.AGENT_PROPOSAL,
        kind=ActionKind.SEND_MESSAGE,
        text=draft.text,
        base_message_id=draft.base_message_id,
        shown_through_message_id=draft.shown_through_message_id,
        run_id=None,
        tool_call_id=None,
        trigger_turn_id=None,
        draft_run_id=draft.run_id,
        draft_flags=draft.draft_flags,
        delegation_id=None,
    ),
    clock=clock,
    config=config,
    trigger=AutomationTrigger(automation_id=plan.automation_id, occurrence_at=plan.occurrence_at),
)
```

   - The M04 proposal policy decides: window open ⇒ `awaiting_approval` (an approval card); window closed, or any other send precondition failed ⇒ `blocked` with copy-ready text (FR-DRF-06; RG-18). A nudge is never `delegation_auto`, never carries `delegation_id`, never reaches `approved` without the owner's tap (NUDGE-POL; the CHECK `ck_actions_trigger_not_autonomous`). It carries no AI disclosure (PRD §11.4; the CHECK `ck_actions_disclosure_origin`).
   - `ProposalRefused("trigger_not_available")` (the follow-up no longer names the chat, or NUDGE-POL is absent) ⇒ log `nudge_trigger_refused` (ids, code), audit `nudge_not_drafted` `{"status": "failed"}`, the §3.5 notice check, return `"refused"`.
   - `NotInAccountError` (the follow-up was deleted meanwhile) ⇒ return `"gone"`.
   - `result.reused` (a live card with the same trigger, NUDGE-POL §3.1 step 5) ⇒ return `"already"`.
   - New action: when `presenter` is set, `presenter.present_after_commit(session, result.action_id)` and, for `result.superseded_action_id`, `presenter.sync_after_commit(...)` (the `propose_draft` pattern). Without a presenter (contract gap CG-8), M04's `system.sync_action_cards` presents the live or blocked card within about a minute. Audit `nudge_proposed` (`subject_type = 'automation'`, actor `system`) `{"action_id", "state": "awaiting_approval" | "blocked"}`. Return the state.

### 3.5 Telling the owner (the "no draft" line; M7-FUP §3.8)

The reminder is delivered on its own (the reminder never waits for the draft, PRD §15) and its renderer reads this job's outcome at delivery: the card's state, or the `nudge_not_drafted` audit row. So:
- If the reminder is still `queued` when this job finishes, the renderer will show the outcome itself.
- If it was already delivered (or failed), the owner saw "I'm drafting a nudge", so a "no draft" outcome needs one more short message.

**Notice check** (in the same transaction as the audit row): lock the reminder row (`dedupe_key = auto:{a}:{o}`, account-scoped) `FOR UPDATE` after the account lock (delivery takes the account `FOR NO KEY UPDATE` first, so the two serialize, never deadlock). Status `queued` or `suppressed` ⇒ nothing. Any other status (`sent`, `uncertain`, `failed`, `batched`) ⇒ enqueue `follow_up_missed` with `dedupe_key = f"nudgeout:{automation_id}:{occurrence_at}"`, priority `HIGH`, refs `automation_ids`, `task_ids`, `conversation_ids`, `contact_ids` and `alert_kind = "nudge_not_drafted"`, `conversation_id = c`, `scheduler=scheduler`. M7-FUP's renderer turns it into "No draft nudge for “{title}”: {reason}. Write to {name} yourself if you still want to follow up." (contract gap FUP-4/NUDGE-2: this second row and its key are not in P3-interfaces §11.2). A drafted card needs no extra message: the card is the message.

`nudge_not_drafted.status` values: the eight `DraftStatus` values plus `stale` and `disabled` (contract gap NUDGE-1: P3-interfaces §13 says "a `DraftStatus` value"). `EVENT_NUDGE_NOT_DRAFTED = "nudge_not_drafted"` and `EVENT_NUDGE_PROPOSED = "nudge_proposed"` are module constants here; a test pins `EVENT_NUDGE_NOT_DRAFTED == inbox.followup_texts.NUDGE_NOT_DRAFTED_EVENT`.

### 3.6 Idempotency, retries, races

| Case | Behaviour |
|---|---|
| The job runs twice for one occurrence | The second finds the card (any state) or the audit row: `ALREADY`, no model call |
| Two jobs at once | Impossible: the queueing lock `nudge:{a}:{o}` holds one `todo` job; a running job's re-defer (`HELD`) is the only second `todo` |
| Crash after the draft, before transaction 2 | No card, no audit: a later run (none is scheduled; no reconciler, P3-interfaces §7.1) would draft again. The reminder keeps "drafting"; accepted (§9) |
| The contact sends the documents while drafting | The listener closes the task; `settle` step 1 sees it closed: no card |
| The contact writes something else while drafting | `stale`: no card; the owner is told |
| The contact writes between `settle`'s stale check and `propose` | Impossible: `settle` holds the conversation lock from the check through `propose`, and ingestion needs that lock before it stores a message; `propose`'s own `not_stale` rule is the second line |
| The owner taps Done on the task, or deletes the follow-up, during `settle` | Neither takes the conversation lock, so `settle` may still create the card for a task done a moment ago. It is an owner approval card with nothing sent, and a deleted follow-up ends the job as `gone` (`NotInAccountError`); the owner's approval is the only path to a send |
| The reminder's delivery decision is not made within the 60 s start delay (a backlog) | The card can be presented while the reminder is still held for quiet hours: a card is an owner-facing prompt with no send, so this is accepted; the usual order (reminder decision first) holds whenever delivery is not behind |
| The conversation is deleted before or during the draft | `prepare` returns `GONE`, or the `Drafter` raises `NotInAccountError`: job code `gone`, nothing written |
| The owner writes "ok" before the nudge | Not a takeover for the nudge: NUDGE-POL measures from the action's creation (NUDGE-POL §3.2) |
| The owner writes after the card was created | Tx A blocks the send (`takeover`), as for any owner request (NUDGE-POL) |
| A delegation covers the chat, `autonomy.send` on | Still an approval card (OD-18); no delegation counter changes |
| A reopened follow-up whose first card was rejected | A new deadline is a new occurrence epoch only if the follow-up was re-armed before its deadline (M7-FUP §3.10); NUDGE-POL allows a new card when no live one exists |

### 3.7 Wiring (`_wire_automation` in `bootstrap.py`; function-local imports only)

```python
nudges_on = FOLLOWUPS_NUDGE in ctx.settings.feature_flags and ctx.llm is not None
if ctx.llm is not None:
    drafter = Drafter(
        session_factory=ctx.session_factory,
        llm=ctx.llm,
        clock=ctx.clock,
        config=draft_config(ctx.settings),
    )  # bootstrap.draft_config: the draft_reply route
    nudge_job = NudgeJob(
        session_factory=ctx.session_factory,
        drafter=drafter,
        clock=ctx.clock,
        scheduler=ctx.scheduler,
        actions_config=ctx.actions_config,
        presenter=ctx.approval_cards,  # P3WireContext.approval_cards (CG-8 closed by OD-11); None where not built
        nudges_on=nudges_on,
    )
    # fragment: followups=FollowUpJobHandlers(nudge=nudge_job.run)
followups = FollowUps(
    scheduler=ctx.scheduler, clock=ctx.clock, nudge=nudges_on
)  # M7-FUP's line, flipped here
```

The `Drafter` is stateless; a second instance beside M04's uses the same route, effort and budget (`draft_config`). With `ctx.llm` `None` (a role without the LLM), no job group is wired and M7-FUP never requests a nudge.

## 4. Files

**Owned:**

| File | What |
|---|---|
| `src/assistant/automation/nudges.py` (new) | §3.1, §3.3, §3.4, §3.5 |
| `src/assistant/jobs/tasks_followups.py` (pre-created by F0-R; this unit owns it after CP1, P3-interfaces §15.1) | `NudgeJob` (§3.2); F0-R's `register_followup_tasks` and `FollowUpJobHandlers` unchanged |
| Tests (new) | §5 |

**Named touch points:** `bootstrap.py` inside `_wire_automation` only (§3.7, including the `nudge=` argument of M7-FUP's line); `docs/ARCHITECTURE.md` §12.2 (§7).

**Consumed, never edited:** `actions/proposals.py` (`propose`, `ProposalRequest`, `ProposalRefused`; NUDGE-POL's file), `actions/ports.py` (`AutomationTrigger`, `ApprovalCardPresenter`), `actions/states.py`, `agent/drafting.py` (`Drafter`, `DraftStatus`, `draft_flags`), `automation/{rules,schedules}.py`, `automation/followups.py` and `inbox/followup_texts.py` (M7-FUP), `inbox/notifications.py::enqueue`, `core/jobs.py`, `core/flags.py`.

**Never edit without an ICR** (§15.2): `policy/*`; `actions/{executor,lifecycle,states,proposals,ingestion,policy_loader,autonomy}.py`; `telegram/*`; `agent/drafting.py` and `agent/draft_context.py` (no P3 owner); `agent/prompts/draft_reply.*` (unchanged in P3); `config.py`, `core/*`; migrations, `privacy/*`.

## 5. Tests (red first)

Real PostgreSQL 18; `FakeClock`; `FakeLLM` (scripted `DraftResult`s, prompts and `tools` inspected); `FakeTelegramSender`; the real M7-FUP deadline handler and fire path to produce the reminder and the job; NUDGE-POL's `propose`; the real approval and execution path (`execution_env.harness`) for the send tests. Files: `tests/integration/test_nudges.py`, `tests/integration/test_nudge_flow.py`, `tests/integration/test_log_hygiene_m7_nudge.py`.

**Named in IMPLEMENTATION_PLAN S07.2 and the unit table (README §3):**
- `test_window_closed_at_deadline_gives_copy_ready_only`: the contact's last message is 25 h old at the deadline → reminder, then the job → one action `blocked/window_closed` with `trigger_automation_id` set; its card offers the text to copy; zero business sends; approving is impossible.
- `test_a_nudge_only_via_an_approval_card`: window open → `awaiting_approval`, origin `agent_proposal`, `delegation_id` NULL, `disclosure_included` false; no business call until the owner approves; after approval and Tx A, exactly one send.
- `test_rg17_nudge_drafts_are_given_no_tools`: the FakeLLM request for the nudge has `tools == ()`, run type `draft_reply`, prompt `draft_reply.v1`.
- `test_rg08_without_managed_consent_no_nudge_is_drafted_and_the_reminder_says_so`: `ai_managed` revoked → no model call, no run row, audit `nudge_not_drafted{no_consent}`; the reminder (delivered after the job) says "AI for your connected chats is off".
- `test_rg18_a_nudge_approved_after_the_window_closed_is_never_sent`: card at 23:00, window closes 23:30, owner approves at 23:40 → `expired`/`blocked`, zero business calls.
- `test_rg20_no_text_or_result_reports_the_nudge_as_sent_before_it_is`: the reminder's nudge line for `awaiting_approval`, `blocked`, `approved` and `succeeded`; only the last says "sent"; the job's codes are states, never "sent".

**Further tests:**
- `test_the_reminder_never_waits_for_the_draft`: FakeLLM blocks; the reminder is delivered with "I'm drafting a nudge"; the card arrives after the call returns.
- `test_not_needed_over_budget_refused_and_invalid_give_a_truthful_no_draft_line` (parametrised over the `DraftStatus` values; one audit row each; the reminder line per status).
- `test_a_no_draft_outcome_after_the_reminder_went_out_sends_one_short_notice` (`nudgeout:{a}:{o}`, `alert_kind = "nudge_not_drafted"`) and `test_no_extra_notice_while_the_reminder_is_still_queued`.
- `test_guidance_is_the_owners_expectation_only_for_an_untainted_follow_up`: FakeLLM's prompt contains the "Owner instruction for this draft" block with the description for `tainted: false`; none for `tainted: true` or `{}`; never the contact's name.
- `test_a_second_run_makes_no_second_draft_or_card` (after a card; after a "not drafted" audit row).
- `test_a_met_follow_up_drafts_nothing` (task closed before the job: no model call) and `test_a_match_during_drafting_leaves_no_card`.
- `test_a_contact_message_during_drafting_drops_the_draft_as_stale`.
- `test_a_held_reminder_holds_the_nudge_until_quiet_hours_end` (deliver_after 08:00 → re-deferred to 08:00; no card before).
- `test_a_nudge_under_an_active_delegation_is_still_a_card` (autonomy.send on, `AUTONOMY_LANGUAGES` set).
- `test_followups_nudge_off_at_run_time_drafts_nothing_and_says_so` (`disabled`).
- `test_a_refused_trigger_is_reported_as_no_draft` (a follow-up whose `refs` no longer name the chat → `refused`, audit `failed`).
- `test_rg21_the_nudge_job_reads_only_its_automations_account` (a job naming account A's automation never reads B's chat or writes B's rows).
- `test_the_nudge_event_names_match_the_renderer`.
- `test_the_follow_up_handler_requests_a_nudge_only_when_wired` (with this PR's wiring: `remind_with_draft` + flag + LLM → requested; `remind`, flag off or no LLM → not requested, no "nudge" line).
- `test_a_follow_up_made_by_the_tool_gets_no_guidance` (R68, the production path: a follow-up confirmed through `create_follow_up` is stored `tainted: true`, so the FakeLLM prompt for its nudge holds no "Owner instruction for this draft" block).
- `test_the_nudge_job_takes_no_automation_task_or_occurrence_lock` (R71: another session holds `FOR UPDATE` on the follow-up automation, its task and its occurrence row; `prepare` and `settle` neither block nor deadlock, and M7-FUP's listener and handler are not delayed by a running nudge job).
- `test_a_deleted_conversation_ends_the_job_quietly` (`GONE` before the call; `NotInAccountError` from the `Drafter` after it: code `gone`, no audit row, no card, no notice).
- `test_a_nudge_blocked_for_a_reason_other_than_the_window_is_still_copy_ready_and_the_reminder_does_not_say_window` (a `blocked/takeover` action: the card offers the text to copy; the reminder line, read from `state_reason`, does not claim the window closed).
- `test_the_disabled_branch_writes_the_audit_row_and_the_notice_under_the_notice_locks` (`followups.nudge` off at run time: audit `nudge_not_drafted{disabled}`, one `nudgeout:` notice when the reminder was already sent, none while it is queued).
- `test_a_nudge_carries_no_disclosure_and_no_delegation_id_under_any_delegation` (the two CHECKs are the backstop: `disclosure_included` false, `delegation_id` NULL, `ck_actions_trigger_not_autonomous` holds).

**Log hygiene** (`tests/integration/test_log_hygiene_m7_nudge.py`): `test_nudge_job_logs_ids_and_codes_only` — canary text in the description, the draft body and the contact name; no canary in logs or audit details.

**Scenarios:** when a QA-S7 J7 scenario (window-closed copy-ready, nudge approval) marked `awaiting M7-NUDGE` turns green with this PR, tell QE1 on the unit's issue; QE1 removes the strict-xfail marker in a commit on this PR's branch (R47; P3-interfaces §15.1). Do not edit `tests/scenarios/m07/**` yourself.

Run locally: these files plus `tests/integration/test_nudge_policy.py` (NUDGE-POL), `tests/integration/test_followups.py` (M7-FUP) and the M04 draft tests `tests/integration/test_drafting.py`; ruff, format, mypy on `src/assistant/{automation,jobs}`; `lint-imports` (the `automation` → `agent` ban must hold).

## 6. Acceptance criteria

1. A missed follow-up with `remind_with_draft` produces at most one nudge per deadline: an approval card inside the window, a `blocked` copy-ready card outside it; never `delegation_auto`, never sent without the owner's tap, never with a disclosure line.
2. The draft uses `draft_reply.v1` unchanged, with no tools; guidance only from a follow-up whose stored provenance is explicitly untainted (none made by the tool in P3, R68); consent and budget re-checked when the job runs; the job takes no automation, occurrence or task lock (R71).
3. Every outcome without a card is told to the owner exactly once: in the reminder if it is still queued, else in one short notice.
4. The reminder is never delayed by the draft; a reminder held for quiet hours holds the card too.
5. Re-runs make no second model call or card; a met follow-up or a stale draft makes no card.
6. Every named test in §5 passes in CI; RG-08, RG-17, RG-18 and RG-20 extended; no RG test weakened.
7. ARCHITECTURE §12.2 updated.

## 7. Docs updated in the same PR

**ARCHITECTURE §12.2**, the `followups.nudge` row as built: arguments (ids only), queue `llm`, queueing lock, no lock, no retry, deferred inside the reminder's fire transaction (M7-FUP), the quiet-hours hold, idempotency through `trigger_automation_id` and the audit row, the "no draft" notice. The PR description restates the outcome codes and the audit vocabulary for M7-FUP's renderer, the contract gaps, and the open items for M07.md.

## 8. Owner decisions

- **OD-18** (and conflict 1): nudges always need approval, even under a delegation; the takeover reference is the nudge's creation (NUDGE-POL). If the owner allowed delegation-authorized nudges, §3.4 step 4 would gain a second origin path through M6-PLAN's planner (an ICR and new tests).
- **OD-17:** follow-up deadline alerts wait for quiet hours, so the card follows the held reminder (§3.3 step 4). If the owner wanted deadline alerts at once, the hold would never trigger and nothing else changes.
- **OD-9:** the nudge uses the `draft_reply` route and `draft_reply.v1` (no prompt change, so no E2 re-run under AGENTS §8). If nudge quality later needs a dedicated prompt version, that is a gated prompt change with an E2 re-run and the owner's cost approval.

## 9. Risks and notes

- **Contract gaps** (reported to the TL): **NUDGE-1** `nudge_not_drafted.status` also takes `stale` and `disabled`; **NUDGE-2** (= M7-FUP's FUP-4) the second `follow_up_missed` row `nudgeout:{a}:{o}` with `alert_kind = "nudge_not_drafted"`; **CG-8** (already raised by M6-PLAN and M6-SCHED) `P3WireContext` has no approval-card presenter, so a new nudge card can take up to a minute to appear; the `Drafter` is built in `_wire_automation` from `bootstrap.draft_config` because `P3WireContext` carries no drafter (no contract change needed).
- **NUDGE-3 (R68 consequence, accepted for P3 by R96; open item to revisit after the trial):** the guidance path is dead in production because every card-confirmed follow-up is stored tainted, so `guidance` is `None` for every production nudge and drafts are generic or "not needed". The owner's own description (what they wait for) never reaches the draft, so a nudge may be generic or "not needed". If that proves too weak in the trial, the fix is a TL-approved narrower baseline for follow-ups whose card text equalled the owner's own words (an M7-FUP and contract change, not this unit's), never clearing `ctx.held`.
- **Rulings:** NUDGE-1 and NUDGE-2 follow R32 and R78 (the contract adopts the spec's more precise text: the extra `nudge_not_drafted` statuses and the second `follow_up_missed` row with `alert_kind = "nudge_not_drafted"`); CG-8 (no presenter in `P3WireContext`) is accepted as written.
- **Nudge quality is not gated:** E2 has no nudge cases (DATA-E3F adds informational follow-up monitor cases only). Every nudge is an approval card the owner reads first; the trial's weekly rating covers usefulness.
- **No reconciler:** a crash between the draft and transaction 2 leaves the reminder saying "drafting" with no card. The window is one model call; accepted and listed as an open item.
- **Delegation counters:** a nudge never has `delegation_id`, so it never counts against a delegation's caps and never appears as an autonomous send (NUDGE-POL G15).
