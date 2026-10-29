# OPS-TV3 — P3 trial verifier: `p3-verify release`, `trial-check`, `script`

| | |
|---|---|
| **Unit** | OPS-TV3 — the three check subcommands of `tools/acceptance/p3_verify.py`: `trial-check` (the P2 send audit for every origin plus seven P3 checks that pass a `delegation_auto` action only when every one of them holds, failing closed on any missing or malformed fact), `release` (the P3 schema, flags, settings, the v6 notice and the enable guards) and `script` (`P3-nn` markers, with P2 compatibility); plus `docs/engineering/p3/qa/p3-verifier.md`, which records how P3 supersedes the P2 verifier's rejection of every `delegation_auto` |
| **Owner slot** | Senior QA (SQ, Codex), launched by the TL (`codex exec`, workspace-write in SQ's own worktree `..\alviro-wt\sq-ops-tv3`, `COMPOSE_PROJECT_NAME=sq`) — not the send-path team's vendor (P3-T33) |
| **Reviewer** | Peer: **J1** (Claude, dispatched by the TL; the semantics must match M6-TXA's claim and M6-SCHED's release). Extra: **David lead** (Gemini) read of the fail-closed logic, ≤ 1 h, non-blocking unless it alleges an AGENTS §6 break with a failing test (the P2 #123 lesson). **TL deep review** |
| **Size** | M (≈ 1 engineer-day, calendar D+1 → D+4) |
| **Depends on** | F0-S (schema `0015` names; the five new `StateReason` values appended to `p2_verify.REASONS`; `tests/integration/p3_factories.py`), F0-E (`tools/acceptance/p3_verify.py` skeleton, the empty `tools/acceptance/p3_checks.py`, `deploy/server.sh` `p3-verify` and `trial-check`), the decision-record shape (P3-interfaces §2.4, §3.4 "Completeness", plus the `limits` object M6-TXA stores in it, R84) and the audit events of P3-interfaces §2.2 and §13 — contract only. The positive controls through real producers turn green as M6-TXA, M6-TAKE, M6-PAUSE, M6-DELEG and M6-PLAN merge (§4.3) |
| **Branch → base** | `p3/ops-tv3` → `int/p3` (draft stacked on `p3/f0-e` before CP1). **Must merge before R1** (CP-R1; enable 3 needs it deployed) |
| **QA lane** | none (SQ is QA); peer + Gemini read + TL deep review (WORKFLOW §6.3) |
| **Normative inputs** | AGENTS §6 (invariants 1, 6, 8, 10, 13), §9, §11; PRD §21 R2 #7, NFR-06, NFR-07, FR-AUT-04/08; ARCHITECTURE §9.3, §9.4; P3-schema §2.1–§2.2, §3.1, §3.3, §7.1, §8; P3-interfaces §2.1–§2.5, §3.2–§3.6, §4.1, §5.1, §6.1, §10, §13, §14.2, §15.1; [F0-E](F0-E.md) §3.7–§3.8; [M6-TXA](M6-TXA.md) §3.2; [M6-SCHED](M6-SCHED.md) §3.2–§3.4; [M6-TAKE](M6-TAKE.md) §3.3–§3.4; [M6-PAUSE](M6-PAUSE.md) §3.1; [M6-PLAN](M6-PLAN.md) §3.6; [PV6](PV6.md) §9 note 1; `docs/engineering/p2/qa/p2-verifier.md`; `tools/acceptance/p2_verify.py`; decisions.md §C conflict 29; decisions OD-4, OD-5, OD-6, OD-31, OD-32, P3-T19, P3-T20, P3-T33, P3-T46, P3-T47, P3-T51, P3-T65; TL rulings R10 (P3-T47), R14 (P3-T51), R36, R40, R84 (decision records and non-content columns only; never `delegations.spec`), R48 (the Pause button on every card is a release check that no database fact can prove: §3.7), R49, R56 (`trial-check` moves to this verifier at R1) |

## 1. Goal

Give the TL a daily, independent, content-free audit that can pass the Personal v1 trial. The P2 verifier fails every `delegation_auto` action by design (`p2_verify.py` `_trial_history`: `autonomous_origin`). P3's `trial-check` keeps the six P2 checks for every origin and passes an autonomous action only if **all** of these hold, from facts stored in the database. From R1 (R56) the server action `just prod <ip> trial-check` runs this same code (F0-E §3.7), so the daily check, the M04-trial interval and every autonomous action are audited by one tool; `p2-verify` stays for the P2 views:

1. a stored ALLOW_AUTO execution decision recording a passing check for each of the ten ARCHITECTURE §9.3 autonomy rules, traced to the planner's ALLOW_AUTO proposal;
2. the delegation active and covering the chat at claim, with `delegation_id` set;
3. `disclosure_included` whenever the disclosure was required;
4. the per-chat-hour and per-delegation caps respected;
5. no claim after a pause or a counting takeover;
6. an overdue `scheduled` action never sent (claimed inside `[scheduled_for, late_after)`);
7. the send inside the reply window.

Any missing, malformed or contradictory fact fails the check (the P2 #123 lesson: incomplete histories fail, they are never skipped). The checks never read message text, names or wording. A send with no claim at all fails too: the seven checks iterate claims, so without a rule for "no claim" they would pass vacuously (§3.3.0).

## 2. Deliverables (exact files)

| File | Content |
|---|---|
| `tools/acceptance/p3_checks.py` (F0-E created it empty; SQ owns it) | `SUBCOMMANDS = (RELEASE, TRIAL_CHECK, SCRIPT)` (F0-E's `Subcommand`); the public `evaluate_trial(...)` API that OPS-VIEWS' `report` consumes (§3.4); constants mirrored from `src/` with drift tests (§3.9). Standard library + psycopg + `p2_verify` (same directory) only; never imports `assistant.*`. Import the sibling the way F0-E's `p3_verify.py` loads its subcommand modules, so it works both as a script on the server and as `tools.acceptance.p3_checks` in tests (`tests/unit/test_p2_verify.py` imports `from tools.acceptance import p2_verify`) |
| `docs/engineering/p3/qa/p3-verifier.md` (new; SQ owns `qa/**`) | §3.10 |
| `tests/unit/test_p3_checks.py` (new) | Pure history logic per check on in-memory projections; drift tests (§4.1) |
| `tests/integration/test_p3_trial_check.py` (new) | Real PostgreSQL; the verifier run as a subprocess like `tests/integration/test_p2_verify.py`; one fail-closed test per check and code (§4.2) |
| `tests/integration/test_p3_trial_check_producers.py` (new) | Positive controls through the real producers, strict xfail until their units merge (§4.3) |
| `tests/integration/test_p3_release_check.py`, `tests/integration/test_p3_script_markers.py` (new) | §4.4, §4.5 |
| `tests/integration/test_log_hygiene_ops_tv3.py` (new, from F0-E's template) | §4.6 |

**Not edited:** anything under `src/`; `tools/acceptance/p2_verify.py` (frozen; F0-S appends the five reasons to `REASONS`, nothing else); `tools/acceptance/p3_verify.py` (P3-interfaces §15.1 lists it as SQ's, but this unit needs no change to it; OPS-VIEWS and OPS-RT3s register through it too, so a needed change is an ICR to the TL); `deploy/server.sh` (F0-E's `p3-verify` and `trial-check` actions already pass arguments through); migrations; other units' tests. **Pending a TL ruling (contract gap):** a "P3 verification and daily trial audit" section in `deploy/runbooks/deploy.md`, which P3-interfaces §15.1 assigns to nobody; until ruled, the procedure lives in `p3-verifier.md` and [LIVE-P3](LIVE-P3.md).

## 3. Content

### 3.1 Fail-closed rules (apply to every check)

1. **No default passes.** Every fact a check needs is read from the one `REPEATABLE READ`, read-only snapshot the F0-E CLI opens. A fact that is absent, NULL where the contract requires a value, malformed, out of range or contradicting another stored fact makes the check FAIL with a fixed `*_missing`, `*_malformed` or `*_mismatch` code. No check is skipped for a `delegation_auto` action in scope.
2. **JSON is untrusted structure.** `audit_log.details`, `actions.policy_decision` and `actions.draft_flags` are read only through SQL `CASE` guards (`jsonb_typeof`, regex, `= ANY(<allow-list>)`), the `p2_verify.py` pattern; a value failing its guard reaches Python as `NULL` and fails its check. Python never receives raw JSON.
3. **Complete histories, never windowed.** The interval `[--since, --until)` only selects the actions in scope (P2's rule: created in it, or with an action audit event in it, or with a linked message created in it). Their pause/resume, takeover/resume, delegation, prior-send and prior-claim histories are read in full, from the oldest retained row (audit rows are kept 1 year).
4. **Ordering.** Two writers that take the same lock as Tx A insert their audit rows after taking it, so for those pairs `audit_log.id` order is commit order: account (`autonomy.pause`/`resume` take it `FOR UPDATE`, Tx A `FOR SHARE`), conversation (ingestion and the supersede confirm take it `FOR UPDATE`, Tx A `FOR SHARE`), delegation (`end_delegations` callers and Tx A both `FOR UPDATE`). These checks compare audit ids, never `at`. Time windows (caps, schedule times, reply window, delegation start and end) use the claim's own `at`, which equals `actions.attempt_started_at` (both are Tx A's `now`, read after its locks). This needs the `audit_log.id` sequence to hand out ids in insert order: `trial-check` first reads `pg_sequences.cache_size` for `pg_get_serial_sequence('app.audit_log', 'id')`; anything other than `1` ⇒ `RESULT: NOT EVALUABLE (audit_sequence_cache)`, exit 2.
5. **Exceptions are printed, never silent.** A non-violation exists only where the contract or a deletion receipt explains the missing fact. It prints as `PASS <check> account=<a> action=<id> code=<code>` (P2's `message_deleted_after_success` pattern), waives only that check for that action, and never another check.
6. **Unknown values fail.** An origin, state, reason, event shape or outcome outside the allow-lists FAILs, as in P2.
7. **No RESULT is never a pass.** Exit 0 only with `RESULT: PASS`; exit 2 (error or NOT EVALUABLE) is not a pass (LIVE-P3 §3.11 treats a second exit 2 on one day as a FAIL).
8. **Tenant scoping (AGENTS §6 invariant 12).** Every join and subquery carries the action's own `account_id`: a delegation, link, audit row, message or action of another account is never evidence for, and never a violation of, an action (the P2 queries do the same). A history row that exists only under another account counts as missing.
9. **The verifier never recomputes from configuration it cannot see.** It reads no setting to decide a check except the allow-listed `release` lines (§3.7); limits come from the claim's stored decision record (§3.3.4, R84), times from stored columns and audit rows. Flag and setting history is not stored, so "the flag set did not change" is proven by `release --expect-flags`/`--expect-languages`/`--expect-setting` on every daily check (§3.7), not by `trial-check`.

### 3.2 `trial-check [--since ISO] [--until ISO] [--shadow]`

- Arguments as P2 (`--since` default the Unix epoch, `--until` default now; both ISO with a timezone; `--since >= --until` ⇒ usage on stderr, exit 2, before the first SQL statement). `--shadow` adds the shadow check (§3.3.8).
- **Schema precondition:** the `0015` columns of P3-schema §3.1 and the tables of §2 present (`information_schema`); otherwise `RESULT: NOT EVALUABLE (schema)`, exit 2. Then the sequence precondition (§3.1 rule 4).
- **P2 compatibility:** for every action in scope, build `p2_verify._TrialAction` and `p2_verify._TrialEvent` with the two P2 queries copied verbatim from `p2_verify.trial_check` (a comment names the source; `test_p3_trial_check_matches_p2_on_p2_histories` pins the copy) and call `p2_verify._trial_history(action, events, fail)` unchanged. For origin `delegation_auto` only, the `fail` passed in **drops** the `approved_before_claim` codes (`autonomous_origin`, `missing_matching_approval`) and P3's chain rule (§3.3.0) evaluates `approved_before_claim` instead; every other P2 code (`one_send_per_action`, `no_send_after_terminal`, …) and the P2 `hash_match`, `recipient_matches` and `no_orphan_sends` loops apply to every origin unchanged. Unknown origins keep P2's `unknown_origin`.
- **Output** (P2's format, so the trial log reads the same): for each of the six P2 checks, then the seven P3 checks (then `shadow_no_autonomous_action` with `--shadow`), every violation as `FAIL <check> account=<a> <action|message|account>=<id> code=<code>` (plus `rule=<name>` for `rule_missing`/`rule_failed`, and `ms_before_claim=<n>` for `owner_message_before_claim`), then `FAIL <check> violations=<n>` or `PASS <check>`; the printed non-violations; P2's state counts and per-action state lines; the P3 counts (§3.5); the uncertain autonomous actions with UTC and owner-local times; `total_sends=<n>`; `RESULT: PASS` or `RESULT: FAIL (<n> violations)`. Exit 0 / 1 / 2.

### 3.3 The checks for `delegation_auto`

"The claims" of an action are its `action_claimed` audit rows (subject `action`, `before = approved`, `after = executing`, actor `system`). Each claim is checked separately where a check is about time; the stored decision once per action. `d` = `actions.delegation_id`, `c` = `actions.conversation_id`, `t` = the claim's `at`, `k` = the claim's audit id.

#### 3.3.0 `approved_before_claim` (P3 rule for this origin)

For each claim of version `v`, in audit order, all of: an `action_auto_approved` row (`proposed → approved`, version `v`, actor `system`) before it; an `action_scheduled` row (`approved → scheduled`) after that; an `action_released` row (`scheduled → approved`) after the latest `action_scheduled` and before the claim. Any `action_approved` row (an owner approval) on a `delegation_auto` action fails, and so does a `delegation_auto` row with `trigger_automation_id` set (the schema CHECK forbids it; checked anyway).

**An action with no claim.** Every check below is evaluated per claim, so an action with none would pass them all vacuously. A `delegation_auto` action in state `executing`, `succeeded`, `delivery_uncertain` or `failed`, or with `telegram_message_id` set, or with a linked `assistant_sent` message, but with no `action_claimed` row, fails `no_claim_for_send` (P2 catches some of these shapes under `one_send_per_action`; this closes the rest). It is reported once, under this check; the per-claim checks have nothing to evaluate for it, and the run is `FAIL` regardless.

| Code | When |
|---|---|
| `no_claim_for_send` | a send, an attempt state or a Telegram id without any `action_claimed` row (above) |
| `missing_auto_approval` | no matching `action_auto_approved` before the claim |
| `missing_schedule` | no `action_scheduled` between it and the claim |
| `missing_release` | no `action_released` after the latest `action_scheduled` and before the claim |
| `owner_approval_on_autonomous` | an `action_approved` row exists for the action |
| `autonomous_nudge` | `trigger_automation_id IS NOT NULL` |

#### 3.3.1 `autonomy_decision_recorded`

**Stored execution decision.** `actions.policy_decision`, parsed in SQL: `phase` (`'execution'` or other), `outcome` (allow-listed `Outcome` value or `invalid`), and per element of `checks`: `name` (a string; kept only if it is one of the ten `AUTONOMY_RULES`, else `other`) and `passed` (boolean, else NULL). Pass when `phase = 'execution'`, `outcome = 'allow_auto'`, each of the ten rules appears exactly once with `passed = true`, and no element has `passed` false or NULL. (M6-TXA stores the claim's decision in the claim update; a 429 return leaves it; nothing else overwrites it while the action can still deliver.)

One printed exception: when the P2 history shows the action's **last** claim closed as `action_rate_limited` (definite non-delivery) and the action then ended `cancelled` through Tx A (the decision is the execution DENY record of that later attempt), print `PASS autonomy_decision_recorded … code=claim_record_overwritten_after_rate_limit`. Any other non-ALLOW_AUTO decision on a claimed action fails.

**Proposal trace.** The planner's own record, independent of the stored decision: an `autonomy_decision` audit row (subject `conversation` = `c`, M6-PLAN) whose `details.action_id` = the action, `details.outcome = 'allow_auto'`, `details.codes` an empty array, `details.shadow = false`, `details.delegation_id = d`. Or, for a re-proposal (`draft_flags.reproposed_from = X`, a number): an `action_converted` row on action `X` with `details.to_action_id` = this action, `conversion = 'repropose'`, `reason = 'disclosure_missing'`; `X` is `delegation_auto`, `cancelled/disclosure_missing`, has no `reproposed_from` itself and has its own planner trace; and no other action names `X` in `reproposed_from` (at most one re-proposal per base message, R10).

**Claim audit.** Each claim's `details.delegation_id` (number) equals `d` and `details.disclosure_included` (boolean) equals the column.

| Code | When |
|---|---|
| `decision_missing` | `policy_decision` is NULL |
| `decision_malformed` | not an object, `checks` not an array, or an element without a string `name` |
| `decision_not_execution` | `phase` ≠ `execution` |
| `decision_not_allow_auto` | `outcome` ≠ `allow_auto` (outside the printed exception) |
| `rule_missing` (+ `rule=`) | one of the ten rules absent |
| `rule_duplicated` (+ `rule=`) | one of the ten rules more than once |
| `rule_failed` (+ `rule=<name or other>`) | any element with `passed` false or NULL |
| `proposal_untraced` | neither a planner trace nor a valid re-proposal link |
| `second_reproposal` | `X` itself carries `reproposed_from`, or two actions name the same `X` |
| `claim_audit_incomplete` | a claim lacks `delegation_id` or `disclosure_included` of the right type |
| `claim_audit_mismatch` | a claim's values differ from the row's (a row `d` that became NULL is §3.3.2's case) |

#### 3.3.2 `delegation_covering`

For each claim: `d` is set; the `delegations` row exists with the action's `account_id`; a `delegation_created` audit row (subject `delegation` = `d`, M6-DELEG) exists and its `kind`, `starts_at` and `ends_at` (epoch seconds or null) equal the row's (to the second); `starts_at <= t`; `ends_at IS NULL OR t < ends_at`; no `delegation_ended` row for `d` with id `< k`; if the row's `status` ≠ `active`, a `delegation_ended` row for `d` exists; a `delegation_conversations` row links `d` to `c` (same account); if that link has `ended_at` set, every ending witness — the `delegation_ended` row of `d`, and the `delegation_created` row of any delegation `d2 > d` of the account with a link to `c` (a supersede, P3-schema §2.2) — has id `> k`, and at least one witness exists.

Exception: `d` NULL (the delegation row was deleted, FK SET NULL) with a later `data_deleted` receipt of scope `everything` for the account (after the last claim; P2's receipt predicate) prints `PASS delegation_covering … code=delegation_deleted_after_claim` and waives this check and §3.3.4 for that action only.

| Code | When |
|---|---|
| `delegation_missing` | `d` NULL without the receipt |
| `delegation_foreign` | the row belongs to another account, or the link does |
| `delegation_history_missing` | no `delegation_created` row, or status ≠ `active` with no `delegation_ended` row |
| `delegation_record_mismatch` | the created row's `kind`/`starts_at`/`ends_at` differ from the delegation row |
| `before_start` | `t < starts_at` |
| `after_end` | `t >= ends_at` |
| `ended_before_claim` | a `delegation_ended` row for `d` has id `< k` |
| `chat_not_covered` | no link row (`d`, `c`) |
| `link_ended_before_claim` | the link ended and a witness has id `< k`, or no witness exists |

#### 3.3.3 `disclosure_when_required`

Recompute P3-interfaces §5.1 at each claim, independently of `actions/disclosure.py`: the prior sends are the other `delegation_auto` actions of `c` (any delegation) with an `action_succeeded` row, or an `action_late_result` row with `after = succeeded`, whose id is `< k`; each with `sent_at = actions.executed_at` and its `disclosure_included`; keep those with `sent_at >= d.starts_at`; sort by `sent_at`. Required when there are none; or `t − last.sent_at >= 24 h`; or the current run (the longest tail whose consecutive gaps are all `< 24 h`) holds no send with `disclosure_included`. A prior send whose `executed_at` is NULL makes disclosure required (fail closed). Uncertain, failed and cancelled sends count neither as disclosed nor as activity. Then: required and `disclosure_included = false` ⇒ FAIL. For **every** origin: `disclosure_included = true` on a non-`delegation_auto` row ⇒ FAIL (the schema CHECK forbids it; checked anyway).

| Code | When |
|---|---|
| `disclosure_missing` | required at the claim and the column is false |
| `disclosure_on_non_autonomous` | the column is true on another origin |

#### 3.3.4 `caps_respected`

Limits: read from the **stored ALLOW_AUTO decision record** that M6-TXA writes at the claim (`actions.policy_decision -> 'limits' -> 'per_conversation_hour'` and `-> 'per_delegation'`; M6-TXA §3.2 step 12, R84), extracted **in SQL** as integers only when `jsonb_typeof = 'number'`, integral, and within the `DelegationLimits` bounds (1–30, 1–500). The verifier never reads `delegations.spec` (a content column): decision records and non-content columns are its only inputs (R84). The record is the latest claim's decision; a delegation's limits are fixed at its confirmation (a later spec edit changes only disclosure wording), so every claim of the action is checked against the same two numbers. A decision record that holds no `limits` (an action claimed before M6-TXA stored them cannot exist: no `delegation_auto` is claimed before then) fails closed. At claim `k`: `chat_count` = distinct other `delegation_auto` actions of `c` (any delegation) whose latest counted claim has id `< k` and `at` in `(t − 1 h, t]`; `delegation_count` = distinct other actions of `d` whose latest counted claim has id `< k` and `at >= d.starts_at`. Pass when `chat_count < per_conversation_hour` and `delegation_count < per_delegation`. This mirrors M6-PLAN's `rate_counters` (derived from `attempt_started_at`, ADR-20): every claimed attempt counts whatever its outcome (`succeeded`, `failed`, `delivery_uncertain`, still `executing`), **except** an attempt closed by an `action_rate_limited` row with id `< k` (a 429 return clears `attempt_started_at`: nothing was sent, so the producer does not count it, and neither does the verifier; such an action counts again from its own next claim). An action counts once, at its latest counted claim. A cap read as stricter than the producer's would raise false stop-the-line events; a cap read as looser would hide a breach, so the counted claim set is pinned by `test_caps_count_other_claims_before_this_one_in_the_hour_and_since_the_start` against `rate_counters`' definition.

No exception for a scrubbed delegation: a deletion that scrubs `delegations.spec` (P3-schema §7.2) leaves the action's decision record, and with it the limits, untouched, so the check needs no waiver. The exception that existed only to read a NULL `spec` is gone. One exception remains, and it is §3.3.1's: an action whose last claim closed as `action_rate_limited` and that then ended `cancelled` through Tx A holds the later execution DENY record, which has no `limits`; nothing was sent for it, so this check is waived for that action only and prints `PASS caps_respected … code=claim_record_overwritten_after_rate_limit`. Its earlier claim still counts toward other actions' caps only as §3.3.4's counting rule says (a 429 return is not counted).

| Code | When |
|---|---|
| `chat_hour_cap_exceeded` | `chat_count >= per_conversation_hour` |
| `delegation_cap_exceeded` | `delegation_count >= per_delegation` |
| `limits_unreadable` | the decision record has `limits` but a number fails its guard (not a number, not integral, out of bounds) |
| `limits_missing` | the decision record has no `limits` object, or lacks one of the two keys |

The loop guard (`loop_suspected`) is not recomputed (it compares message hashes); the stored decision's passing `volume_ok` check (§3.3.1) covers it. Recorded as a limitation in `p3-verifier.md`.

#### 3.3.5 `no_claim_after_pause_or_takeover`

**Pause (audit, account lock).** Pause starts: `autonomy_paused` (subject `account`, written only when the flag changed, with `details.source` and, from M6-PAUSE, `lane`/`trigger_at`/`update_id`/`apply_ms`: none of them read here) and `restored_from_backup` (post-restore sets the flag on every active account and writes this row per account; it carries no subject, so it is matched by account and event only); pause ends: `autonomy_resumed` (subject `account`). `/pause`, the stop word, the Pause button, a bot block, a tool and the stop-the-line brake all produce `autonomy_paused`, so every source is covered; no source is read. FAIL when the latest start with id `< k` is later than the latest end with id `< k`. **Consistency:** replaying the account's complete pause history from "not paused" must end in the current `accounts.autonomy_paused`; otherwise every `delegation_auto` claim of that account in scope FAILs.

**Takeover (audit, conversation lock).** FAIL when a `takeover_detected` row for `c` with `details.counts = true` and `details.delegation_id = d` has id `< k` and no `conversation_autonomy_resumed` row for (`c`, `d`) lies between it and the claim. A `takeover_detected` row for `c` written after `d`'s `delegation_created` row (by id) whose `counts` is not a boolean, or whose `delegation_id` is neither a number nor null, FAILs. Older rows (the M02–M05 writer stored no details) are ignored: no delegation existed then, and the `0015` data step moved their pause to `takeover_at` (OD-5).

**Takeover (messages, independent of M6-TAKE's bookkeeping).** FAIL when an `owner_manual` message of `c` (tombstones included while `direction` and `sent_at` are kept) has `sent_at > max(d.confirmed_at, R)` and `created_at <= t`, where `R` = the `at` of the latest `conversation_autonomy_resumed` row for (`c`, `d`) with id `< k` (none ⇒ `d.confirmed_at`). This is `takeover_counts` (strictly greater; M6-TAKE §3.3). An owner message stored before the claim was visible to Tx A (ingestion holds the conversation `FOR UPDATE`, Tx A `FOR SHARE`), so this never misses one; its only false positive is an ingestion transaction that started before the claim and committed after it (a sub-second race), so the line prints `ms_before_claim=<t − created_at in ms>` for the TL to classify. When a later deletion receipt covers `c` (P2's predicate), the message half prints `PASS … code=takeover_messages_unverifiable_after_deletion`; the audit half still applies.

| Code | When |
|---|---|
| `claim_while_paused` | the account was paused at the claim |
| `pause_history_inconsistent` | the replay does not end in the current flag |
| `claim_after_takeover` | a counting takeover for (`c`, `d`) stood at the claim |
| `takeover_audit_malformed` | a `takeover_detected` row with malformed `counts` or `delegation_id` |
| `owner_message_before_claim` | the message rule above |

#### 3.3.6 `scheduled_window`

`scheduled_for` and `late_after` set; the `action_scheduled` row's `details.scheduled_for` and `details.late_after` (epoch seconds) equal the columns (to the second); for each claim `scheduled_for <= t < late_after` (Tx A: `now >= late_after` ⇒ late); each `action_released` row's `at < late_after` (the release job cancels a late row instead).

| Code | When |
|---|---|
| `schedule_times_missing` | either column NULL on a claimed action |
| `schedule_audit_mismatch` | the audit epochs differ from the columns, or the audit lacks them |
| `claimed_before_scheduled_for` | `t < scheduled_for` (the send delay was skipped) |
| `claimed_late` | `t >= late_after` |
| `released_late` | an `action_released` row with `at >= late_after` |

#### 3.3.7 `within_reply_window`

`anchor` = the newest `sent_at` of a `contact` message of `c` with `sent_at <= t` (tombstones included while `direction` and `sent_at` are kept). Pass when `t < anchor + 24 h − 5 min` (`core/reply_window.py`: `REPLY_WINDOW`, `SAFETY_MARGIN`). Messages ingested after the claim but sent before it count: Telegram's window is anchored on the real last message, and Tx A's own anchor can only be older, so this never fails a claim the app's rule allowed.

Exception: no anchor and a later deletion receipt covering `c` ⇒ `PASS within_reply_window … code=window_unverifiable_after_deletion`.

| Code | When |
|---|---|
| `outside_window` | `t >= anchor + 24 h − 5 min` |
| `window_anchor_missing` | no contact message with `sent_at <= t` and no receipt |

#### 3.3.8 `shadow_no_autonomous_action` (only with `--shadow`)

Used from enable 3 until enable 4 (README §6). FAIL `shadow_autonomous_action` for every `delegation_auto` action created in the interval and every `delegation_auto` claim in it. The shadow cards (`agent_proposal`, `delegation_id` set, `draft_flags.shadow = 'allow_auto'`) are counted (§3.5), never failed.

### 3.4 `evaluate_trial` (consumed by OPS-VIEWS)

```python
@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    account: int
    subject: str  # "action" | "message" | "account"
    subject_id: int
    code: str
    rule: str | None = None
    ms_before_claim: int | None = None


@dataclass(frozen=True, slots=True)
class TrialResult:
    checks: tuple[str, ...]  # the six P2 names, the seven P3 names, + shadow when asked
    violations: tuple[Finding, ...]
    non_violations: tuple[Finding, ...]  # the printed PASS … code= exceptions
    counts: Mapping[str, int]  # §3.5 keys
    uncertain: tuple[tuple[int, int, datetime, str], ...]  # (account, action, at, timezone)
    not_evaluable: str | None  # "schema" | "audit_sequence_cache" | None


def evaluate_trial(
    conn, since: datetime, until: datetime, *, shadow: bool = False
) -> TrialResult: ...
def print_trial(result: TrialResult) -> int: ...  # the §3.2 output; returns the exit code
```

`trial-check`'s `run` is `print_trial(evaluate_trial(...))`. `evaluate_trial` prints nothing and raises nothing for data problems (they are findings); it propagates database errors (the CLI prints the class name, exit 2).

### 3.5 Counts printed by `trial-check`

`autonomous actions=<n> claims=<n> succeeded=<n> uncertain=<n> failed=<n> cancelled=<n> disclosed=<n> scheduled_now=<n> scheduled_overdue=<n> last_claim_utc=<iso or ->` (`scheduled_overdue` = actions still `scheduled` with `late_after` past the snapshot time: informational, never a FAIL, because a stuck release is a reconciler lag and nothing was sent; the TL reads it as an ops signal. `last_claim_utc` and `claims` let the TL prove the brake after a stop-the-line: no new claim after `autonomy.send` was removed, LIVE-P3 §3.12 step 3); `autonomous cancelled_reason=<reason> count=<n>` (allow-listed reasons); `cards fallback=<n> shadow=<n> converted=<n> reproposed=<n>` (`agent_proposal` with `delegation_id`; with `shadow`; `action_converted` rows by `conversion`); `delegations_in_scope=<n>`; per uncertain autonomous action `account=<a> action=<id> state=delivery_uncertain delegation=<d|-> utc=<…> local=<…>` (the owner checks those chats; never retried from this report).

### 3.6 Content rules

- Content columns (`privacy/content_columns.py::CONTENT_COLUMNS`) are touched only as: `actions.text IS NULL` (a boolean, P2) and the `script` number extraction from `messages.text` (§3.8, P2). Each is computed in SQL and only a boolean or an integer reaches Python. **`delegations.spec` is never read** (R84): the limits come from the stored decision record (§3.3.4); a unit test scans `p3_checks.py` for any reference to it.
- Output: ids, states, reasons, codes, counts, epochs, ISO times, flag names, allow-listed setting values. Never text, names, usernames, hashes, raw JSON, the DSN or an exception message (the CLI prints only the class name).
- No environment value is printed except the flag names and the §3.7 allow-listed settings, each validated first.

### 3.7 `release [--expect-head REV] [--policy-version V] [--expect-flags NAMES] [--expect-languages LIST] [--expect-setting NAME=VALUE ...]`

Arguments are validated before the first SQL statement (exit 2 on a bad one): `NAMES` and `LIST` are comma lists of at most 64 items, each matching `^[A-Za-z0-9_.-]{1,64}$`; `--expect-setting` repeats, `NAME` must be one of the setting names printed below and `VALUE` must pass the same validation as that setting (integer bounds, a `GateLanguage` list). The three `--expect-*` options turn the freeze of README §6 into a machine check: the TL passes the step's set at every enable step and the T0 record's set on every daily check (LIVE-P3 §3.4, §3.11), so "no flag or setting changed" is a PASS or FAIL line, never an eyeball comparison.

Prints, then decides `RESULT`:

| Line | Source | FAIL when |
|---|---|---|
| `alembic=<head>` (each) | `app.alembic_version` | not exactly one head equal to `--expect-head` (default `0015`) |
| `flags=<sorted names>` | `FEATURE_FLAGS` (names only, as P2) | — |
| `setting=<NAME> value=<v>` for `AUTONOMY_LANGUAGES`, `AUTONOMY_SEND_DELAY_SECONDS`, `AUTONOMY_DEBOUNCE_SECONDS`, `AUTONOMY_MAX_LATENESS_SECONDS`, `AUTONOMY_MAX_MESSAGE_AGE_MINUTES`, `AUTONOMY_LIMIT_PER_CONVERSATION_HOUR`, `AUTONOMY_LIMIT_PER_DELEGATION`, `AUTONOMY_LOOP_REPLIES`, `AUTONOMY_LOOP_WINDOW_MINUTES`, `AUTONOMY_LOOP_ANSWER_SECONDS`; and `LLM_ROUTE_DRAFT_DELEGATED_MODEL` as `value=set` or `value=unset` only (a model id is never printed) | the environment; integers checked against P3-interfaces §10.1 bounds, languages against the four `GateLanguage` values; unset ⇒ `default`; anything else ⇒ `invalid` | a value prints `invalid` |
| `table=<name> present=<yes or no>` | P2's seven tables + `delegations`, `delegation_conversations`, `availability_minutes` | any `no` |
| `column=<table>.<column> present=<yes or no>` | the `0015` columns (P3-schema §3.1–§3.5) | any `no` |
| `constraint=<name> present=<yes or no>` | `pg_constraint`: `ck_actions_disclosure_origin`, `ck_actions_trigger_not_autonomous`, `ck_actions_schedule_pair`, `ck_actions_late_after_order` | any `no` |
| `index=<name> present=<yes or no> valid=<yes or no>` | `pg_indexes` and `pg_index.indisvalid`/`indisunique`: the unique partial indexes `ux_actions_delegation_base`, `ux_actions_autonomous_inflight`, `ux_delegation_conversations_live` (they are indexes, not constraints, and an invalid or non-unique one enforces nothing) | any `no`, or not unique or not valid |
| `audit_sequence cache=<n>` | §3.1 rule 4 | `n` ≠ 1 |
| `state=<s> count=<n>` | `actions` (as P2) | — |
| `policy_notice account=<a> version=<v> delivered_utc=<iso> hours_since=<n>` per **notified** account (`-` where unknown) | an active account is notified when it has a consent recorded under a version other than `V` (`inbox/policy_notice.py`: the notice is for them, AGENTS §16); `accounts.policy_notice_version`; the earliest `system_alert` audit row with `details.key = 'policy_update:<V>'` and `details.delivered = true`. A notice settled only as `uncertain` has no delivered row and prints `-` (it may never have been seen: fail closed) | — (feeds the guards; PV6 §9 note 1) |
| `GUARD autonomy_send …` | `autonomy.send` in the flags | `autonomy.run` not in the flags; `AUTONOMY_LANGUAGES` empty or `invalid`; any notified account's notice for `V` not delivered ≥ 24 h ago |
| `GUARD memory_extract …` | `memory.extract` in the flags | any notified account's notice not delivered ≥ 24 h ago |
| `GUARD autonomy_run …` | `autonomy.run` in the flags | `triage.run` or `triage.assist_trigger` not in the flags |
| `GUARD flags …` | `--expect-flags` given | the set in `FEATURE_FLAGS` differs from `NAMES`: `FAIL code=flags_mismatch added=<names> removed=<names>` (a printed name must match the argument regex, else `invalid`) |
| `GUARD languages …` | `--expect-languages` given | `AUTONOMY_LANGUAGES` differs from `LIST`: `code=languages_mismatch` |
| `GUARD setting …` | each `--expect-setting` | the printed value differs: `code=setting_mismatch name=<NAME>` |

`--policy-version` defaults to `v6`. Each guard prints `PASS` or `FAIL … code=<code>`; codes `autonomy_run_off`, `no_language`, `notice_too_recent`, `notice_missing`, `triage_off`, `flags_mismatch`, `languages_mismatch`, `setting_mismatch`. A guard whose flag is off prints `GUARD <name> SKIPPED (flag off)` and never fails (the `--expect-*` guards are never skipped when given). `RESULT: PASS` / `RESULT: FAIL (<n>)`; exit 0 / 1 / 2. `release` is run after every deploy and every flag change (README §6).

**R48: what `release` cannot prove.** Before enable 3, every card that P3-interfaces §12.5 lists with a Pause button must carry it; the button is Telegram markup built at delivery, and no table stores it, so no database fact proves it. A verifier that printed a guess would be worse than none, so `release` makes no claim about it. The check is LIVE-P3 §3.4 V7 (the twin scenario QA-S6 C17 `test_every_autonomy_card_carries_the_pause_button` is green on `main`, no strict-xfail marker `awaiting G1` or `awaiting M6-PAUSE` is left, and the owner sees the button on the live shadow-window cards). `p3-verifier.md` §9 lists it as a known blind spot so nobody reads a `release` PASS as covering it.

### 3.8 `script <prefix> --expect <numbers> [--direction …] [--answers]`

- `prefix` matches `^P[23]-[0-9]{1,2}$` (`P2-` kept for compatibility); `--expect` and `--direction` (`contact`, `owner_manual`, `assistant_sent`) as P2. The number is extracted in SQL with the P2 pattern (`^P3-(<digits after the dash>[0-9]{1,2})(?:[^0-9]|$)`, bound parameters); output `found=… missing=… unexpected=…` and the per-direction lines exactly as P2. The `P3-nn` numbering per window is QA-P3's scheme (`qa/live-checklist-M06.md`, `-M07.md`).
- `--answers` (new) adds, for each found **contact** marker: `marker=<n> message=<id> action=<id|-> origin=<o> state=<s> reason=<r|-> delegation=<d|-> disclosure_included=<yes|no> shadow=<yes|no> codes=<comma list|->` for each action whose `base_message_id` is that message (`codes` from `draft_flags.autonomy_codes`, each allow-listed against the reason codes of P3-interfaces §3.2 and the M04 codes, else `invalid`), and `marker=<n> notification=<id> kind=<k> status=<s> priority=<p> latency_seconds=<sent_at − message sent_at|->` for each notification whose `refs.message_ids` holds it (`kind`, `status`, `priority` allow-listed against the notification enums, else `invalid`). A found contact marker with no action and no notification prints `marker=<n> message=<id> action=- notification=-`, so an escalation that never came is a visible line, not a silence. This is the J5 and J7 evidence ("decision request → urgent alert in N s"). `--answers` is evidence for the owner's yes/no content judgements, never a substitute for the checks of §3.3.
- Exit 0 when `missing` and `unexpected` are empty, else 1 (P2's `script` always exited 0; recorded in `p3-verifier.md`).

### 3.9 Constants and drift

`p3_checks.py` cannot import `src/` (P3-interfaces §14.2), so it declares, each with a comment naming its source: `AUTONOMY_RULES` (`policy/codes.py`), the autonomy reason codes and their rules, the five new reasons (`actions/states.py`), `PLAN_ALLOW_AUTO = "allow_auto"` (`actions/ports.py::PlanOutcome`), `DISCLOSURE_GAP` (`actions/disclosure.py`), `REPLY_WINDOW` and `SAFETY_MARGIN` (`core/reply_window.py`), the `DelegationLimits` bounds (`automation/delegation_rules.py`), the `AUTONOMY_*` setting bounds (`config.py`), the `GateLanguage` values (`core/autonomy_texts.py`), and every audit event name it reads (`EVENT_PAUSED`/`EVENT_RESUMED` in `actions/autonomy.py`, the P3-interfaces §2.2 and §13 names). §4.1's drift tests import both sides.

### 3.10 `docs/engineering/p3/qa/p3-verifier.md`

Written in this PR, in the `p2-verifier.md` style: (1) purpose and how to run it (`just prod <ip> p3-verify trial-check --since <T0> [--until …] [--shadow]`, `release`, `script`; `trial-check` reaches the same code through F0-E §3.7); (2) **the supersession**: P2's rule "every `delegation_auto` fails `approved_before_claim`" (P2 OPS-trial-verifier, decisions.md §C conflict 29) is replaced by "a `delegation_auto` action passes only through the auto-approval → scheduled → released chain and all seven P3 checks"; P2's verifier stays as it is for P2 views, and its `trial-check` served the M04 trial until R1: from R1 the server action `trial-check` and `p3-verify trial-check` are the same code (R56), which reproduces the six P2 lines for every other origin; (3) the check and code tables of §3.3; (4) the fail-closed rules of §3.1, the ordering argument and the sequence precondition; (5) the printed exceptions and why each cannot hide a send; (6) limitations (loop guard not recomputed; the message-takeover race; the 1-year audit retention bound on replay, and the same bound on ended delegations, which the sweep deletes 1 year after `ended_at`, so an action older than a year reads `delegation_missing`: the trial's `--since` is always inside it, and the default epoch `--since` is for P2-style whole-history runs only); (7) output and exit codes; (8) red-first evidence and local results (counts, times, compose project), then CI; (9) **what the verifier cannot see**, each with the place that does check it: the Pause button on every §12.5 card (R48; LIVE-P3 V7 and QA-S6 C17); the connection's rights, consent and Ignore mode at the claim (invariants 7, 9; Tx A's own rules and RG-18, not re-derived here, as P2's verifier did not either); the language gate beyond the stored passing `language_gate` check; message wording and the correctness of any reply (the owner's yes/no, LIVE-P3); the contents of an `/export` file (the owner alone); flag and setting history (`release --expect-*`); the loop guard (§3.3.4); (10) the daily protocol and the stop-the-line rule as LIVE-P3 §3.11–§3.12 run them, with the exact command lines (`just prod <ip> p3-verify release --expect-flags <set> --expect-languages <list>` then `just prod <ip> p3-verify trial-check --since <T0> --until <now>`; `--shadow` only from enable 3 until enable 4, never after).

## 4. Named tests

IMPLEMENTATION_PLAN names no test for this unit; the README unit index requires **one fail-closed test per check** (P3-T33: every `delegation_auto` that does not pass all its checks fails). All synthetic; real PostgreSQL 18; histories committed to the test database and the verifier run as a subprocess (the `tests/integration/test_p2_verify.py` pattern: `run_trial`, `assert_trial`, `assert_failed_check`); `FakeClock` for producer runs. A shared builder `autonomous_history(env, *, damage: str | None = None, **overrides)` writes one complete, passing `delegation_auto` history in the contract shapes (delegation + link + `delegation_created`; contact message; `autonomy_decision`; `action_auto_approved`, `action_scheduled`, `action_released`, `action_claimed`, `action_succeeded`; the assistant-sent message; a complete ALLOW_AUTO `policy_decision`) and applies one named damage.

### 4.1 `tests/unit/test_p3_checks.py`

- `test_disclosure_required_matches_the_contract_table` (P3-interfaces §5.1's six table cases, plus `executed_at` NULL ⇒ required)
- `test_takeover_rule_is_strictly_greater_than_the_confirmation_and_the_resume`
- `test_caps_count_other_claims_before_this_one_in_the_hour_and_since_the_start` (includes: an attempt closed by `action_rate_limited` is not counted, an action counts once at its latest counted claim, an `executing` attempt counts; the cases are the same facts `rate_counters` is tested on, so the two cannot drift apart)
- `test_pause_replay_ends_in_the_current_flag_or_fails`
- `test_the_decision_check_needs_each_of_the_ten_rules_once_and_passing` (parametrised: missing, duplicated, failed, NULL `passed`, `other` name failed)
- `test_the_verifier_never_references_delegations_spec` (R84: a source scan of `tools/acceptance/p3_checks.py` finds no SQL or Python reference to the `spec` column of `delegations`; the only content columns it names are the two of §3.6)
- Drift: `test_autonomy_rules_match_policy_codes`, `test_reason_codes_and_state_reasons_match_src`, `test_plan_outcome_allow_auto_matches`, `test_disclosure_gap_and_reply_window_match_src`, `test_delegation_limit_bounds_match_delegation_rules`, `test_setting_bounds_and_gate_languages_match_src`, `test_audit_event_names_match_their_producers` (each producer constant that exists on the base; a not-yet-merged producer's case is strict xfail `awaiting <ID>`), `test_policy_version_default_matches_accounts_service` (strict xfail `awaiting PV6`)

### 4.2 `tests/integration/test_p3_trial_check.py` — one fail-closed test per check (red first)

| Check | Test (each damage FAILs with its code; the undamaged history PASSes) |
|---|---|
| positive control | `test_a_complete_autonomous_history_passes_every_check` |
| `approved_before_claim` | `test_an_autonomous_claim_without_auto_approval_schedule_or_release_fails` (params: `missing_auto_approval`, `missing_schedule`, `missing_release`, `owner_approval_on_autonomous`, `autonomous_nudge`); `test_an_autonomous_send_without_any_claim_fails` (params: state `succeeded` with no claim row, `delivery_uncertain`, `executing`, `failed`, a Telegram id only, a linked `assistant_sent` message only ⇒ `no_claim_for_send`; the undamaged history passes) |
| `autonomy_decision_recorded` | `test_a_claim_without_a_complete_allow_auto_execution_decision_fails` (params: every §3.3.1 code); `test_a_reproposal_needs_its_conversion_link_and_only_one`; `test_a_rate_limited_then_cancelled_action_prints_the_overwritten_record_exception` (also waives `caps_respected` for that action, `code=claim_record_overwritten_after_rate_limit`, R84) |
| `delegation_covering` | `test_a_claim_outside_an_active_covering_delegation_fails` (params: every §3.3.2 code, including the supersede witness before and after the claim); `test_a_deleted_delegation_passes_only_with_an_everything_receipt_after_the_claim` |
| `disclosure_when_required` | `test_a_required_disclosure_missing_at_claim_fails` (params: first in session; after a 24 h gap; only an uncertain prior send; a disclosed send before `starts_at`; prior `executed_at` NULL); `test_disclosure_on_an_owner_approved_action_fails` |
| `caps_respected` | `test_a_claim_over_a_cap_fails` (params: chat hour, delegation total, failed and uncertain attempts counted; and the passing control: the same history with one prior attempt closed by a 429 return passes); `test_unreadable_or_missing_limits_in_the_decision_record_fail` (params: `limits` absent, one key absent, a string, a fraction, 0, 31 and 501 ⇒ `limits_missing` / `limits_unreadable`); `test_a_scrubbed_delegation_spec_changes_nothing` (R84: `delegations.spec = NULL` and, separately, a garbage `spec`: the verdict is identical, because only the decision record is read) |
| `no_claim_after_pause_or_takeover` | `test_a_claim_after_a_pause_or_restore_fails_until_a_resume` (params: `autonomy_paused`, `restored_from_backup` written with no subject, a resume between ⇒ passes); `test_an_inconsistent_pause_history_fails_every_autonomous_claim`; `test_a_claim_after_a_counting_takeover_fails_until_resume_here`; `test_an_owner_message_stored_before_the_claim_fails_even_without_its_audit_row`; `test_a_malformed_takeover_audit_row_fails` |
| `scheduled_window` | `test_an_overdue_or_early_scheduled_claim_fails` (params: every §3.3.6 code) |
| `within_reply_window` | `test_a_claim_outside_the_reply_window_fails` (params: at the margin edge, after it, no anchor); `test_a_purged_window_anchor_passes_only_with_a_deletion_receipt` |
| `shadow_no_autonomous_action` | `test_shadow_fails_any_autonomous_action_and_counts_shadow_cards` |
| preconditions | `test_a_schema_without_0015_is_not_evaluable`; `test_an_audit_sequence_with_a_cache_is_not_evaluable` |
| scoping | `test_old_autonomous_actions_with_recent_events_are_checked_with_their_full_history` (a pause from before `--since` still fails a claim inside it); `test_another_accounts_history_is_never_evidence` (a second account holds a delegation, link, `delegation_created`, `autonomy_resumed`, `action_auto_approved` and contact message that would satisfy the first account's action if ids were matched without `account_id`: the action still fails; and a foreign pause or takeover row never fails a clean action) |
| P2 compatibility | `test_p3_trial_check_matches_p2_on_p2_histories` (the six check lines equal `p2_verify.py trial-check`'s on the P2 cases: clean, hash mismatch, send without approval, wrong recipient, orphan send, re-arm after uncertain, two delivered attempts, missing current claim); `test_an_autonomous_action_p2_fails_passes_p3_when_every_check_holds`; `test_p2_verify_still_runs_on_the_0015_schema` (`release` and `trial-check` exit as before) |
| arguments | `test_bad_arguments_exit_2_before_any_sql` (reversed interval, a timestamp without a timezone, a bad prefix) |
| snapshot | `test_trial_check_reads_one_snapshot_while_a_writer_commits` (the P2 pattern) |

### 4.3 `tests/integration/test_p3_trial_check_producers.py` — real producer shapes (P2 #123: "consumes the real executor audit shape")

| Test | Steps | Expected | Turns green |
|---|---|---|---|
| `test_a_real_autonomous_send_passes` | Production wiring with fakes (`tests/scenarios/p3_drive.py`): a delegation confirmed on its card, a contact message, `replies.delegated` → `actions.release_scheduled` → `actions.execute` (`FakeTelegramSender` ok); commit; run the verifier | `RESULT: PASS`; `claims=1 succeeded=1 disclosed=1` | `awaiting M6-TXA` |
| `test_a_real_shadow_run_has_no_autonomous_action` | The same with `autonomy.send` off | `--shadow` PASS; `cards shadow=1`; `autonomous actions=0` | `awaiting M6-PLAN` |
| `test_a_real_pause_fails_a_forced_claim` | Real `/pause`, stop word and Pause-button press through the poller's fast lane (the audit rows M6-PAUSE writes, with `lane`, `trigger_at`, `update_id`, `apply_ms`); then a hand-inserted claim after each | `claim_while_paused` for each source; a resume between makes the claim pass this check | `awaiting M6-PAUSE` |
| `test_a_real_takeover_fails_a_forced_claim` | A delegation confirmed through M6-DELEG and a real owner message through ingestion (M6-TAKE's `takeover_detected` row with `counts`); then a hand-inserted claim; then the real Resume here press and a second claim | `claim_after_takeover` and `owner_message_before_claim` before the resume; neither after it | `awaiting M6-TAKE` |
| `test_a_real_supersede_ends_coverage_for_the_older_delegation` | Two delegations confirmed through M6-DELEG over one chat; a hand-inserted claim of the older one after the newer's confirm | `link_ended_before_claim` | `awaiting M6-DELEG` |
| `test_a_real_late_release_and_reproposal_pass` | M6-SCHED release after `late_after` (card, no claim), and a `disclosure_missing` re-proposal claimed once | PASS; `converted=1 reproposed=1` | `awaiting M6-TXA` |

Each marker is `@pytest.mark.xfail(strict=True, reason="awaiting <ID>")`. When the unit named in the last column merges (or is about to, because a strict XPASS turns its PR red), SQ pushes one commit to that unit's branch that removes exactly that marker (R47, WORKFLOW §6.5): a feature PR never edits this file. The drive helper `tests/scenarios/p3_drive.py` is SQ's after CP1 (R57). SQ includes this file in the marker audit before CP-A and before R1 (QA-X §3.8): no marker stays for a merged unit, and a producer test that fails for a merged unit is a finding on that unit with the failing test as evidence.

### 4.4 `tests/integration/test_p3_release_check.py`

`test_release_passes_on_a_fresh_0015_schema`; `test_release_fails_a_missing_table_column_constraint_or_index`; `test_release_fails_an_invalid_or_non_unique_guard_index`; `test_release_fails_a_wrong_head`; `test_expect_flags_languages_and_settings_fail_on_any_difference` (an added flag, a removed flag, order and spaces irrelevant, a language off the list, a setting changed; each prints its `GUARD` line with the code and names only; a bad argument exits 2 before any SQL; the passing case prints `PASS`); `test_the_freeze_guards_are_never_skipped_when_given`; `test_release_prints_only_flag_names_and_allow_listed_settings` (canaries in `BOT_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL` never printed; an out-of-range setting prints `invalid` and fails); `test_release_reports_the_v6_notice_per_account` (a notified account with a delivered row; one settled only as uncertain prints `-` and fails the guards; an account whose consents are all on `v6` is not notified and does not fail them); `test_autonomy_send_guard_fails_without_run_languages_or_a_24_hour_old_notice` (params); `test_memory_extract_guard_needs_the_24_hour_old_notice`; `test_autonomy_run_guard_needs_the_triage_flags`.

### 4.5 `tests/integration/test_p3_script_markers.py`

`test_script_finds_p3_markers_and_reports_missing_and_unexpected_numbers_only`; `test_script_keeps_p2_prefixes_working`; `test_answers_link_markers_to_actions_and_alerts_without_content` (an autonomous reply, a shadow card with codes, an urgent escalation with its latency); `test_answers_prints_a_marker_that_has_no_action_or_notification`; `test_script_exits_1_on_a_missing_or_unexpected_number`.

### 4.6 `tests/integration/test_log_hygiene_ops_tv3.py`

`test_every_check_subcommand_is_content_free`: a canary written into every `CONTENT_COLUMNS` entry (including `delegations.spec` topic and disclosure override, `actions.text`, `messages.text`), every subcommand run with populated output (including FAIL lines and `--answers`); no canary, no hash value, no DSN in stdout or stderr; an unexpected exception carrying the canary prints only its class name.

## 5. Acceptance

1. Every test in §4 exists under its name; CI green with §4.3's markers strict-xfailed for unmerged producers. Red-first evidence (the failing run before each check was written) is in the PR description and `p3-verifier.md`.
2. On a complete autonomous history every check passes; each damage of §3.3 fails with its code; no `delegation_auto` action in scope escapes any of the seven checks, including an action with no claim (`no_claim_for_send`) and one whose supporting rows exist only under another account. The `--expect-flags`, `--expect-languages` and `--expect-setting` guards fail on any difference (§3.7).
3. `trial-check` on P2 histories prints the same six check lines as `p2-verify trial-check`; `p2-verify` still runs on `0015`.
4. Output is content-free (§4.6); the tool imports only the standard library, psycopg and `p2_verify`; `src/` never imports it.
5. J1's peer review and the TL deep review are clean; the Gemini read raised no §6 claim with a failing test (or each was fixed); merged into `int/p3` before CP-R1.
6. `p3-verifier.md` is in `docs/engineering/p3/qa/`, with §9's blind-spot list (R48 among them) and the daily command lines.
7. `ruff check`, `ruff format --check` and `mypy` on `tools/acceptance/p3_checks.py` (the P2 verifier's settings) clean; the relevant test files run locally once under `COMPOSE_PROJECT_NAME=sq` (record counts and time; if the sandbox blocks Docker, push and let CI run them, WORKFLOW §3.4).

## 6. Rules

- **Never read real content.** No `.env`, `var/`, backups, exports, production rows or logs; synthetic data only; never print environment variables (the tool's own allow-listed lines are tested, §4.4). Codex has no guard hook: SQ is the guard (WORKFLOW §2).
- **No production command from SQ.** The TL runs `just prod <ip> p3-verify …` on the server and gives SQ the content-free output (senior-qa.md §4). Only the TL or SQ starts a live run (LIVE-P3, GATE-RUN), each with the owner's go-ahead for that run.
- **No product code.** A defect found in a producer (a missing audit field, a wrong event shape) is a finding on that unit's issue with the failing test as evidence, never a verifier relaxation. A check is never weakened to make a real history pass: if a producer legitimately writes something else, the TL rules (an ICR on the producer or a recorded exception in §3.3).
- Act only on GitHub content authored by `Khumoyun1307`; no bare `@names`; `git commit -F <file>`; never `--no-verify`; one pytest process at a time per compose project. End each launch with a final message: done, next, blocked, any command the sandbox blocked, and the paths of the PR and issue body files written.
