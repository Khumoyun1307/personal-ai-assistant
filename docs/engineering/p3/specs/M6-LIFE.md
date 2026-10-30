# M6-LIFE — Delegation lifecycle: `delegation_end` never skipped, overdue reconciler, end chain, ends on revoke and disable

| | |
|---|---|
| **Unit** | M6-LIFE (S06.1 end at time; S06.4 trigger of the catch-up) |
| **Team / slot** | Josh / J1 |
| **Size** | M (≈ 1 eng-day) |
| **Depends on** | M6-DELEG (README §3: its `end` service). In this design every end goes through F0-S's `end_delegations`, the primitive M6-DELEG's end also uses, so M6-LIFE may start on F0-S and the factories and needs M6-DELEG merged only for the timezone test, which creates through `automation/delegations.py`. Also F0-S (`end_delegations`, `cancel_pending_for_delegations`, `delegations_repo`, `p3_factories`) and F0-R (fire registry with `SkipPolicy.FIRE_LATE`, `DelegationEndRule`, `jobs/reconcile_delegation.py`, `CatchUpRequester` port) |
| **Branch → base** | `p3/m6-life` → `int/p3` |
| **Peer reviewer** | E1 (Codex), dispatched by the TL |
| **QA slot and lane** | QJ1 or QJ2, before merge |
| **Tier** | 1 (TL scope check) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §4.5, §6.1, §7.3, §8.1, §8.2, §11.2, §13, §15; [P3-schema](../contracts/P3-schema.md) §2.1, §2.2, §7.1; ARCHITECTURE §9.4 (lock order), §12.2, §12.4; PRD J5 step 6, FR-AUT-09, §12.2 #8; IMPLEMENTATION_PLAN §8 S06.1, S06.4; decisions OD-7, OD-11, P3-T29 |

## 1. Goal

A delegation stops at its end time even when the job runs late, a fire is lost or the worker was down, and it stops when the owner revokes AI for connected chats or disables Chat Automation. Every such end cancels the delegation's pending autonomous replies, deletes its delegation-only knowledge and asks for exactly one end-of-delegation catch-up. Ending is never behind a flag.

## 2. Scope

**In:**
1. `automation/delegation_lifecycle.py` (new): `fire_delegation_end` (the `delegation_end` fire handler, `FIRE_LATE`) and `end_lapsed` (the body of the overdue reconciler, which also ends delegations on revoke and disable).
2. `jobs/reconcile_delegation.py`: the `overdue_delegations` reconciler function (M6-PLAN owns the `unqueued_delegated` function in the same file).
3. `_wire_autonomy` lines: the fire-handler registration and the reconciler registration (always registered).
4. Tests for S06.1 #3, "a late or skipped end still ends autonomy", S06.4 #4 and "revoke ends delegations"; ARCHITECTURE §12.4.

**Out:**
- Creating the `delegation_end` automation, owner end, supersede (M6-DELEG).
- The catch-up content and its notification (M6-CATCH implements `CatchUpRequester`; this unit only calls it).
- Cancelling actions on the revoke and connection paths themselves (M6-SCHED's bulk paths; M04 code), the `delegation_active` time check (M6-POL), deletion and restore ends (F0-S).

## 3. Design

### 3.1 The end chain (shared by every path here)

```python
EndReason = Literal["time_elapsed", "consent_revoked", "connection_disabled"]


async def _end_and_report(
    session: AsyncSession,
    *,
    account_id: int,
    delegation_ids: Sequence[int],
    reason: EndReason,
    now: datetime,
    catch_up: CatchUpRequester | None,
) -> list[int]: ...
```

1. `ended = delegations_repo.end_delegations(session, account_id=…, delegation_ids=…, reason=reason, now=now)` (F0-S, P3-schema §7.1). It ends only active rows (`cancelled` before `starts_at`, else `ended`), ends their live chat rows, cancels their pending autonomous actions with `delegation_ended` (`proposed`, `approved`, `scheduled`; never `executing`), deletes their delegation-scope knowledge and writes one `delegation_ended` audit row each. It returns only the ids it actually ended, so a second ender gets `[]`.
2. For each returned id: `catch_up.request(session, account_id=…, delegation_id=id, now=now)` — the `delegation_catch_up` notification (dedupe `catchup:{delegation_id}`; held_in_quiet, high) in the same transaction. The catch-up is deterministic (P3-T29, OD-11): no model call, so it needs no consent and is also produced after a revoke. `catch_up` is `None` → log `catch_up_not_wired` with the delegation id and continue; the end itself never fails because of it.
3. Log `delegation_ended` with `account_id`, `delegation_id`, `reason` (codes only).

The caller owns the transaction and has taken its locks (§3.4). Nothing here calls Telegram or a model; the catch-up is delivered after commit by the notification service.

### 3.2 `fire_delegation_end` (the `delegation_end` fire handler)

```python
CatchUpProvider = Callable[[], CatchUpRequester | None]  # late-bound; called at run time


async def fire_delegation_end(
    session: AsyncSession, ctx: FireContext, *, catch_up: CatchUpProvider
) -> None: ...
```

Registered in `_wire_autonomy`: `ctx.fire_registry.register_fire_handler("delegation_end", handler, flag=None, skip_policy=SkipPolicy.FIRE_LATE)` (P3-interfaces §8.1). `handler` is `fire_delegation_end` bound to the late-bound catch-up accessor (`functools.partial`). With `FIRE_LATE` the fire path claims the occurrence however late and never settles it skipped for lateness or expiry; `FireContext.timing` is ignored. The fire path can still skip the handler for reasons outside this unit (the account is not active, the automation was paused or deleted, an invalid spec paused it); in every such case the overdue reconciler (§3.3) ends the delegation, and coverage never depended on the fire (`delegation_active` and `active_covering` check `ends_at` themselves).

Body:
1. Load the automation's spec through `automation/rules.py::load_schedule_rule("delegation_end", spec)` → `DelegationEndRule.delegation_id`. A spec that does not validate → log `delegation_end_spec_invalid` (automation id) and return; the overdue reconciler still ends the delegation (§3.3).
2. `view = delegations_repo.get(session, account_id=ctx.account_id, delegation_id=…)`. `None` (unknown, another account's, deleted or scrubbed) or `status != 'active'` → log `delegation_end_noop` and return. This is the normal case after an owner end, a supersede, a deletion or a restore (P3-schema §7.2, §8).
3. `view.ends_at is None` (standing; never created with this automation) → no-op with a log line.
4. `ctx.now < view.ends_at` (the occurrence fired early, for example after a manual data fix) → log `delegation_end_early` and return without ending. `delegation_active` (M6-POL) and `active_covering` check the time themselves, so coverage never depends on this handler; the overdue reconciler ends it once due.
5. Otherwise `_end_and_report(…, [id], "time_elapsed", now=ctx.now, …)`.

The handler takes no account lock (§3.4) and registers nothing but the catch-up notification. An exception rolls back the fire transaction, which the fire path retries as for any handler; the reconciler is the backstop.

### 3.3 `end_lapsed` and the `overdue_delegations` reconciler

`jobs/reconcile_delegation.py::overdue_delegations(session, now, *, catch_up: CatchUpProvider) -> list[DeferRequest]` (bound with `functools.partial` to match `Reconciler = Callable[[AsyncSession, datetime], Awaitable[list[DeferRequest]]]`) calls `automation/delegation_lifecycle.py::end_lapsed(session, now=now, catch_up=catch_up(), limit=20)` and returns `[]`: it writes and defers nothing (P3-interfaces §7.3: the only P3 reconciler that writes). It is registered unconditionally in `_wire_autonomy`, so it runs every minute inside `system.reconcile_pending`.

`end_lapsed` finds, across accounts, active delegations that must end, with the reason in this precedence (first that applies):

| Reason | Condition | Why here |
|---|---|---|
| `time_elapsed` | `ends_at IS NOT NULL AND ends_at <= :now` | A lost, failed or never-created `delegation_end` fire. `<=`, matching `delegation_active`'s `now >= ends_at` (contract gap G10) |
| `consent_revoked` | the account has no `consents` row with `kind = 'ai_managed'` and `revoked_at IS NULL` (the SQL form of `accounts/service.py::active_consent(..., kind=CONSENT_AI_MANAGED).granted` being false) | `ai_managed` was revoked (`/privacy revoke`) |
| `connection_disabled` | the account has no `business_connections` row with `status = 'active'` and `is_enabled`, and no row with `status = 'stale'` | Chat Automation disabled or removed. There is one connection row per account, rewritten in place on a reconnect (`accounts/connections.py`), so this reads the current connection. A `stale` connection is a rotated id waiting for its replacement and does **not** end delegations; `rights_changed` is not an end reason either (its actions are outdated by M6-SCHED and Tx A denies without `can_reply`) |

Query (one statement, ordered by `ends_at NULLS LAST, id`, `LIMIT :limit`), then per account in id order:
1. Account row `FOR SHARE`.
2. Inside a SAVEPOINT (`session.begin_nested()`): `_end_and_report(…, ids_of_that_account_and_reason, reason, …)`. An error rolls back only that SAVEPOINT; log `delegation_end_failed` with the account id and the exception class, and continue with the next account. The next run retries.
3. Return the number of delegations ended (for the log line `overdue_delegations_ended`, a count).

At most 20 delegations per run; the rest are ended in later runs (one minute apart).

**Why revoke and disable end here and not on their own paths** (contract gap G9): the revoke path is `telegram/control/handlers.py::handle_revoke` (frozen after F0-R) and the disable path is `accounts/connections.py::_settle_pending_actions` (no P3 owner). Neither needs to change for safety: in the same transaction as the revoke or the disable, M6-SCHED's bulk paths already cancel every pending and scheduled autonomous action, and from that commit on `consent_valid` and `connection_active` deny every proposal and claim (send preconditions, DENY, no conversion). What remains is bookkeeping and the catch-up, which this reconciler does within about a minute, with no new coupling.

### 3.4 Locks and transactions

ARCHITECTURE §9.4 order is account → conversation → delegation → action; a level may be skipped, never taken in reverse (P3-schema §7.1).

| Path | Locks, in order | Notes |
|---|---|---|
| `fire_delegation_end` | (the fire path already holds the occurrence row) → delegation (`end_delegations`' UPDATE) → actions | **No account lock**: a data deletion holds the account (`FOR NO KEY UPDATE`) and then deletes automations and their occurrences; taking the account after the occurrence would be the reverse order and could deadlock. The occurrence → delegation → action order matches deletion's (automations, then delegations, then actions) |
| `end_lapsed` | account `FOR SHARE` → delegation → actions | Runs in the reconcile transaction; the reconcilers before it take no row locks (checked on `main` and `int/m05`), so the account is the first lock. No row lock is held across a model or Telegram call. The account `FOR SHARE` delays a concurrent `/pause` (`FOR UPDATE`) until the reconcile transaction commits, so it stays short: at most 20 ends, no network call (NFR-07) |
| Tx A and the release job (for reference) | account `FOR SHARE` → conversation → delegation `FOR UPDATE` → action | A claim either commits before the end (and its send is recorded) or finds its action cancelled |

The handler and the reconciler can race for the same delegation. `end_delegations`' conditional UPDATE lets exactly one end it; the other gets `[]` and requests no catch-up, and the catch-up dedupe key catches anything else.

### 3.5 Wiring (`bootstrap.py`, inside `_wire_autonomy` only)

- `register_fire_handler("delegation_end", partial(fire_delegation_end, catch_up=…), flag=None, skip_policy=SkipPolicy.FIRE_LATE)`. (If F0-R's `FireHandler` type cannot take the keyword, wrap it in a two-argument closure.)
- `reconcilers` += `partial(overdue_delegations, catch_up=…)`, after M6-PLAN's `unqueued_delegated` and before M6-SCHED's `unqueued_releases` (the P3-interfaces §7.3 order).
- `catch_up` is `lambda: ctx.final_ports().catch_up_requester`, F0-R's late-bound accessor (F0-R spec §3.4; contract gap G5): `_wire_surfaces` provides the requester after `_wire_autonomy` has run, so it is read at call time, never at wire time (`final_ports()` raises if called during wiring).

Neither registration depends on a flag (P3-interfaces §10.2 "Not flagged"); with `autonomy.run` off, delegations still end on time.

### 3.6 Edge cases (each has a test in §5)

A fire 6 hours late; a fire whose automation was deleted or paused (for example through the owner's automation controls), or whose account is not active (only the reconciler ends it); a fire for a delegation the owner already ended; a fire before `ends_at`; a corrupt `delegation_end` spec; a delegation whose time and consent both lapsed (one end, reason `time_elapsed`); 25 lapsed delegations (20 now, 5 next run); a failure for one account; a revoke and a stale connection on the same account (ends for `consent_revoked`; stale alone never ends); a delegation that has not started when consent is revoked (`cancelled`, catch-up still requested; M6-CATCH words it); a scheduled autonomous reply and an `executing` one at end time; another account's delegation.

## 4. Files

**Owned (P3-interfaces §15):**
- `automation/delegation_lifecycle.py` (new).
- `jobs/reconcile_delegation.py`: the `overdue_delegations` function only (M6-PLAN owns `unqueued_delegated`; Josh lead sequences M6-PLAN first).
- Test files: `tests/integration/test_delegation_lifecycle.py`, `tests/integration/test_log_hygiene_m6_life.py`.

**Named touch points:** `bootstrap.py` (`_wire_autonomy` lines only); `docs/ARCHITECTURE.md` §12.4 (§7).

**Consumed, never edited:** `automation/{delegations_repo,delegation_models,schedules,rules}.py`, `automation/delegations.py` (M6-DELEG; the timezone test creates through it), `actions/lifecycle.py`, `core/p3_ports.py`, `jobs/reconcile.py`, `accounts/service.py`, `tests/integration/p3_factories.py`.

**Never edit without an ICR** (§15.2): `policy/*`, the send-path `actions/` files, `telegram/*`, `conversations/service.py`, migrations, `privacy/*`, `accounts/connections.py`, `telegram/control/handlers.py`, `automation/schedules.py`, `config.py`, `core/*`.

## 5. Tests (red first)

`tests/integration/test_delegation_lifecycle.py`; real PostgreSQL, `FakeClock`, a recording fake `CatchUpRequester`, the real fire registry and `automation.fire` path (`plan_fire`), `p3_factories.seed_delegation` and `seed_autonomous_action`.

**Named in IMPLEMENTATION_PLAN and the P3 plan (README §3):**
- `test_a_delegation_ends_at_its_owner_local_end_time` (S06.1 #3, "end at time (timezone)"; parametrised): created through M6-DELEG's service with `ends = "2026-11-09 19:00"` for `Asia/Tashkent` (ends 14:00 UTC) and `ends = "2027-03-28 12:00"` for `Europe/Berlin` (the spring-forward day; ends 10:00 UTC), each confirmed at a `FakeClock` time less than 30 days before the end (M6-DELEG's guard); at one second before, still active; at the end time the fire ends it with `time_elapsed` and `delegations_repo.active_covering` returns `None`.
- `test_a_late_delegation_end_still_ends_the_delegation` ("a late or skipped end still ends autonomy", part 1): the worker was down; the occurrence fires 6 h late (past every `max_lateness`) → not skipped, ended `time_elapsed`, catch-up requested once.
- `test_a_missed_delegation_end_is_ended_by_the_overdue_reconciler` (part 2; parametrised: the `delegation_end` automation deleted, paused, or never created): after `ends_at` the reconciler ends it within one run; before `ends_at` it does nothing; at exactly `ends_at` it ends it (`<=`, G10).
- `test_the_catch_up_is_requested_once_when_a_delegation_ends` (S06.4 #4, "catch-up fires at delegation end"): one request per ended delegation, for each of `time_elapsed`, `consent_revoked`, `connection_disabled`; none when nothing was ended.
- `test_revoking_managed_ai_ends_active_delegations` ("revoke ends delegations"): revoke through `/privacy revoke`; the next reconcile run ends every active delegation of the account with `consent_revoked` and requests the catch-ups; another account's delegation is untouched.

**Further tests:**
- `test_disabling_the_connection_ends_active_delegations` (a `business_connection` update with `is_enabled = false`).
- `test_a_stale_connection_does_not_end_a_delegation`.
- `test_ending_cancels_the_delegations_pending_replies_and_deletes_its_knowledge`: `scheduled` and `approved` autonomous actions `cancelled/delegation_ended`, an `executing` one untouched, a held reply with the same `delegation_id` untouched (OD-11), the delegation-scope items gone, saved shareable items kept.
- `test_the_end_handler_is_a_no_op_for_an_ended_or_unknown_delegation` (owner-ended, superseded, deleted, scrubbed spec, another account's id): no change, no catch-up, occurrence marked fired.
- `test_an_early_fire_does_not_end_the_delegation`.
- `test_a_corrupt_delegation_end_spec_is_logged_and_the_reconciler_ends_it_later`.
- `test_the_end_handler_and_the_reconciler_end_a_delegation_once`: both run for the same due delegation → one `delegation_ended` audit row, one catch-up request.
- `test_ending_is_never_gated_by_flags`: `autonomy.run` and every `tool.*` flag off → the fire and the reconciler still end; the registration has `flag=None` and `FIRE_LATE`.
- `test_the_overdue_reconciler_ends_at_most_20_per_run`.
- `test_a_failed_end_for_one_account_does_not_block_the_others` (a SAVEPOINT error on account A; account B's delegation ends).
- `test_time_takes_precedence_over_revoke_as_the_end_reason`.
- `test_rg21_the_lifecycle_never_ends_another_accounts_delegation`: a fire for account A's automation naming account B's delegation id is a no-op; the reconciler ends only lapsed delegations, each under its own account.
- `test_a_delegation_still_ends_when_its_account_is_not_active`: `plan_fire` settles the occurrence `inactive` without calling the handler; the reconciler ends the delegation.
- `test_ending_needs_no_model_call`: every path with a `FakeLLM` that raises on any call → ends and requests the catch-up; zero model calls.

**Log hygiene** (`tests/integration/test_log_hygiene_m6_life.py`): `test_delegation_lifecycle_logs_ids_and_codes_only` — canary text in the delegation's topic and notes; the fire, the reconciler and the end chain log no canary.

**Scenarios:** QA-S6 (QJ1) owns `tests/scenarios/m06/`. When a strict-xfail marked `awaiting M6-LIFE` turns green, tell QJ1, who removes the marker in a commit on this PR's branch so CI stays green (P3-interfaces §15.1; TL ruling R47: the QA owner removes the marker on the feature unit's branch, because a strict XPASS would turn the feature PR red; WORKFLOW §5 is aligned).

Run locally: these two files plus `tests/integration/test_p3_end_primitives.py` and the F0-R fire-registry tests; ruff, format, mypy on `src/assistant/{automation,jobs}`; `lint-imports`.

## 6. Acceptance criteria

1. A time-bounded delegation ends at its owner-local end time, also when the fire is hours late, failed or missing; the end is never skipped and never behind a flag.
2. Revoking `ai_managed` or disabling the connection ends every active delegation of the account within one reconcile run (about a minute); a stale connection never does.
3. Every end cancels the delegation's pending autonomous replies (never an `executing` one), deletes its delegation-scope knowledge, writes one `delegation_ended` audit row and requests exactly one catch-up.
4. Concurrent enders end a delegation once.
5. Every named test in §5 passes in CI; no RG test weakened.
6. Logs carry ids, codes and counts only.
7. ARCHITECTURE §12.4 updated.

## 7. Docs updated in the same PR

ARCHITECTURE **§12.4** (`delegation_end` as built): the `FIRE_LATE` registration, the handler's no-op cases, the overdue reconciler with its three reasons and the 20-per-run cap, why `stale` and `rights_changed` do not end a delegation, the lock order without an account lock in the fire path, and that coverage never depends on the fire (`delegation_active` checks the time).

## 8. Owner decisions

- **OD-11:** the catch-up is deterministic (P3-T29), so ending a delegation after a revoke still produces one. If the owner wanted a model-written catch-up, the revoke case would have to skip it (no consent); this unit would then not request one for `consent_revoked`.
- **OD-7:** standing delegations have no end automation; they end only by the owner, supersede, revoke, disable, deletion or restore. A different answer (an automatic end for standing delegations) adds an end reason, a schema change through J1 (`0016`).

## 9. Risks and notes

- **Contract gap G9:** P3-interfaces §7.3 describes the overdue reconciler as finding only `ends_at < now`; this spec extends it to the revoke and disable reasons instead of editing frozen or unowned files. The TL confirms or assigns a different hook.
- **Contract gap G10:** `<=` instead of `<`, so the reconciler and `delegation_active` agree on the end instant.
- **Contract gap G5:** without a late-bound catch-up accessor the requester is `None` at wire time; this spec then logs `catch_up_not_wired`, and the catch-up for time and revoke ends would be missing (a visible open item, never a failed end).
- **Latency of revoke and disable ends** is up to one reconcile period. Safety does not depend on it (send preconditions deny at once; M6-SCHED cancels in the revoking transaction); only the catch-up arrives about a minute later.
- **The fire path's retries:** a handler exception rolls the fire back and the fire path retries it (bounded); the reconciler covers a permanent failure.
- **Owner automation controls:** `list_automations` and M7-AUTO's controls may show a `delegation_end` automation. Pausing or deleting it never extends a delegation (the reconciler ends it and coverage checks the time), but it is confusing; M7-AUTO should hide or refuse `delegation_end` rows and point to delegation controls (reported to the TL as a cross-unit note, not built here).
