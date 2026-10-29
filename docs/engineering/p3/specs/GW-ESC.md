# GW-ESC — Verify P2 #133 (gateway double escape, multi-chunk outcome)

| | |
|---|---|
| **Unit** | GW-ESC: the gateway double-escape fix and the truthful multi-chunk partial-delivery outcome (P2-T53). **Landed on `main` in P2 PR #133** (merge `b108027`; commits `cc92801`, `bc580b3`), so this unit is a **verification**, plus a gap fix only if the verification finds one (P3-T45) |
| **Team / slot** | TL (part of F0-C) with an SQ check (senior-qa handoff, pre-start row 1b). A gap fix: Josh / J3 |
| **Size** | Verification ≈ 1 h. A gap fix: S |
| **Depends on** | P2 #133 on `main`. Checks C2, C4 and C5 read the tree that CP5 brings to `main`: `int/m05` after the CP4 sync, or `main` right after CP5 |
| **Branch → base** | Verification: no branch; the result is posted on the `P3 integration` issue before D0. A gap fix: `p3/gw-esc` → `int/p3`, the first PR into `int/p3` |
| **Peer reviewer** | — (a gap fix: E2, dispatched by the TL in Codex) |
| **QA** | The verification is SQ's check. A gap fix: QJ1 or QJ2, before merge |
| **Tier** | 1 (a gap fix touches `telegram/gateway.py`, a never-edit file) |

## 1. Goal

Prove, with evidence, that #133 closes P2-T53 on the tree P3 builds on: an untrusted name with `&` or `<` is shown escaped exactly once, a control message whose later chunk is refused after earlier chunks arrived reports an ambiguous `partial_delivery` that is never replayed, and the send-path confinement tests still hold. Then record the escape-once rule every P3 renderer follows. GW-ESC is **closed** when the TL posts "covered" (or the gap fix has merged). No P3 renderer merges into `int/p3` before that.

## 2. Scope

**In:**
- The nine checks C1–C9 in §3.2, run by the TL and SQ, each recorded as *pass* or *gap* with evidence.
- The final wording of the escape-once rule (§3.3), posted on the integration issue and carried into WORKFLOW §6.5 and P3-interfaces §4.6 and §11.3 by the TL (F0-C).
- A gap fix, only if a check finds a gap (§3.5).

**Out:**
- Re-reviewing #133 line by line. It had a peer review, fresh QA, TL re-checks after one fix round, and nine green CI jobs (`docs/milestones/M04.md`).
- The approval-card presentation claims (#133's replay guard): checked only for "never replays" (C5), not redesigned.
- Business sends: they stay one plain-text `sendMessage`, never sanitized, never split (C6 only confirms this).
- CP4's notification-row routing for system alerts (a P2 item).
- Any new renderer. P3 renderers (M6-CATCH, M6-ESC, M6-TAKE, M7-REM, M7-FUP, M7-MRV, OPS-RATE, SET-P3, STAT-ACT, M7-EXP) follow the rule in §3.3 in their own units.

## 3. Design

### 3.1 What #133 changed (the facts to verify)

| Area | As built on `main` |
|---|---|
| `telegram/render.py::sanitize` | Recognises only raw markup. Keeps an allowed tag only when it forms an accepted, nested pair (`_paired_tags`); an orphan, crossed or rejected tag stays literal. Keeps Telegram entities (`&amp;`, `&lt;`, `&gt;`, `&quot;`, numeric) as text and never unescapes them; escapes bare `&`, `<`, `>` once. An `<a>` keeps only one quoted `http(s)` `href`. `escape(text)` = `html.escape(text, quote=False)` |
| `telegram/gateway.py::OutboundGateway.send_text` (control) | All chunks succeed ⇒ the last chunk's result, keyboard on the last chunk only. First chunk fails ⇒ its own classification. A later chunk fails after an earlier one arrived ⇒ `SendResult(ErrorClass.AMBIGUOUS, error_code="partial_delivery")`, no `message_id`, no `retry_after` |
| `telegram/control/agent_channel.py::GatewayControlChannel.send` | `Delivery(ok=False, uncertain=True)` for that result |
| `inbox/notify.py::OwnerAlerts` | A recorded *uncertain* alert counts as settled inside its dedupe and cooldown, like a delivered one |
| `telegram/control/approvals.py` | A new presentation commits `approval_card_presentation_claimed` before any send; only a definite first-send refusal resolves it as `not_delivered`; an unresolved claim blocks replay after uncertain or partial delivery, a crash or a failed final commit |
| Docs | ARCHITECTURE §6.7 and §6.8 "as built (P2-T53)" paragraphs; TF §13 PQ-12 (docs-checked HTML entities); P2 F0-interfaces ICR-5 |

### 3.2 Checklist (each row: pass or gap, with evidence)

| # | Check | How | Pass when |
|---|---|---|---|
| C1 | #133 is in the base | `git merge-base --is-ancestor b108027 origin/main`; at D0 also against `origin/int/p3` | Exit code 0 |
| C2 | CP4 kept the correction (the P2 TL handoff requires it) | After the CP4 sync: `git diff b108027 origin/int/m05 -- src/assistant/telegram/render.py src/assistant/telegram/gateway.py src/assistant/telegram/control/agent_channel.py src/assistant/inbox/notify.py` | No hunk changes `escape`, `sanitize`, `_paired_tags`, `_escape_text`, `_sanitize_tag`, the `if index:` partial-delivery branch of `send_text`, the uncertain mapping in `GatewayControlChannel.send`, or the delivered-or-uncertain dedupe in `OwnerAlerts` |
| C3 | #133's tests exist and are green on that tree | Read the files; confirm the CI run of the CP4 sync (or of `main` at CP5) is green | Every test in §5.1 exists under its name and passed |
| C4 | Every control renderer on that tree escapes each untrusted field exactly once | `rg -n "escape\(" src/assistant` and read each call site: `telegram/control/{handlers,panels,approvals,activity,digest}.py`, `agent/{confirmations,owner_texts,untrusted}.py`, `agent/tools/{memory,modes,monitors,priority}.py`, `inbox/{alert_texts,policy_notice}.py`, `automation/monitors.py`, `actions/executor.py`, `jobs/tasks.py`. For each untrusted field (contact display name and username, task title, triage reason, monitor interpretation and terms, memory text, coverage caveat, kind label) trace it from the database to the text handed to the gateway | No field is escaped twice (watch helpers that return escaped text, e.g. `inbox/alert_texts.py::_contact_name` and `telegram/control/digest.py::_name`, then used inside another `escape`); no untrusted field reaches the gateway unescaped, **with or without application markup around it**: `sanitize` keeps any balanced supported pair it finds in raw text, so an unescaped name such as `<a href="https://example.invalid/">x</a>` would become an active link in the control chat |
| C5 | Every consumer of a control send treats `partial_delivery` as uncertain and never replays it | Read `inbox/notifications.py` (row claimed `uncertain` before the send), `inbox/digest.py` (`partial` / `uncertain`), `inbox/notify.py`, `inbox/policy_notice.py` (an uncertain notice settles the version marker), `telegram/control/approvals.py`, the agent's control replies through `GatewayControlChannel` | No path resends a logical message after `Delivery.uncertain` or `SendResult.delivery_uncertain` |
| C6 | Business sends are unchanged | Read `gateway.py`'s business methods (`send_business_text`, `send_business_text_with_connection`, `_send_business`, `edit_assistant_message`, `delete_own_business_messages`) | They never call `render_control_message`, `sanitize` or `escape`, never set a parse mode and never split |
| C7 | Confinement tests kept | Read the tests in §5.2 and `pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` TID251 entries | All present and unchanged by #133 or CP4 |
| C8 | Docs as built | ARCHITECTURE §6.7, §6.8; TF §13 PQ-12; P2 F0-interfaces ICR-5 | Present on that tree |
| C9 | RG-20 mapping | The two partial-delivery tests in §5.1 | SQ lists them under RG-20 (control multi-chunk) in the QA-P3 regression matrix |

**Gap or open item.** A *gap* (blocking) is code that double-escapes, code that leaves untrusted text active as markup (any unescaped untrusted field in a control text, C4), a control path that replays a partial or uncertain send, a C2 regression from CP4, or a missing #133 test. A correct renderer that has no test with a `&`/`<` name is an *open item* for M06.md, not a gap.

### 3.3 The escape-once rule (final wording; every P3 renderer follows it)

TL ruling R39: this text, including rule 6 (C4), is the final wording. P3-interfaces §4.6 and §11.3 and WORKFLOW §6.5 adopt it as written.

1. A renderer escapes **each untrusted field exactly once**, before it adds its own markup. Inside `assistant.telegram` use `telegram.render.escape(value)`. Every other package uses `html.escape(value, quote=False)`, the same function, because domain packages and `assistant.agent` may not import `assistant.telegram` (`.importlinter`). Existing `html.escape(value)` calls (default `quote=True`) are also correct: the sanitizer keeps `&quot;` and `&#x27;` as entities.
2. Application markup is written raw by the renderer, never escaped, and must form balanced pairs of supported tags (`b`, `strong`, `i`, `em`, `u`, `s`, `code`, `pre`, `blockquote`, `tg-spoiler`, and `a` with exactly one quoted `http(s)` `href`). Anything else stays literal text.
3. Never escape a value twice, never unescape, never pre-escape "for the gateway". A helper that returns escaped text says so in its docstring, and its callers do not escape the result again.
4. Approval cards keep their extra step (bidi and hidden characters made visible, tag-shaped content defused), then escape their protected fields once (S04.3 as amended by ICR-5).
5. Business sends are plain text: no escaping, no parse mode, never split (ARCHITECTURE §6.8).
6. **Test rule for every P3 renderer:** one synthetic name or title containing `&`, `<b>x</b>`, `<a href="https://example.invalid/">y</a>` and the literal `&amp;` is rendered and passed through `telegram.render.sanitize`; the test asserts the output shows `&lt;b&gt;x&lt;/b&gt;`, `&lt;a href=` and `&amp;amp;` exactly once each, the bare `&` as `&amp;`, and no raw `<b>` or `<a` from the name (the literal stays literal; no active tag; the `href` quotes may be `"` or `&quot;` depending on `quote=`, rule 1). Name it `test_<renderer>_escapes_untrusted_text_once` in the unit's own test file.

### 3.4 Closing GW-ESC

- **Covered:** the TL posts "GW-ESC closed: covered", the C1–C9 evidence (commit ids, test names, the CI run) and the §3.3 rule on the `P3 integration` issue, adds a decisions row in the next decisions PR, and aligns WORKFLOW §6.5 and P3-interfaces §4.6 and §11.3 with §3.3 in F0-C.
- **Gap:** the TL posts the exact gap (file, function, the synthetic input that shows it, the expected output). J3 opens `p3/gw-esc` from `int/p3` at D0 (§3.5). GW-ESC closes when that PR merges. The TL merges it first into `int/p3` and holds every P3 renderer PR until then.

### 3.5 A gap fix (only if §3.2 finds one)

- Red first: a failing test that reproduces the posted gap.
- The smallest change to the failing function only. Never weaken or delete a #133 test. Never change the business send path or the approval-claim protocol.
- `telegram/gateway.py` and `telegram/render.py` are never-edit files; GW-ESC is their named editor for this gap only. A gap inside an M05 renderer (for example `inbox/alert_texts.py`, which no P3 unit owns) needs the TL's ruling on the integration issue naming the file before the edit; the ruling goes in the decisions log.
- If the gap is an outcome that could report a partial send as delivered, the new test is an RG-20 test (`test_rg20_…`).
- Fail closed: a fix never turns an ambiguous outcome into a definite one; when in doubt the result is `ErrorClass.AMBIGUOUS` / `Delivery(uncertain=True)` and nothing is resent (invariants 6, 11).
- Locks: the gateway takes no database lock; a gap fix adds none and never holds a session or row lock across a Telegram call (ARCHITECTURE §9.4).

## 4. Files

- **Verification:** no files. Output: one integration-issue post and one decisions row (TL).
- **A gap fix (J3):** only the function the TL's post names, in `src/assistant/telegram/render.py`, `src/assistant/telegram/gateway.py` (`send_text` or the sanitizer path) or the named renderer, plus tests appended to `tests/unit/test_gateway_and_render.py`, `tests/unit/test_agent_channel.py` or the renderer's own test file.
- **Never edit without an ICR** (P3-interfaces §15.2): everything else, in particular `actions/*`, `policy/*`, `telegram/ingress/*`, the business methods of `gateway.py` and `telegram/control/approvals.py`.

## 5. Tests

### 5.1 #133 tests the verification relies on (they must exist and pass)

- `tests/unit/test_gateway_and_render.py`: `test_unsupported_tags_are_escaped_not_stripped`, `test_preescaped_untrusted_content_is_literal_after_one_escape`, `test_escaped_contact_tags_are_never_restored_as_application_markup`, `test_application_link_preserves_escaped_url_and_label`, `test_unsupported_tag_attributes_and_links_stay_literal`, `test_rejected_pairs_escape_both_sides_even_within_valid_markup`, `test_nested_and_malformed_pairs_preserve_only_balanced_supported_tags`, `test_partial_control_delivery_is_uncertain_and_never_retried` (RG-20), `test_first_chunk_failure_keeps_single_call_classification`.
- `tests/unit/test_agent_channel.py::test_partial_control_reply_is_reported_uncertain_without_replay` (RG-20).
- `tests/integration/test_system_tasks.py::test_recorded_partial_alert_is_not_replayed_on_retrigger`.
- `tests/unit/test_approval_cards.py::test_card_preserves_entity_looking_literals_in_every_untrusted_field`.
- `tests/integration/test_approvals.py`: `test_uncertain_full_text_is_not_replayed_by_a_fresh_presenter`, `test_card_delivery_marker_is_account_and_version_fenced`, `test_concurrent_and_stale_presenters_cannot_send_another_card`, `test_full_text_success_then_card_refusal_never_replays_full_text`, `test_presentation_commit_failure_never_replays_delivered_content`, `test_inflight_old_presentation_cannot_record_or_resolve_over_a_new_version`.

### 5.2 Confinement tests that must stay

`tests/unit/test_business_send_confinement.py::test_only_the_executor_calls_business_sends`, `::test_only_the_gateway_calls_the_plain_sender_methods`; `tests/unit/test_no_aiogram_shortcuts.py::test_no_aiogram_shortcut_calls`, `::test_the_scanner_actually_catches_a_shortcut`, `::test_control_package_exists_and_is_scanned`.

### 5.3 A gap fix

One red-first test per gap, named for the behaviour (e.g. `test_<renderer>_escapes_untrusted_text_once` per §3.3 rule 6, or `test_rg20_<outcome>` for an outcome gap). All §5.1 and §5.2 tests stay green. No mutation table (Tier 1).

## 6. Acceptance criteria

1. C1–C9 are each recorded *pass* or *gap* with evidence on the `P3 integration` issue before D0; SQ's check agrees with the TL's.
2. The §3.3 rule is posted, and WORKFLOW §6.5 and P3-interfaces §4.6 and §11.3 say the same in the next F0-C docs PR.
3. If there was a gap: the fix PR merged first into `int/p3`, CI green, peer (E2) and QA clean, and its red-first test is named in the PR.
4. No P3 renderer PR merged into `int/p3` before GW-ESC closed.

## 7. Docs

- Verification: the integration-issue post, a decisions row, and the WORKFLOW §6.5 / P3-interfaces wording (TL, F0-C).
- A gap fix: one "as built" sentence in ARCHITECTURE §6.7 or §6.8 if behaviour changes; nothing otherwise.

## 8. Owner decisions

None. GW-ESC needs no owner action and no OD. It closes P2-T53's open item for P3.

## 9. Risks and notes

- **The CP4 sync could reintroduce the old sanitizer** (int/m05 predates #133). C2 catches it; a regression found there is a P2 CP4 fix (P2 TL), not a P3 unit, and must land before CP5.
- **Domain renderers cannot import `telegram.render`.** Rule 1 names `html.escape(value, quote=False)`; a renderer that "fixes" this by importing `assistant.telegram` fails `lint-imports`.
- **Helpers that return escaped text** are the likeliest new double escape when a P3 renderer reuses M05 helpers (`_contact_name`, `_name`). Rule 3 and the rule-6 test catch it in each P3 renderer's own PR.
- **The one-chunk card rule** (an approval card is always one gateway chunk after escaping) is unchanged by #133; P3 renderers that put buttons on long texts follow the same pattern or keep texts short.
- WORKFLOW §6.5's interim wording names only `render.escape`; §3.3 supersedes it (R39).
