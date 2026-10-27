# Phase P2 — R1 exit + Inbox intelligence (M04 ∥ M05)

*Engineering plan for the phase after M03. Owner-approved 2026-10-26 (see [decisions.md](decisions.md)). Written by the Technical Lead. This folder is the **single entry point** for every engineer in P2: start here, then read your handoff in [handoffs/](handoffs/).*

**Status (2026-10-27):** **P2 has started.** The owner said "M03 OK" on 2026-10-27 and gave every engineer the go-ahead the same day (D0 = 2026-10-27; [decisions.md](decisions.md) B4). CP0 is done: labels, one GitHub issue per work unit (#44–#66), the `P2 integration` issue (#43) and the owner-gate issues. Wave 1 is running; the datasets (E2D, E3D) and QA-P are authorised with the go-ahead and run as ordinary units.

---

## 1. What P2 delivers

| Milestone | Outcome | Ends when |
|---|---|---|
| **M04 Assisted replies** (IMPLEMENTATION_PLAN §6) | The assistant drafts replies; the owner approves, edits or rejects in the control chat; approved replies are sent **exactly once** after a fresh policy check; `/activity` shows everything | PRD §21 R1 items 3–6, 9; restore test; 7-day daily-use trial with zero incorrect sends; owner reviews `docs/milestones/M04.md` → `done` |
| **M05 Inbox intelligence & alerts** (IMPLEMENTATION_PLAN §7) | Triage of incoming bursts, tasks/open loops, notification policy with quiet hours, daily digest, monitors ("notify me when Daniel replies") | PRD §21 R2 items 4–5; digest on 5 consecutive days; E3 gate owner-confirmed; NFR-05 measured; owner reviews `docs/milestones/M05.md` → `done` |
| **P3 readiness** (no code) | E2 authored at the M06 gate size; E4 adversarial set (if capacity); TL proposal for P3 (M06 ∥ M07) | Delivered to the owner |

**Out of scope:** M06 and M07 code (they need M04's send path and M05's triage/notifications/schedules), M08, M09. Inferred memory (S07.4) is deliberately not pulled forward (it widens processing of contacts' data).

**Language (owner, 2026-10-26):** language quality is not a focus now. Reply language/tone/style stay owner preferences passed to the model (PRD §6.10); language checks in evals are informational; the owner schedules native-language reviews of E2/E3 later. Nothing in P2 engineering waits on them, except the formal E3 gate result (which needs owner-confirmed labels, IMPLEMENTATION_PLAN §10.1).

## 2. Team

| Who | Role | Main work |
|---|---|---|
| **Technical Lead (TL)** | Specs, contracts, decisions, architecture & safety sign-off, integration merges, owner-gate coordination | [handoffs/tech-lead.md](handoffs/tech-lead.md) |
| **Neryn** (Claude) | Send-path engineer, schema steward | F0, S04.1, S04.4 — [handoffs/neryn.md](handoffs/neryn.md) |
| **Viro** (Codex) | Product-flow engineer | S04.2, S04.3, E2 target, S05.4 — [handoffs/viro.md](handoffs/viro.md) |
| **Jack** (Claude) | M05 stream engineer | S05.1, S05.2, S05.3b, E3 target — [handoffs/jack.md](handoffs/jack.md) |
| **Junior** (Gemini) | Junior engineer (bounded units, mentored) | E2/E3/E4 data, S05.3a, S04.5, S05.2P — [handoffs/junior.md](handoffs/junior.md) |
| **Antony** (Codex) | QA engineer | Test plan, regression matrix, scenario tests, PR QA review, live checklists, ops tests — [handoffs/antony.md](handoffs/antony.md) |

Roles, review matrix and communication rules: [ORG.md](ORG.md). Git, worktrees, PRs, CI, integration, deploy: [WORKFLOW.md](WORKFLOW.md). Engineers organise their own internal subagents freely within their unit's boundaries; the only constraints are the safety rules in WORKFLOW §2.

## 3. Work-unit index

Every piece of work is a **work unit (WU)** with one owner, one spec, one branch and one PR. Sizes: S ≈ 1–2 days, M ≈ 3–5, L ≈ 1–2 weeks of single-developer effort (IMPLEMENTATION_PLAN §1.5); actual pace with AI engineers is faster (M03 shipped six slices in about four days).

| WU | Title | Owner | Peer reviewer | Size | Depends on | Branch → PR base | Spec |
|---|---|---|---|---|---|---|---|
| **F0** | Foundation: phase schema, privacy cascade, contracts, hot-file refactor | Neryn | Viro + Jack | M | "M03 OK" | `p2/f0-foundation` → `main` | [specs/F0-foundation.md](specs/F0-foundation.md) |
| S04.1 | Actions core and policy engine | Neryn | Viro | M | F0 | `m04/s04.1-actions-policy` → `int/m04` | [specs/S04.1-actions-and-policy.md](specs/S04.1-actions-and-policy.md) |
| S04.2 | Drafting (draft run, propose tools, Assist trigger, outdating) | Viro | Neryn | M | a: "M03 OK"; b: F0 + S04.1 proposal service | `m04/s04.2-drafting` → `int/m04` | [specs/S04.2-drafting.md](specs/S04.2-drafting.md) |
| S04.3 | Approval UX (cards, edit loop, expiry, copy-ready) | Viro | Neryn | M | S04.1, S04.2 | `m04/s04.3-approval-ux` → `int/m04` | [specs/S04.3-approval-ux.md](specs/S04.3-approval-ux.md) |
| S04.4 | Verified execution, kill switch, takeover | Neryn | Viro | L | S04.1 | `m04/s04.4-verified-execution` → `int/m04` | [specs/S04.4-verified-execution.md](specs/S04.4-verified-execution.md) |
| S04.5 | Activity log + assistant activity in catch-up | Junior | Viro (mentor) | S | F0 (S04.1 for real states) | `m04/s04.5-activity-log` → `int/m04` | [specs/S04.5-activity-log.md](specs/S04.5-activity-log.md) |
| E2D | E2 synthetic draft dataset (data only) | Junior | Viro (mentor) | M | — (*pre-start allowed*) | `evals/e2-dataset` → `int/m04` (or `main` before CP1) | [specs/E2-drafts-eval.md](specs/E2-drafts-eval.md) §A |
| E2T | E2 draft eval target | Viro | Antony | S | S04.2 draft run, E2D | `m04/e2-target` → `int/m04` | [specs/E2-drafts-eval.md](specs/E2-drafts-eval.md) §B |
| S05.1 | Triage pipeline, tasks, attention v2 | Jack | Viro | M | F0 (run part: "M03 OK") | `m05/s05.1-triage` → `int/m05` | [specs/S05.1-triage.md](specs/S05.1-triage.md) |
| S05.2 | Notification policy and delivery | Jack | Neryn | M | F0 | `m05/s05.2-notifications` → `int/m05` | [specs/S05.2-notifications.md](specs/S05.2-notifications.md) |
| S05.2P | M05 settings panels and contact priority UI | Junior | Jack (mentor) | S | F0, S05.2 settings keys | `m05/s05.2p-settings-panels` → `int/m05` | [specs/S05.2P-settings-panels.md](specs/S05.2P-settings-panels.md) |
| S05.3a | Schedule maths (pure, DST/timezones) | Junior | Jack (mentor) | S | "M03 OK" | `m05/s05.3a-schedule-math` → `int/m05` | [specs/S05.3a-schedule-math.md](specs/S05.3a-schedule-math.md) |
| S05.3b | Automations core and daily digest | Jack | Neryn | M | F0, S05.3a, S05.2 | `m05/s05.3b-automations-digests` → `int/m05` | [specs/S05.3b-automations-and-digests.md](specs/S05.3b-automations-and-digests.md) |
| S05.4 | Monitors | Viro | Jack | M | F0, S05.2 | `m05/s05.4-monitors` → `int/m05` | [specs/S05.4-monitors.md](specs/S05.4-monitors.md) |
| E3D | E3 synthetic triage dataset (data only) | Junior | Jack (mentor) | M | — (*pre-start allowed*) | `evals/e3-dataset` → `int/m05` (or `main` before CP1) | [specs/E3-triage-eval.md](specs/E3-triage-eval.md) §A |
| E3T | E3 triage eval target + model comparison | Jack | Antony | S | S05.1 run, E3D | `m05/e3-target` → `int/m05` | [specs/E3-triage-eval.md](specs/E3-triage-eval.md) §B |
| E4D | E4 adversarial dataset (data only, M06 prep, optional) | Junior | Viro | M | — | `evals/e4-dataset` → `main` | [specs/E4-adversarial-data.md](specs/E4-adversarial-data.md) |
| QA-P | QA plan, regression matrix, live checklists (docs) | Antony | TL | S | — (*pre-start allowed*) | `qa/p2-plan` → `main` | [qa/](qa/) |
| QA-S4 | M04 acceptance scenario tests | Antony | Neryn + Viro | M | F0 | `qa/m04-scenarios` → `int/m04` | [qa/test-plan.md](qa/test-plan.md) §4 |
| QA-S5 | M05 acceptance scenario tests | Antony | Jack | M | F0 | `qa/m05-scenarios` → `int/m05` | [qa/test-plan.md](qa/test-plan.md) §5 |
| OPS-LV08 | LV-08 reopen half (live, Alviro window) — **deferred, runs only if needed** (decisions B3 E1) | Antony + owner | TL | S | M04 trial shows a need; owner go-ahead | `ops/lv08-probe` → `main` | [specs/OPS-LV08-reopen.md](specs/OPS-LV08-reopen.md) |
| OPS-TV | Trial verifier (content-free send audit) | Antony | Neryn | S | S04.4 | `ops/trial-verifier` → `int/m04` | [specs/OPS-trial-verifier.md](specs/OPS-trial-verifier.md) |
| OPS-RT | Restore test before R1 exit | Antony + TL | Neryn | S | S04.4; run at CP3 | `ops/restore-test-p2` → `int/m04` | [specs/OPS-restore-test.md](specs/OPS-restore-test.md) |

## 4. Execution waves and critical path

```
pre-start (only with the owner's OK; data/docs only, no product code or tests):
    E2D, E3D (Junior) · QA-P (Antony)            — authorised with the go-ahead (owner, 2026-10-27)
──── "M03 OK" (S03.7 merged and deployed) ────────────────────────────────────────────
Wave 1  F0 (Neryn)                 ← critical path
        S04.2 part a: draft run (Viro)       S05.1 run part + S05.2 policy fn (Jack)
        S05.3a schedule maths (Junior)       QA-S4/QA-S5 skeletons (Antony; after F0 types exist)
──── CP1: F0 merged into main; int/m04 and int/m05 cut from main; contracts frozen ────
Wave 2  M04: S04.1 (Neryn) → S04.4 (Neryn)          ← critical path
             S04.2 part b (Viro) → S04.3 (Viro) → E2T (Viro)
             S04.5 (Junior)
        M05: S05.2 → S05.1 rest → S05.3b (Jack; order inside at Jack's discretion)
             S05.4 (Viro, after S04.3 and S05.2)     S05.2P (Junior)
──── CP2: send-path safety review (S04.1 + S04.4 core in int/m04) ────
──── CP3: int/m04 → main (owner), deploy flags off, live window, 7-day trial, restore test ────
──── CP4: int/m05 merges main (M04 present), moves M04 alert callers to notifications ────
──── CP5: int/m05 → main (owner), deploy, friends' session, digest days, E3 gate ────
──── CP6: milestone reports, owner reviews, P3 proposal ────
```

**Critical path:** "M03 OK" → F0 → S04.1 → S04.4 → CP2 → CP3 (merge, deploy, live window) → 7-day trial → M04 report. Everything else is scheduled so nobody waits on it: S04.2/S04.3 and all of M05 run beside it against the F0 contracts; QA writes the send-path tests from the spec while S04.4 is built.

**Calendar estimate (relative to D0 = "M03 OK"; owner gates dominate):** F0 merged ~D2; M04 code-complete ~D8–10; trial D10–17; M05 code-complete ~D11–13; digest days and friends' session overlap the trial; reports ~D17–19.

## 5. Integration checkpoints

| CP | What | Who | Exit criteria |
|---|---|---|---|
| CP0 | Phase kickoff | TL, owner | This package merged to `main`; GitHub labels and one issue per WU created (WORKFLOW §8); engineers' worktrees/clones set up |
| CP1 | Foundation merged | Neryn, TL, owner | F0 PR green in CI, reviewed (Viro, Jack, Antony, TL), merged to `main` by the owner; `int/m04` and `int/m05` created from that `main`; contracts frozen — later changes only through an interface change request (ORG §5) |
| CP2 | Send-path safety review | TL, Antony, Viro | S04.1 merged into `int/m04`; S04.4's Tx A/Tx B protocol, run-bound ingestion barrier and `mark_uncertain` merged; RG-05/06/12/18/19/20 tests green in CI (RG-04 is checked at CP3 with S04.3); TL safety sign-off recorded in `decisions.md` |
| CP3 | M04 to production | owner, TL, Antony | RG-04 and every M04 scenario green; `int/m04` → `main` PR green and merged by the owner; deployed with every new flag **off**; flags enabled in order (WORKFLOW §10); M04 live checklist passed in a production window; restore test passed; trial starts |
| CP4 | M05 catches up | Jack, TL | `int/m05` merged `main` (with M04); S05.2 has moved M04's alert callers (`delivery_uncertain`, connection stale) onto notifications; all M05 tests green |
| CP5 | M05 to production | owner, TL, Antony | `int/m05` → `main` merged by the owner; deployed flags off; enabled in order; friends' scripted session (NFR-05) and monitor checks done; 5 digest days counted; E3 run done (gate result after owner label confirmation) |
| CP6 | Close | TL, owner | `docs/milestones/M04.md` and `M05.md` complete; owner reviews → `done`; P3 proposal delivered |

**Trial protection:** while the M04 7-day trial runs, M05 features that change M04 behaviour stay off — specifically the flag `triage.assist_trigger` (triage replaces ingestion as the Assist draft trigger). Alerts, digests and monitors may go live.

## 6. What the owner does in P2 ("Needs you")

| When | Item | Why it cannot be automated |
|---|---|---|
| CP0 | Merge this docs PR; confirm `gh` works in the Codex/Gemini sessions and which Gemini tool Junior runs in | Owner merges `main`; session setup is the owner's |
| Before D0 | Say "M03 OK" | Gate (AGENTS §3) |
| Pre-start (optional) | Go-ahead for E2D/E3D authoring and QA-P (not given yet; the owner authorises the plan's start after marking M03 done) | Owner's call |
| OPS-LV08 (only if needed) | Deferred (decisions B3 E1): if the M04 trial shows a need, client taps 24 h apart in a closed chat and a per-run go-ahead for Alviro | Needs the owner's Telegram accounts and the production token |
| CP1, CP3, CP5 | Merge F0, `int/m04`, `int/m05` into `main` | Only the owner merges `main` |
| CP3, CP5 | Be present in the live windows (Alviro); enable flags or approve the TL doing it | Production bot; real accounts |
| CP3–CP6 | 7-day daily use; daily look at `/activity` | Owner's own use |
| M05 | Brief two friends; they send scripted messages | Real people |
| Later | Native-language review of E2/E3; E2 ratings; confirm E3 labels | Native-speaker judgement |
| Eval runs | Each live eval run within the USD 2 phase cap | Cost approval (AGENTS §8) |
| CP6 | Review M04.md and M05.md | Milestone completion |

## 7. Document map

| Document | Purpose |
|---|---|
| [decisions.md](decisions.md) | Owner approvals (2026-10-26) and TL decisions for P2; the interface-change log |
| [ORG.md](ORG.md) | Roles, ownership, review matrix, communication, escalation, mentoring |
| [WORKFLOW.md](WORKFLOW.md) | Safety rules, git model, worktrees, compose isolation, tests, PRs, reviews, integration, deploy, flags, live windows, evals |
| [contracts/F0-schema.md](contracts/F0-schema.md) | Exact schema of migrations 0013 and 0014; content columns; retention; deletion cascade |
| [contracts/F0-interfaces.md](contracts/F0-interfaces.md) | Module map, enums, state machine, ports, listeners, job contracts, callback/pending-input kinds, settings, flags, prompt versions, file ownership |
| [specs/](specs/) | One implementation spec per WU |
| [handoffs/](handoffs/) | One start-here brief per person |
| [qa/](qa/) | Test plan, regression matrix, live checklists, evidence template |
| `docs/milestones/M04.md`, `M05.md` | Milestone records (plan pointer now; report at the end) |

Source documents keep their precedence (AGENTS §2): TELEGRAM_FEASIBILITY > PRD > AGENTS §6 > ARCHITECTURE > IMPLEMENTATION_PLAN > these specs. If a spec here conflicts with a higher document, the higher one wins and the engineer raises it with the TL (ORG §5).
