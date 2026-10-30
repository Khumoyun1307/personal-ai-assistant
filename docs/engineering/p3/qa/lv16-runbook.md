# LV-16 rate retest — TL, Alviro, D+6

*Executor: `TL`; SQ supplies the procedure and records safe evidence. Owner answer 2026-10-30, OD-27 and P3-T168. Planned production window: D+6 (2026-11-08), after shadow and before J7 day 1; enable 4 remains later. This is an unexecuted runbook.*

## 1. Entry and baseline

The 2026-10-18 LV-16 observation was five back-to-back sends, five successes in about 1.3 s, no 429. It did not exercise the designed ~1 msg/s per-chat throttle or establish that limits are absent. PQ-03 remains open ([TF §12 LV-16, §13 PQ-03](../../../TELEGRAM_FEASIBILITY.md#12-live-validation-results)).

TL uses **Alviro's current production business connection**, into **the owner's second-account chat only**, already in Alviro's scope and `PROBE_ALLOWED_CHAT_IDS`. Never F1's or F2's chats: those are real people. The dev bot has no business connection and cannot run this test (P3-T168; M02 item 5, M03 handoff, P2-O7). Owner is present; client steps remain the owner's. Substitute runtime identifiers privately; publish aliases only. Agents never open configuration, `.env`, `var/`, backups or real chat data. Console is content-free, **never `--verbose`**. No polling/listen: the production server remains the sole poller.

All three gates must be evidenced before any send. **If any is missing, the run does not happen and LV-16 stays incomplete.**

| Gate | Required evidence |
|---|---|
| Tool change merged before D+6 | TL's merged change makes `burst` stop at the first `TelegramAPIError`, with zero subsequent sends, and adds `--interval SECONDS` between sends. Confirm the probe checkout contains that commit and its focused CI checks pass; no count-one workaround substitutes for it |
| Production allowance approved | Owner-approved AGENTS §9 wording permits non-polling `burst` under `--prod-token` for this allow-listed synthetic run; the merged tool also allows `burst` under that flag. Today only `me` and `getfile` are allowed. TL brings the wording/tooling path with group D; this runbook grants no exception |
| Go-ahead for this run | Owner's dated go-ahead explicitly covers Alviro LV-16 in the D+6 window, target scope and bounded counts. OD-27 assigns the executor; it is not a standing production-run approval |

## 2. Timing and side-effect gates

**Chosen slot: 09:45–10:00 owner-local on D+6**, after shadow S8/S9 and before J7-1. Shadow contact messages keep the 24 h reply window open; bot sends do not refresh it. Before the first burst, TL confirms shadow ended, its delegation is `ended`, catch-up is settled, and S1–S9 evidence (including Pause-button answers) is captured with an explicit shadow end time. Keep LV-16 in its own interval and `P3-90` marker block, outside shadow's `P3-60` evidence. Do not delete or edit messages to tidy evidence.

There must be **no active delegation covering this chat**, and no pending/queued business action able to execute during the burst. Keep `autonomy.send` off. Nobody approves a held shadow draft or sends a new contact/manual test message during LV-16. Check these conditions immediately before sending, then compare safe state/count projections afterwards. The only permitted contact-facing effects are the scripted probe sends; no takeover or application-generated contact-facing action may result.

**Classification expectation:** bot-sent owner messages use `assistant_sent` when `sender_business_bot.id == bot.id`, evaluated before `owner_manual` or `owner_automated` (ARCHITECTURE §6.5). They must not count as owner presence, contact messages, or takeover. However, TF §12 LV-03 observed **no own-bot echo updates**. The running app may therefore ingest none of these out-of-band probe sends: the probe's synchronous send results are authoritative for LV-16 counts, and the probe bypasses application send-time persistence. Do not claim app ingestion or create application action rows to simulate it. Record echo count and classification if an echo is observed; zero echoes means classification not exercised. TL supplies only content-free counts/enums, never payloads or content-bearing rows. An unexpected echo class, takeover delta or application business action stops the run and blocks acceptance pending TL review.

**Hard boundary: all sending ends before J7-1 at 10:00.** Once J7's commitment is sent, the second-account chat stays silent through leg c (D+7 about 11:00; ≥24 h), with no LV-16 send, cleanup, window-opening message or client probe (LIVE-P3 §3.7; OD-26). Record checks may continue without chat traffic. If shadow/evidence collection overruns or a stop/wait cannot fit, there is no valid remaining slot in the unchanged calendar: leave LV-16 incomplete and enable 4 off. Least-bad proposal for TL/owner: retime J7-1 later on D+6, run LV-16 after sealed shadow evidence and before that new start, and move leg c/deadline so the full ≥24 h silence remains. That calendar change needs their explicit agreement; never squeeze the burst into the silence leg or mix it into shadow.

## 3. Exact sequence — bounded by the slot

Run only against the merged interface:

```text
just probe-prod burst <connection_id> <chat_id> <text> --count N --interval S
```

Placeholders are Alviro's current connection and the allow-listed second-account chat. Prepare the count sheet and tool/approval checks before 09:45, without sending. Times below are budgets, not evidence of achieved pace; apply §4 after every result. Run sequentially, never parallel processes or a shell loop that advances after an error.

| Owner-local time | Stage | Bounded operation | What to measure |
|---|---|---|---|
| 09:45–09:48 | Preparation | Seal shadow evidence; confirm ended delegation, no executable business action, open reply window and entry gates; capture safe before-counts | UTC start, commits, client versions or “not recorded”; no probe sends yet |
| 09:48–09:50 | A: rapid, one chat | Command A once: 10 sequential back-to-back attempts, interval 0 | Planned 10 (>5 baseline); actual elapsed/spacing; attempted/succeeded/refused/unknown/not attempted |
| After A | Idle | At least 60 s after A completes, only if A had no error; no catch-up sends | Actual idle seconds; a stop forbids B |
| About 09:51–09:54 | B: paced, same chat | Command B once: 10 attempts, interval 1 second between sends | Actual elapsed and achieved pace; whether ~1 msg/s was exercised, rather than slower invocation/network time |
| 09:54–10:00 | Close and check | No more sends; reconcile counts, compare safe app state/counts and record end before J7 | Echo classes if any, zero new takeover/application business actions, ended delegation; §5 disposition |

```text
just probe-prod burst <connection_id> <chat_id> "P3-9001 Synthetic LV16 rapid." --count 10 --interval 0
just probe-prod burst <connection_id> <chat_id> "P3-9002 Synthetic LV16 paced." --count 10 --interval 1
```

These are stage templates, not a pasteable batch. Inspect A's result and respect idle/stop rules before B. The tool appends an ordinal to each synthetic text. Public evidence stores marker numbers and counts, never Telegram message IDs. Do not infer per-send server timing from a success line containing only an ID/“ok”; record stopwatch elapsed and resolution. Ten paced calls entail at least nine 1-second intervals plus request latency. The allowance is **20 planned attempts total**, no automatic repeat or increased volume. A later approved run gets fresh markers and separate counts, never retries an uncertain attempt.

**Two-chat stage omitted from this one-chat run.** Only if a second owner-controlled chat is already in Alviro's scope may TL propose a conditional shared-connection extension (original count: five sends per chat, ten combined). It additionally needs an allow-list/production allowance and run go-ahead covering both targets, an open reply window in both, no active delegation, and a slot outside shadow/J7 evidence. It is not authorized by the current one-chat gate or included in the 20 attempts. Never substitute F1 or F2. Without that extension, shared-limit behaviour is unmeasured.

## 4. Stop rules

| Observation | Action |
|---|---|
| First 429 / `error_class=TelegramRetryAfter`, with `retry_after` | Tool stops internally before any next send; TL ends all sending stages. Honour at least the reported seconds from receipt with no further probe sends on the connection. Record value and actual wait. This session sends nothing further, even after the wait; never delay J7 silently if the wait overlaps its start |
| 429 without a usable `retry_after` | End session, record missing value, leave retest incomplete; no guessed wait followed by a send |
| Any other `TelegramAPIError` | Tool stops internally at that error. End session; record class/count only; no next stage or retry |
| Missing safe result, process failure, unknown/ambiguous outcome | End session; count unknown separately, never retry or infer success |
| Wrong target/configuration, delegation active, unexpected echo class/takeover/action, client reports unexpected effect, or time boundary reached | Stop immediately and notify TL/owner; no continuation. Apply LIVE-P3 §3.12 for an unintended send |
| No refusal | Proceed only within the bounded script and available slot. Absence of 429 is an observation at measured load, not proof of no limits |

The merged tool must guarantee first-error stopping; Ctrl+C is not that guarantee. Do not rely on shell exit 0 as proof of success: reconcile safe per-attempt outcomes and confirm **zero further attempts after the first error**. No automatic retry or error-message parsing wrapper is introduced. Do not publish exception messages or raw console transcripts (AGENTS §7/§9).

## 5. TF record and enable 4

TL adds an **agent-run follow-up line within TF §12's existing LV-16 row**, preserving the 2026-10-18 baseline, as P3-T168 requires. Do not create a new LV entry, call it owner-executed, or mark anything *Verified*. This launch edits only QA documents; the later TL records the actual result. Only content-free fields enter the public record:

| Field | Record |
|---|---|
| Date / executor / authority | UTC date; TL agent-run; OD-27/P3-T168; approved AGENTS §9 allowance and per-run go-ahead references/times |
| Clients / commits | Versions or not recorded; merged probe SHA, deployed app SHA; synthetic text only |
| Slot / stage / scope | Shadow end/evidence cut-off; LV-16 UTC/local start/end; J7 actual start; A/B; one owner-controlled chat, same connection; no IDs |
| Counts | Planned; attempted (“send calls”); succeeded; definitely refused; unknown; not attempted. Attempted = succeeded + refused + unknown |
| Timing | Per-stage start/end UTC; elapsed and resolution; idle seconds; configured interval; achieved pace; target achieved yes/no |
| Rate refusal | First refusal ordinal; fixed error class; `retry_after` seconds or absent; waited seconds; zero further attempts |
| App side effects | Delegation ended before/after; executable business actions before/after; echo count and class counts or no echo/classification not exercised; takeover and application business-action deltas zero; owner unexpected-effect yes/no |
| PQ-03 conclusion | No 429 at measured load / 429 observed at that load / incomplete. One chat cannot isolate bot-, connection- and chat-wide limits |
| Throttle conclusion | Keep the designed ~1 msg/s per-chat throttle. If it failed at that pace or slower, enable 4 waits for TL classification and reviewed throttle change/retest. Never raise the throttle from a successful burst |
| Coverage gap | Every NOT RUN/NOT EVALUABLE stage, pace not achieved, missing side-effect projection, no echo, two-chat stage omitted, or time/permission/tool gate missing |
| Enable-4 disposition | Accepted by TL with owner decision reference if required / blocked pending complete evidence |

Sources: [decisions §A3/§B11](../decisions.md); [README §6 enable 4](../README.md#6-release-deploy-and-flags); [LIVE-P3 §3.3–3.7](../specs/LIVE-P3.md); TF §12 LV-16/LV-03, §13 PQ-03; ARCHITECTURE §6.5. Do not close PQ-03 merely because no 429 occurred. A 429 is not itself a failed observation; ignoring the stop or `retry_after` invalidates the run.

**Guard:** “LV-16 done” needs accepted dated results at the intended load, safe stop handling, side-effect checks and a throttle conclusion. Missing tool/allowance/go-ahead, unmeasured required load, incomplete checks or an unaccepted result leave LV-16 incomplete and `autonomy.send` off. `p3-verify release` does not inspect this documentary gate. CP-G, CP-A, clean shadow and ≥24 h notice remain separate enable-4 guards.
