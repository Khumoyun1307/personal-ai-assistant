# M6-PAUSE — Kill-switch completion: Pause button, bot-block auto-pause, pause sources, latency harness

| | |
|---|---|
| **Unit** | M6-PAUSE ★ (S06.3, kill-switch part) |
| **Team / slot** | Josh / J2 |
| **Size** | M (≈ 1 eng-day) |
| **Depends on** | F0-S, M6-SCHED (README §3). F0-R is the foundation prerequisite at CP1. Josh lead sequences the `actions/autonomy.py` edits after M6-TAKE; this is file sequencing, not another unit dependency. |
| **Branch → base** | `p3/m6-pause` → `int/p3` |
| **Peer reviewer** | E1 (Codex; the inbox and triage side), dispatched by the TL |
| **QA slot and lane** | QJ1 or QJ2, before merge, with the mutation table |
| **Tier** | **1A**: Gemini read (David lead), mutation table, TL deep review, part of CP-A |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §1.2, §2.2 (scheduled exits), §12.3 (`pz`), §12.5 (Pause button), §13 (`autonomy_paused`), §15; [P3-schema](../contracts/P3-schema.md) §3.1; ARCHITECTURE §6.1, §6.3, §6.8, §9.4, §18; PRD J9, FR-AUT-06, NFR-07, §21 R2 #2; IMPLEMENTATION_PLAN §8 S06.3 and §10 RG-19; decisions OD-11, OD-31, P2-T23 |

## 1. Goal

`/pause`, the stop word and a new **Pause button** stop all autonomous sending within 5 seconds, and no autonomous send starts after the pause is acknowledged (NFR-07). An owner who blocks the bot gets the same pause automatically. Every pause records its source and timing data, content-free, so the latency can be measured per source. Owner approvals stay valid (P2-T23, OD-31).

## 2. Scope

**In:**
1. The poller fast lane recognises a Pause-button press and a bot block (`my_chat_member` → `kicked`) besides `/pause` and the stop word, and records the real source.
2. `actions/autonomy.py`: two new pause sources (`button`, `bot_blocked`), the `cancelled` count in the `autonomy_paused` audit row; callers add the content-free timing keys (§3.1).
3. `telegram/control/pause_button.py` (new): **only** the `pz` press handler `handle_pause_press` (answers the press and acknowledges after commit). It defines no button builder and no wrapper around one (R99). The button itself is F0-S's `core/autonomy_texts.py::pause_button()` (R45, §3.2), and every caller, in `inbox/`, `memory/`, `agent/` and `telegram/`, uses that function.
4. `telegram/control/inbox_handlers.py`: the `my_chat_member` branch logs the membership status code (never pauses twice, never resumes).
5. Registration of the `pz` route in `_wire_autonomy`.
6. RG-19 extension and the S06.3 kill-switch tests; the latency harness (automated tests plus a dev-bot run recorded in the PR).
7. `actions/autonomy.py::pause_after_control_forbidden(...)` and its one named call site in `inbox/notifications.py` (R46, ARCHITECTURE §6.8; §3.9).
8. ARCHITECTURE §6.1, §6.8 and §18 as built.

**Out:**
- The renderers that show the button (escalation and volume-cap: M6-ESC; catch-up: M6-CATCH; takeover notice: M6-TAKE; delegation-on notice: M6-DELEG). They append F0-S's fixed `pause_button()` (§3.2); this unit owns what happens when it is pressed.
- The `nfr07.sql` query (OPS-AV) and the live timing on Alviro (LIVE-P3, D+7, PRD R2 #2).
- The other call site of `pause_after_control_forbidden` (M7-EXP's control sends: M7-EXP calls it, §3.9).
- Resuming on unblock. Autonomy resumes only through `/resume` or a card button (AGENTS §6.3b; unchanged).
- Takeover (M6-TAKE), the release re-check (M6-SCHED), Tx A (M6-TXA).

## 3. Design

### 3.1 Pause sources and the audit row (`actions/autonomy.py`)

Add two constants and keep the legacy one:

```python
SOURCE_BUTTON = "button"  # the Pause button (pz:pause:p)
SOURCE_BOT_BLOCKED = "bot_blocked"  # my_chat_member: the owner blocked the bot
#: Legacy value (exists today) in audit rows written before P3; no longer written.
SOURCE_FAST_LANE = "fast_lane"
LANE_FAST = "fast"  # paused in the poller's batch transaction
LANE_INBOX = "inbox"  # paused while the inbox processed the update
LANE_SEND = "send"  # paused after a forbidden control send (§3.9)
```

`pause(session, *, account_id, now, source, details=None) -> bool` keeps its signature and behaviour (account `FOR UPDATE`, flag set, `cancel_autonomous_pending`, audit only when the flag changed). It now keeps the ids returned by `cancel_autonomous_pending` and writes `cancelled` (their count) into the audit `details`, next to `source` and the caller's `details`.

Callers put these keys in `details` (ids, epochs, codes and integers only; P3-interfaces §13, extended by contract gap G2):

| Key | Value |
|---|---|
| `lane` | `fast`, `inbox` or `send` (a forbidden control send, §3.9); tool and card pauses omit it |
| `trigger_at` | epoch seconds of the Telegram `date` of the update that paused (a message or `my_chat_member`); `null` for a button press (a `callback_query` has no date) |
| `update_id` | the Telegram `update_id` of that update; `null` when none |
| `apply_ms` | fast lane only: milliseconds from the start of `fast_lane.apply` until that account's lock is held (the lock wait), measured with the injected monotonic function (§3.3) |

The first source that changes the flag is the one recorded. A later `/pause` while paused writes no row (unchanged), so a source is never counted twice. `actor` stays `owner` for every source except `tool`.

### 3.2 The Pause button

**One fixed button.** Label "⏸ Pause automatic replies", callback data `pz:pause:p` (kind `pz`, the fixed token `pause`, which is base62, op `p`; 10 bytes). No `pending_inputs` row, no stored token, no I/O.

**Where it is built (R45, G1 ruled).** The button is a pure helper in `core`, because three of its consumers may not import `assistant.telegram`: the escalation and volume-cap renderers (`inbox/`, M6-ESC), the catch-up renderer (`inbox/`, M6-CATCH) and the delegation-on notice (`agent/tools/delegations.py`, M6-DELEG) (import-linter contracts 1 and 2). F0-S writes `core/autonomy_texts.py::pause_button() -> Button` and `PAUSE_CALLBACK_DATA = "pz:pause:p"` before CP1 (`core` is frozen after it), and every renderer uses it. This unit therefore adds **no** builder (there is no `telegram/control/pause_button.py::pause_button` wrapper); it:
- imports `PAUSE_CALLBACK_DATA` from `core/autonomy_texts.py` in `fast_lane.py` and in the handler, so the button, the fast lane and the handler share one constant;
- may append the button in `telegram/control/takeover.py` and fix its test, only if M6-TAKE merged without it (named touch point, R45, a few lines; M6-TAKE spec §3.5);
- adds no port, no `P3Ports` field and no other builder.

Before enable 3 (shadow), every card that P3-interfaces §12.5 lists with a Pause button must carry it: the TL checks this at the enable (LIVE-P3, OPS-TV3; R48), so a missing button blocks the release instead of being silently omitted.

The token is fixed and never stored, on purpose:
- A kill switch must work on a card of any age. A stored, expiring token would make an old card's Pause fail.
- Pausing is always safe. The only check that matters is that the presser is an allowlisted owner (`callback_query.from.id`, set by Telegram, never by the client), which routing already enforces (`route_update` → `CONTROL`).
- One predicate decides whether a press is a pause, used by both the fast lane and the handler: `is_pause_press(data: str | None) -> bool` — `callbacks.parse(data)` succeeds, `kind == "pz"` and `op == "p"`. The token is not checked. It lives in `telegram/ingress/fast_lane.py` (pure; no new import there beyond `accounts.callbacks` and the constant), and `telegram/control/pause_button.py` imports it from there, so the two can never disagree.

### 3.3 Fast lane (`telegram/ingress/fast_lane.py`)

`poller.py` is frozen after F0-S and stays unchanged: it calls `fast_lane.pausing_owners(...)` and passes the result to `fast_lane.apply(session, result, now)` without looking inside. This unit changes the element type of that list and adds one keyword parameter with a default, so the poller's call is unchanged.

```python
@dataclass(frozen=True, slots=True)
class PauseTrigger:
    telegram_user_id: int
    source: str  # autonomy.SOURCE_COMMAND | SOURCE_STOP_WORD | SOURCE_BUTTON | SOURCE_BOT_BLOCKED
    trigger_at: int | None  # Telegram date (epoch seconds); None for a button press
    update_id: int | None


def pausing_owners(updates, *, owner_ids, bot_id, bot_username) -> list[PauseTrigger]: ...
async def apply(
    session: AsyncSession,
    triggers: Sequence[PauseTrigger],
    now: datetime,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> int: ...
```

`pausing_owners` inspects only the updates this batch actually stored (the poller already filters them). For each update, in `update_id` order:

| Update | Counts when (all must hold) | Source | `trigger_at` |
|---|---|---|---|
| `message` (unchanged rule) | routes to `CONTROL`; `from.id == chat.id`; no forward markers; whole text `/pause` (optionally `@bot`) → `command`, or a stop word (`handlers.is_stop_word`) → `stop_word` | `command` / `stop_word` | `message.date` |
| `callback_query` | routes to `CONTROL` (sender in the allowlist); `is_pause_press(data)` | `button` | `None` |
| `my_chat_member` | routes to `CONTROL`; `chat.type == "private"`; `from.id == chat.id`; `new_chat_member.status == "kicked"`; `new_chat_member.user.id == bot_id` | `bot_blocked` | `my_chat_member.date` |

Everything else is ignored, as today: business updates are never inspected (invariant 5), forwarded text is never a command (invariant 3b), a stranger's update never counts (invariant 12), `edited_message` never counts. A non-integer `date` gives `trigger_at = None` (never a guess). Only the first trigger per owner in a batch is kept.

`apply` records `start = monotonic()`, then for each trigger:
1. resolves the owner's active account (`autonomy.active_account_id`; none → skip);
2. takes that account's lock through a new public wrapper `autonomy.lock_account(session, account_id) -> bool` (the existing `_lock`: `FOR UPDATE`, `populate_existing`) and computes `apply_ms = int((monotonic() − start) × 1000)`;
3. calls `autonomy.pause(session, account_id=…, now=now, source=t.source, details={"lane": "fast", "trigger_at": t.trigger_at, "update_id": t.update_id, "apply_ms": apply_ms})`. `pause`'s own lock is then immediate (the same transaction already holds the row), and its signature is unchanged.

All of this runs in the batch's own transaction (unchanged). It logs `autonomy_paused_fast_lane` with `account_id` and `source` only. A replayed update is never re-applied (unchanged: only newly stored updates are passed in).

The module docstring drops "There is no Pause button in M04" and describes the three triggers.

Why `my_chat_member` belongs in the fast lane: the block is stored and paused in the same commit, so there is no window in which a stored but unprocessed block exists without the pause. A stored, unprocessed row also holds every claim through `inbox_drained` (ARCHITECTURE §9.4), as before.

### 3.4 The `pz` callback handler (`telegram/control/pause_button.py`, new)

```python
PAUSE_ANSWER_TEXT = "Paused."


async def handle_pause_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    press: CardPress,
) -> HandlerResult: ...
```

It matches `routes.CallbackHandler`. The dispatcher reaches it for kind `pz` after the poller's fast lane has normally paused already.
1. `press.op != "p"` → after commit, `gateway.answer_callback(press.callback_id, handlers.CALLBACK_EXPIRED_TEXT)`; `HandlerResult(False, "pause", None, note="unknown_op")`.
2. Otherwise `await autonomy.pause(session, account_id=account.id, now=clock.now(), source=autonomy.SOURCE_BUTTON, details={"lane": "inbox", "trigger_at": None, "update_id": None})`. Idempotent: when the fast lane already paused, no audit row is added and the cancel finds nothing new.
3. After commit (`db.uow.after_commit`): `gateway.answer_callback(press.callback_id, PAUSE_ANSWER_TEXT)` and `gateway.send_text(ControlChat(account.id, press.chat_id), dispatcher.PAUSED_TEXT)`. A rollback sends nothing ("Paused" is never said for a pause that did not commit, invariant 11). The card's keyboard is not edited: other cards keep their button, and this one still works.
4. Returns `HandlerResult(True, "pause", None, note="button")`.

No database work happens after commit and no Bot API call happens before it (AGENTS §5, §7). The handler never resumes anything.

### 3.5 `my_chat_member` in the inbox (`telegram/control/inbox_handlers.py`)

The `my_chat_member` branch keeps logging only, with a content-free status code: `owner_membership_changed` with `update_row_id` and `status` (`kicked`, `member` or `other`). It does not pause: the fast lane paused in the storing transaction, and `ControlInboxHandlers` has no clock (contract gap G16 records why the contract's "auto-pause in `inbox_handlers.py`" moved to the fast lane). An unblock (`member`) never resumes autonomy and sends nothing. Update the module docstring line.

### 3.6 Wiring (`bootstrap.py`, inside `_wire_autonomy` only)

Add `"pz": pause_button.handle_pause_press` to the `ControlRoutes.callbacks` fragment, with a function-local import. A duplicate kind raises at startup (F0-R). The button builder needs no wiring (§3.2).

### 3.9 Auto-pause on a forbidden control send (R46; ARCHITECTURE §6.8)

A `forbidden` result on a `ControlChat` send means the owner blocked the bot, which is the same event as `my_chat_member` → `kicked`. The gateway has no session and a pause inside a send could deadlock against the caller's own transaction, so the pause is its own short transaction, run **after** the caller's transaction committed.

```python
async def pause_after_control_forbidden(
    session_factory: async_sessionmaker[AsyncSession], *, account_id: int, now: datetime,
) -> bool:
```

In `actions/autonomy.py` (this unit's file). It opens one transaction, calls `pause(session, account_id=…, now=now, source=SOURCE_BOT_BLOCKED, details={"lane": "send", "trigger_at": None, "update_id": None})` and returns whether the flag changed. `LANE_SEND = "send"` joins the lane constants. It is idempotent (a second call while paused writes no row), sends nothing and never resumes. No message goes to the owner: a blocked bot cannot message them.

**Call sites** (after commit, in their own `try/except Exception` that logs `pause_after_forbidden_failed` with the exception class name only, so a failure never changes the delivery outcome):
- `inbox/notifications.py` (notification delivery): a named touch point owned by this unit for exactly this call: when `_channel.send` returns a delivery that is not ok and not uncertain and whose `error` is the forbidden code, call it after `_record` returned. The engineer first checks how `GatewayControlChannel.send` reports a forbidden result in `Delivery.error`. At planning it does: `telegram/control/agent_channel.py` sets `error=result.error_class.value`, and `core/delivery.py::ErrorClass.FORBIDDEN` is `"forbidden"`, so the call site tests `delivery.error == ErrorClass.FORBIDDEN.value`. If the check on `int/p3` shows the code is not distinguishable, this unit does **not** edit the adapter: GW-DOC adds the one-line change in `GatewayControlChannel.send` (R100; GW-DOC §3.4) and Josh lead sequences GW-DOC first; the engineer raises it on the integration issue and waits for that merge.
- M7-EXP calls it after its control sends (M7-EXP spec); other control senders may adopt it later.

Fail closed: any other control-send failure (`ambiguous`, `rate_limited`, `network`, `terminal` other than forbidden) never pauses. Tests: `test_rg19_a_forbidden_control_send_pauses_autonomy_and_cancels_scheduled_sends` (RG-19 extension: delivery fake returns forbidden; paused, audit `source = bot_blocked`, `lane = send`, the scheduled action `cancelled/autonomy_paused`, no message sent), `test_an_ambiguous_or_rate_limited_control_send_never_pauses`, `test_the_forbidden_pause_runs_after_the_deliverys_transaction_commits` (a rollback of the caller leaves no pause call before commit; the pause failure does not change the delivery status).

### 3.7 What a pause does to each action state (as built after this unit)

| State | Effect of a pause (any source) | Who guarantees it |
|---|---|---|
| `proposed`, `approved`, `scheduled` with origin `delegation_auto` | `cancelled` / `autonomy_paused` in the pausing transaction | `cancel_autonomous_pending` (M04; `PENDING` includes `scheduled`) |
| a queued `actions.release_scheduled` for such an action | no-op: the action is no longer `scheduled` | M6-SCHED |
| a new autonomous proposal or claim | `autonomy_not_paused` fails → card at proposal (OD-11), `cancelled/autonomy_paused` at claim | M6-POL, M6-TXA |
| `executing` (claim committed) | untouched: the send already started and cannot be recalled; its outcome is recorded truthfully | Tx B (M04); documented in ARCHITECTURE §6.1 |
| owner-approved and dictated actions, fallback cards | unchanged (P2-T23, OD-31, OD-11) | M04 |

### 3.8 Latency harness (NFR-07)

1. **What is recorded** (content-free, per pause; §3.1). `trigger_at` is the Telegram `date` of the pausing message or block (Telegram's clock, one-second resolution). The audit `at` is the poller's batch time passed to `apply` (sampled after `getUpdates` returned, before the account lock). `apply_ms` covers the account-lock wait. The cancellation and the commit follow in the same poller transaction with no network call in between. So for `/pause`, the stop word and a block, the measured latency is `at − trigger_at + apply_ms / 1000` plus the cancellation and commit (milliseconds); `nfr07.sql` (OPS-AV) computes it per source and treats `fast_lane` as a legacy source. A button press has no time in the Bot API, so its row has `trigger_at = null`; its latency is never computed from data and never assumed zero.
2. **Automated tests** prove the mechanics with `FakeClock`, `FakeUpdateSource`, barrier-controlled transactions and an injected monotonic function, never sleeps: `test_each_pause_source_is_audited_with_its_source_and_trigger_time` (the fields), `test_a_fast_lane_pause_commits_with_its_update` (atomic with the store), `test_apply_ms_includes_the_wait_for_the_account_lock` and `test_rg19_button_and_block_race_with_claim` (serialization with Tx A).
3. **Dev-bot run** (AGENTS §9 standing exception; the owner taps). J2 runs the app on the dev bot and the dev database (`just run`). The owner sends `/pause`, `/resume`, the stop word `стоп`, `/resume`; once a card with the Pause button exists on `int/p3` (M6-TAKE or M6-ESC merged), presses it three times with `/resume` in between; and blocks the dev bot once, unblocks it and sends `/resume`. J2 reads only these columns of the `autonomy_paused` audit rows: `at`, `details->>'source'`, `details->>'lane'`, `details->>'trigger_at'`, `details->>'apply_ms'`, `details->>'cancelled'`, and records per source the count and the maximum computed latency against 5 s in the PR description. For the button the owner times press → "Paused." with a stopwatch; that answer is sent only after the pause committed, so the time is an upper bound on the pause latency. A source not measured stays an open item for the R1 rehearsal and LIVE-P3; it never counts as a pass. The agent never opens `var/` or content rows. The block step is also an owner-executed live check of `my_chat_member` → `kicked`, which the TL may record in TF §12 (G4).

### 3.9 Edge cases (each has a test in §5)

A `/pause` behind 999 stored updates; a press from a non-owner; a press with another kind or op; malformed callback data; a business update whose text is `/pause`; a replayed press; a press while already paused (no second audit row); a press on an old card whose callback carries an inaccessible message (no date, chat and message id present: handled normally); a bot block before onboarding (no account: nothing happens, no error); a block followed by an unblock in the same batch (paused, never resumed); a pause while one autonomous action is `executing` and another is `scheduled`; a stop word while the model is unavailable; a pause that waits for the account lock (recorded in `apply_ms`); a failure after `apply` in the batch transaction (nothing paused, the replay pauses).

Accepted, not tested here: a press with no `message` at all (inline mode, which the product never uses) is answered "no longer active" by the frozen `inbox_handlers._callback` even though the fast lane has paused; a `/pause` sent while the poller was down is applied when it is stored, and the recorded latency shows the outage truthfully.

## 4. Files

**Owned (P3-interfaces §15):**
- `telegram/control/pause_button.py` (new).
- `telegram/ingress/fast_lane.py` (M6-PAUSE is its only editor after CP1).
- `actions/autonomy.py`: the pause-source constants, the `pause` audit details (`cancelled`), the new `lock_account` wrapper (§3.3) and `pause_after_control_forbidden` (§3.9, R46). M6-TAKE adds `resume_here` and its helpers in separate functions; Josh lead sequences M6-TAKE first.
- `telegram/control/inbox_handlers.py`: the `my_chat_member` branch only.
- Test files: `tests/integration/test_pause_sources.py`, `tests/integration/test_pause_latency.py`, `tests/unit/test_pause_press.py`, `tests/integration/test_log_hygiene_m6_pause.py`.

**Named touch points in other files:**
- `bootstrap.py`: one route line inside `_wire_autonomy`.
- `inbox/notifications.py`: the after-commit call of `pause_after_control_forbidden` in notification delivery (R46, §3.9); nothing else in the file. If `Delivery.error` cannot tell a forbidden result apart (it can at planning, §3.9), the one-line adapter change belongs to GW-DOC (R100), not to this unit.
- `telegram/control/takeover.py` and `tests/integration/test_takeover_notice.py`: only the fallback of §3.2 (R45).
- `tests/integration/test_kill_switch.py`: append the RG-19 extension tests; in `test_the_poller_fast_lane_pauses_on_an_owner_pause_command`, change the asserted `source` from `fast_lane` to `command` and add `lane == "fast"` (not an RG test; the reason goes in the PR description). M6-TAKE and M6-SCHED also add tests to this file; Josh lead sequences.
- `docs/ARCHITECTURE.md` §6.1 and §18 (§7).

**Consumed, never edited:** `core/channel.py`, `core/autonomy_texts.py` (G1), `core/p3_ports.py`, `telegram/ingress/poller.py`, `telegram/control/dispatcher.py` (`PAUSED_TEXT`), `telegram/control/handlers.py` (`is_stop_word`, `CALLBACK_EXPIRED_TEXT`, `CardPress`, `HandlerResult`), `telegram/control/routes.py`, `accounts/callbacks.py`, `actions/lifecycle.py`.

**Never edit without an ICR** (P3-interfaces §15.2): everything else in `policy/*`, the send-path `actions/` files, `telegram/gateway.py`, `telegram/ingress/poller.py`, `conversations/service.py`, migrations, `privacy/*`, `config.py`, `core/*`, `telegram/control/{dispatcher,routes,handlers,panels}.py`.

## 5. Tests (red first)

All synthetic; real PostgreSQL; `FakeClock`, `FakeUpdateSource`, `FakeTelegramSender`, `FakeLLM`. Seed scheduled autonomous actions with `p3_factories.seed_autonomous_action(state="scheduled")`. Tests marked "pins" check behaviour that already holds and need not fail first.

**IMPLEMENTATION_PLAN §8 S06.3 (kill-switch items) and the RG-19 extension** (`tests/integration/test_kill_switch.py`, appended):
- `test_rg19_the_pause_button_cancels_scheduled_autonomous_sends_and_blocks_the_next`: a `pz:pause:p` press through the poller → account paused, the scheduled `delegation_auto` action `cancelled/autonomy_paused`, its release job sends nothing, a new autonomous action is cancelled at claim; zero business calls.
- `test_rg19_a_blocked_bot_pauses_autonomy_and_cancels_scheduled_sends` (S06.3 #7 "bot blocked → autonomy paused and scheduled sends cancelled"): a `my_chat_member` `member → kicked` from the owner → paused, audit `source = bot_blocked`, scheduled action cancelled; no message is sent to the owner.
- S06.3 #2 "pause during `scheduled` cancels" is M6-SCHED's `test_rg19_pause_during_scheduled_cancels` (command and fast lane); this unit adds the button and the block in the two tests above and does not duplicate it.
- `test_pause_during_executing_is_not_undone_but_no_new_send_starts` (S06.3 #3): one autonomous action `executing` (claimed, Tx B pending) and one `scheduled`; pause → the executing one stays `executing` and its Tx B records the real outcome; the scheduled one is cancelled; a later claim attempt for a new autonomous action is cancelled; exactly one business send in total.
- `test_pause_with_a_1000_update_backlog_still_blocks_the_next_autonomous_send` (S06.3 #5), parametrised over two cases, with a scheduled autonomous action due now, 999 synthetic owner notes and `/pause` as the 1000th update (`limit = 100`, so ten batches):
  - *restart with a backlog* (this process run's ingestion barrier not yet caught up): a release and a claim attempted between every batch WAIT (`inbox_drained`), and the batch that stores `/pause` commits the pause and the cancellation; zero business sends at any point;
  - *backlog during a run* (barrier already caught up): right after the batch that stores `/pause` commits, with the 999 notes still unprocessed, a release and a claim end `cancelled/autonomy_paused` (not WAIT); the pause never waits for the inbox; zero business sends after that commit.
- `test_a_stop_word_pauses_while_the_model_is_down` (S06.3 #6 "stop word with FakeLLM down pauses"): `FakeLLM` raises on every call; the owner sends `стоп` → paused, acknowledgement sent, zero model calls.
- `test_resume_never_follows_an_unblock`: kicked then member in one batch → paused; the account stays paused after the inbox processes both, and no message is sent.
- Preserve unchanged (never weaken): `tests/integration/test_kill_switch.py::test_rg19_pause_blocks_an_autonomous_action_at_claim`, `tests/integration/test_execution.py::test_rg19_an_owner_manual_message_blocks_the_send` and the scenario `test_m04_j9_kill_switch.py::test_pause_blocks_an_autonomous_action_at_claim` (the existing RG-19 tests, IMPLEMENTATION_PLAN §10 and AGENTS §6: never weaken or delete a §6 regression test).

**Fast lane** (`tests/integration/test_pause_sources.py`):
- `test_the_fast_lane_pauses_on_the_pause_button`: paused in the poll's transaction, before the inbox runs.
- `test_the_fast_lane_ignores_a_pause_press_from_a_non_owner`.
- `test_the_fast_lane_ignores_other_buttons_and_malformed_data` (parametrised: `act:<t>:p`, `pz:<t>:x`, `pz::p`, `None`, 65 bytes).
- `test_the_fast_lane_ignores_a_block_that_is_not_the_owners_own_chat` (a `my_chat_member` whose `chat.id != from.id`, whose `new_chat_member.user.id` is another bot, or whose status is `left`).
- `test_a_replayed_pause_press_is_not_applied_again`.
- `test_a_pause_press_while_paused_writes_no_second_audit_row`.
- `test_a_block_before_onboarding_changes_nothing`.
- `test_rg21_a_pause_press_pauses_only_the_pressers_account`: two owners, two accounts; owner A presses → only A paused, only A's scheduled action cancelled.
- `test_rg03_business_pause_text_never_reaches_the_fast_lane`: an owner-authored business message, an edited business message and a connection update with the text `/pause` or a stop word never trigger a pause.

**Latency harness** (`tests/integration/test_pause_latency.py`):
- `test_each_pause_source_is_audited_with_its_source_and_trigger_time` (parametrised over `command`, `stop_word`, `button`, `bot_blocked`): `at == T`, `trigger_at == T − 3 s` (`None` for `button`), `lane == "fast"`, `update_id` set, `apply_ms` an integer ≥ 0, `cancelled` = the number cancelled.
- `test_a_fast_lane_pause_commits_with_its_update`: a failure injected after `apply` inside the batch transaction (for example in the heartbeat write) rolls back the stored update, the pause and the audit row together; the next poll replays the update and pauses.
- `test_apply_ms_includes_the_wait_for_the_account_lock`: a barrier-held transaction keeps the account `FOR SHARE` while the injected monotonic function advances 1.5 s; the recorded `apply_ms ≥ 1500`.
- `test_rg19_button_and_block_race_with_claim`: barrier-controlled transactions prove that a claim either commits before the pause takes the account lock or observes the committed pause; for both the button and the block; no new claim after the pause commit.

**Handler** (`tests/unit/test_pause_press.py` and `tests/integration/test_pause_sources.py`):
- `test_is_pause_press_accepts_any_token_with_kind_pz_and_op_p` (unit).
- `test_the_press_handler_module_defines_no_button_builder` (unit, R99: an AST scan of `telegram/control/pause_button.py` finds no function named `pause_button` and no `Button(` construction; `core.autonomy_texts.pause_button` is the only builder, and no module outside `core/autonomy_texts.py` builds a button whose data is `PAUSE_CALLBACK_DATA`).
- `test_the_pause_press_is_answered_and_acknowledged_only_after_commit`: dispatch inside a transaction that rolls back → no answer, no message, not paused; committed → one `answer_callback` with "Paused.", one `PAUSED_TEXT` message, both observed after commit.
- `test_a_press_with_an_unknown_op_is_answered_no_longer_active`.
- `test_a_press_on_an_inaccessible_card_is_handled`: callback message without a date (chat and message id present) → paused, answered, acknowledged.
- `test_a_pause_press_leaves_owner_approvals_and_fallback_cards_valid` (P2-T23, OD-11, OD-31): an owner-approved action and an autonomy-fallback card (origin `agent_proposal`, `delegation_id` set) keep their states.
- `test_the_pause_press_keeps_the_cards_button`: no `edit_message_reply_markup` call.
- `test_wire_autonomy_registers_the_pz_route` (unit): maps kind `pz` to `handle_pause_press`.
- `test_the_pause_button_is_recognised_by_the_fast_lane` (unit): `core.autonomy_texts.pause_button()` returns the specified label and data, equal to `PAUSE_CALLBACK_DATA` (R45), accepted by `is_pause_press` and within 64 bytes.
- The three §3.9 tests (`test_rg19_a_forbidden_control_send_pauses_autonomy_and_cancels_scheduled_sends`, `test_an_ambiguous_or_rate_limited_control_send_never_pauses`, `test_the_forbidden_pause_runs_after_the_deliverys_transaction_commits`) in `tests/integration/test_pause_sources.py`; the first is the RG-19 test of R46.

**Log hygiene** (`tests/integration/test_log_hygiene_m6_pause.py`): `test_pause_paths_log_codes_only` — a pausing message and a card message carrying canary text; logs and audit rows hold only ids, epochs, integers and codes.

**Tier-1A mutation table** (the PR fills the first four columns; QA the last):

| # | Rule, lock or guard added | Mutation | Named test that must fail |
|---|---|---|---|
| 1 | Fast lane recognises a `pz` press | drop the `callback_query` branch | `test_the_fast_lane_pauses_on_the_pause_button` |
| 2 | Press must come from an allowlisted owner (`CONTROL` route) | skip the route check for callbacks | `test_the_fast_lane_ignores_a_pause_press_from_a_non_owner` |
| 3 | Press must be kind `pz` and op `p` | accept any parsed callback | `test_the_fast_lane_ignores_other_buttons_and_malformed_data` |
| 4 | Bot block pauses (`kicked`) | drop the `my_chat_member` branch | `test_rg19_a_blocked_bot_pauses_autonomy_and_cancels_scheduled_sends` |
| 5 | Block counts only for the owner's own chat and our bot | drop the `chat.id == from.id` or the `user.id == bot_id` check | `test_the_fast_lane_ignores_a_block_that_is_not_the_owners_own_chat` |
| 6 | Unblock never resumes | resume on `member` | `test_resume_never_follows_an_unblock` |
| 7 | Pause cancels pending autonomous actions | remove the `cancel_autonomous_pending` call in `pause` | `test_rg19_the_pause_button_cancels_scheduled_autonomous_sends_and_blocks_the_next` |
| 8 | Acknowledgement only after commit | send the answer inside the transaction | `test_the_pause_press_is_answered_and_acknowledged_only_after_commit` |
| 9 | Real source recorded | record `fast_lane` for every fast-lane pause | `test_each_pause_source_is_audited_with_its_source_and_trigger_time` |
| 10 | Pause is per presser's account | pause every active account | `test_rg21_a_pause_press_pauses_only_the_pressers_account` |
| 11 | Only newly stored updates are applied | pass all updates of the batch to `pausing_owners` | `test_a_replayed_pause_press_is_not_applied_again` |
| 12 | Business/control boundary | accept an owner-authored business pause as a command | `test_rg03_business_pause_text_never_reaches_the_fast_lane` |
| 13 | Pause and claim serialize on the account | remove the account lock from `pause` | `test_rg19_button_and_block_race_with_claim` |
| 14 | `apply_ms` measured after the lock is held | compute it before `lock_account` | `test_apply_ms_includes_the_wait_for_the_account_lock` |
| 15 | Pause stored atomically with its update | apply the pause in its own transaction after the batch commit | `test_a_fast_lane_pause_commits_with_its_update` |
| 16 | The pause never waits for the inbox | apply button/command pauses only in the inbox | `test_pause_with_a_1000_update_backlog_still_blocks_the_next_autonomous_send` |
| 17 | A forbidden control send pauses | drop the call in notification delivery | `test_rg19_a_forbidden_control_send_pauses_autonomy_and_cancels_scheduled_sends` |
| 18 | Only a forbidden result pauses | pause on any failed send | `test_an_ambiguous_or_rate_limited_control_send_never_pauses` |

**Scenarios:** QA-S6 (QJ1) owns `tests/scenarios/m06/`. When a strict-xfail marked `awaiting M6-PAUSE` turns green, tell QJ1, who removes the marker in a commit on this PR's branch so CI stays green (P3-interfaces §15.1).

Run locally: the four new files plus `tests/integration/test_kill_switch.py`, `tests/integration/test_ingestion_barrier.py` and `tests/unit/test_routing.py`; ruff, format, mypy on `src/assistant/{actions,telegram}`; `lint-imports`.

## 6. Acceptance criteria

1. A Pause-button press, `/pause`, the stop word, a bot block and a forbidden control send each pause the presser's (or sender's) account and cancel its pending and scheduled autonomous actions in the transaction that stores the update.
2. Every named test in §5 exists and passes in CI; RG-19 extended; no existing RG test weakened.
3. The audit row of each pause names its real source (`command`, `stop_word`, `button`, `bot_blocked`, `tool`, `card`) with `cancelled`, and for fast-lane pauses `lane`, `trigger_at`, `update_id` and `apply_ms`; ids, epochs, integers and codes only.
4. No path resumes autonomy except `/resume` and a card button; an unblock never resumes.
5. The press is answered and acknowledged only after commit, through the gateway; no Bot API call from ingress.
6. Owner-approved actions and fallback cards are untouched by a pause (OD-31, OD-11).
7. The dev-bot run's per-source latency (computed for `/pause`, the stop word and the block; owner-timed upper bound for the button) is in the PR against 5 s. A source not measured stays an open item for the R1 rehearsal and LIVE-P3 (PRD R2 #2) and never counts as a pass. The executing-send exception is documented.
8. The mutation table is complete and QA killed every mutation.
9. ARCHITECTURE §6.1 and §18 updated.

## 7. Docs updated in the same PR

- ARCHITECTURE **§6.1** (fast lane as built): the three triggers and the bot block, the fixed `pz:pause:p` data and why the token is not checked, the recorded audit fields and how latency is computed from them, and that a pause never stops a send already `executing` (its outcome is recorded).
- ARCHITECTURE **§6.8**: the forbidden control send pauses autonomy after commit through `pause_after_control_forbidden` (source `bot_blocked`, lane `send`), as built.
- ARCHITECTURE **§18**: new row "Owner blocked the bot | `my_chat_member` → `kicked` | Pause in the storing transaction; pending and scheduled autonomous actions cancelled; unblock never resumes | None (a blocked bot cannot message the owner)".
- PR description: the TF §13 docs-check text for the TL (contract gap G4), the dev-bot latency results, and the `fast_lane` legacy-source note for OPS-AV.

## 8. Owner decisions

- **OD-31** (PRD §12.2 #1 versus P2-T23): this spec keeps P2-T23, so a pause stops autonomous sending only and owner approvals stay valid. If the owner rules that a pause also voids approvals, §3.7's last row changes: `pause` would also cancel owner `approved` actions, and the tests for P2-T23 invert. That would be a separate change to `actions/lifecycle.py` (M6-SCHED's file) through an ICR.
- **OD-11:** fallback cards survive a pause. A different answer adds autonomy-fallback cards (`agent_proposal` with `delegation_id`) to the pause cancel.

## 9. Risks and notes

- **Contract gap G1 (button location), ruled R45 and R99:** F0-S adds `core/autonomy_texts.py::pause_button()` and `PAUSE_CALLBACK_DATA` before CP1; the contract §12.5 row names it; every renderer uses it and this unit adds no builder and no wrapper; `telegram/control/pause_button.py` holds only the press handler (§3.2; contract §1.1 and §12.5 align to this, R99).
- **Contract gap G2:** P3-interfaces §13 lists only `source` and `cancelled` for `autonomy_paused`; `lane`, `trigger_at`, `update_id` and `apply_ms` are additions that OPS-AV's `nfr07.sql` reads.
- **Contract gap G16:** the contract puts the auto-pause in `inbox_handlers.py`; this spec does it in the fast lane (the inbox handler has no clock, and the fast lane commits it with the store).
- **Contract gap G3, ruled R46:** ARCHITECTURE §6.8's forbidden-send auto-pause is built here as `actions/autonomy.py::pause_after_control_forbidden` (own short transaction, source `bot_blocked`, lane `send`), called after commit by `inbox/notifications.py` delivery (named touch point of this unit) and by M7-EXP (§3.9).
- **G4 (docs-check, not verified):** before relying on it, the engineer checks the Bot API docs and quotes them in the PR: in a private chat, `my_chat_member` arrives only when the user blocks or unblocks the bot, and status `kicked` means blocked (https://core.telegram.org/bots/api#update, https://core.telegram.org/bots/api#chatmemberbanned). The TL adds the TF §13 row ("docs-checked, not live-verified"); only the owner's block step in the dev-bot run, recorded by the TL with date and client versions, can make it Verified in TF §12.
- **The changed fast-lane source value:** M04 rows say `fast_lane`; new rows say the trigger with `lane = fast`. `nfr07.sql` must treat `fast_lane` as a legacy value (OPS-AV is told in the PR).
- **The button's spinner:** the press is answered only after the inbox processes it (normally within a second). The pause itself is in force earlier, at store time.
- **Race with a claim:** `pause` takes the account `FOR UPDATE`, which Tx A reads `FOR SHARE` first (§9.4), so a claim is either committed before the pause or sees it. The existing race tests in `test_action_execution_races.py` cover the command path; the button and block use the same function and get their own race test.
- **Lock waits:** short transactions that hold the account (`FOR SHARE` in Tx A, the release job, M6-DELEG's confirm and M6-LIFE's reconcile; `FOR NO KEY UPDATE` in privacy requests and M7-EXP's Tx 1) delay a pause by their duration. `apply_ms` makes such a wait visible; none of them may hold the account across a model or Telegram call (AGENTS §13).
