# LV-16 rate retest — TL, dev bot

*Executor: `TL`, not owner (owner answer 2026-10-30, OD-27, this launch). SQ supplies the procedure; TL records the result. About 15 minutes before enable 4. This is an unexecuted runbook. Do not change `tools/probe/`.*

## 1. Entry and baseline

The 2026-10-18 LV-16 observation was five back-to-back sends, five successes in about 1.3 s, no 429. It did not exercise the designed ~1 msg/s per-chat throttle or establish that limits are absent. PQ-03 remains open ([TF §12 LV-16, §13 PQ-03](../../../TELEGRAM_FEASIBILITY.md#12-live-validation-results)).

TL uses the **dev bot only** under the AGENTS §9 probe standing exception. Sends contain only this procedure's synthetic text and go only to two chats already in `PROBE_ALLOWED_CHAT_IDS`, on the same current dev connection. Owner does any needed client connection/window-opening actions. Do not ask for or publish the IDs, read configuration, open anything under `var/`, or use production credentials. Console is content-free, **never `--verbose`**. No polling/listen, no new tool or probe command (AGENTS §9; user work-item boundary).

## 2. Existing-command limitation and safe execution

The only sending command is:

```text
just probe burst <connection_id> <chat_id> <synthetic-text> --count N
```

`justfile` recipe `probe` invokes `tools/probe/probe.py`. `cmd_burst` sends sequentially, logs one result per attempt and **continues its for-loop after every TelegramAPIError, including a 429**. There is no pace option, no stop-on-error option and no aggregate exit verdict. `cmd_summary` can read a dump, but this runbook does not use it: only the burst command is authorized here. Never inspect the dump to recover results (`tools/probe/probe.py::cmd_burst`, `::cmd_summary`, `::parser`; read-only source inspection in QA-P3).

Therefore a single `--count 10` cannot guarantee “stop at the first 429.” Ctrl+C may arrive after the next send starts; it is not a hard stop. **Do not run a multi-send invocation under this stop rule.** Use one-send invocations, inspect the safe result after each and launch the next only on success. There is no automatic loop or parallel invocation. Each is a new synthetic test send, never a retry of an earlier attempt.

This gives a bounded, supervised sequence above the old five-send total. Process/container startup may prevent a true high-rate burst or ~1 s spacing. Record measured elapsed time; do not call slow invocations a high-rate result. The exact multi-send target commands in §3 are **NOT RUN / blocked** until TL resolves that incompatibility within the owner's rules. No change or relaxation is authorized by this runbook.

## 3. Exact sequence — about 15 minutes

Runtime placeholders: `<connection_id>` is the dev connection, `<chat_A>`/`<chat_B>` are the two allow-listed chats. Substitute privately; no identifier appears in committed evidence. Keep a stopwatch and a count sheet. Times below are targets, never evidence of achieved pace. The stop rules in §4 apply after **every** result.

| Time | Stage | Supervised sequence using existing command only | What to measure |
|---|---|---|---|
| 00:00–02:00 | Preparation | Confirm dev configuration/allow-list through the existing approved setup, not by opening secrets. Owner confirms both test chats have a valid reply opportunity. Record A/B aliases and client versions or “not recorded” | Date, UTC start, probe commit; no sends yet |
| 02:00–05:00 | A: back-to-back, one chat | Run command A below **ten times**, one at a time. Replace `<nn>` with 01…10. Inspect each result before launching the next; no added wait on success | 10 total planned (>5); actual start/end elapsed; attempts/successes/refusals/unknowns; observed spacing if available |
| 05:00–08:00 | B: paced A | After at least 60 s idle, run command B below **ten times**, `<nn>`=01…10. Target send starts ≥1 s apart; inspect between sends. If startup takes >1 s, use the actual slower pace, never queue a catch-up | Actual elapsed and achieved pace; whether near ~1 msg/s was achieved or remains NOT EVALUABLE |
| 08:00–12:00 | C: shared connection, two chats | After ≥60 s idle, run C-A then C-B below, five pairs, `<nn>`=01…05. Same connection; sequential, inspect after each. No parallel processes | Per-chat counts (5 each), combined 10, first refusal chat alias, total elapsed |
| 12:00–15:00 | Record | Reconcile count sheet against safe console lines; no more sends. TL prepares TF record and enable-4 verdict | See §5 |

```text
just probe burst <connection_id> <chat_A> "P3-9001 Synthetic LV16 rapid A attempt <nn>." --count 1
just probe burst <connection_id> <chat_A> "P3-9002 Synthetic LV16 paced A attempt <nn>." --count 1
just probe burst <connection_id> <chat_A> "P3-9003 Synthetic LV16 pair A attempt <nn>." --count 1
just probe burst <connection_id> <chat_B> "P3-9004 Synthetic LV16 pair B attempt <nn>." --count 1
```

These are command templates, not a pasteable batch. Run the first line for stage A, second for B, third/fourth alternating for C. `<nn>` is part of synthetic text only; the tool also appends its own `#1`. Do not infer per-send server timing from a success line if it prints only `message_id`/“ok”; record stopwatch timing and its resolution. Public evidence omits actual Telegram message IDs.

The intended **internal** high-rate comparisons, not executable with a guaranteed first-429 stop in the current tool, are:

```text
just probe burst <connection_id> <chat_A> "P3-9005 Synthetic LV16 internal rapid A." --count 10
just probe burst <connection_id> <chat_A> "P3-9006 Synthetic LV16 internal pair A." --count 6
just probe burst <connection_id> <chat_B> "P3-9007 Synthetic LV16 internal pair B." --count 6
```

Record these as NOT RUN, never imply that the one-send substitute exercised the same load. If ten supervised invocations consume the allotted time, end with measured coverage and pending stages; do not increase volume or extend a live run silently. A new run must retain separate counts; it never repairs a refused send by retry.

## 4. Stop rules

| Observation | Action |
|---|---|
| First 429 / `error_class=TelegramRetryAfter`, with `retry_after` | End all sending stages immediately. Wait at least the reported seconds from receipt; no other test sends on either chat/connection during that wait. Record the value. This session sends nothing further, even after the wait |
| 429 without a usable retry_after | End session, record missing value, leave retest incomplete; no guessed wait followed by a send |
| Other refusal (`error_class`), missing console line, process failure, unknown/ambiguous outcome | End session; record class/count only; never retry the attempted send or infer it succeeded |
| Wrong target/configuration, client reports an unexpected effect | Stop immediately, notify TL/owner; no continuation |
| No refusal | Proceed only within the scripted bounded stage; absence of 429 is an observation at measured load, not proof of no limits |

The one-send boundary guarantees no later send **within this procedure** follows the first observed 429. Do not rely on shell exit 0 as proof of success: `cmd_burst` catches Telegram errors and continues/returns normally. No automatic retry; no broad exception or output-message parsing wrapper is introduced (AGENTS §7; tool source in §2).

## 5. TF record and enable 4

TL appends a new LV-16 observation to **TF §12**, preserving the 2026-10-18 baseline; records OD-27's executor exception explicitly rather than describing it as owner-executed. Only counts/enums/timing go into the public record. Suggested record fields:

| Field | Record |
|---|---|
| Date / executor / authority | <UTC date>; TL; owner OD-27 2026-10-30 dev-probe exception |
| Clients / probe commit | <versions or not recorded>; <SHA>; synthetic text only |
| Stage and scope | <A/B/C>; <one chat / two allow-listed chats, same connection>; no IDs |
| Counts | Planned; attempted (“sent calls”); succeeded; definitely refused; unknown; not attempted. Attempted = succeeded + refused + unknown |
| Timing | Start/end UTC; elapsed and resolution; achieved pace; target achieved yes/no |
| Rate refusal | First refusal ordinal; fixed error_class; retry_after seconds or absent; waited seconds; zero further attempts |
| PQ-03 conclusion | No 429 at measured load / 429 observed at that load / incomplete. Two-chat data may suggest shared pressure; it cannot isolate bot-, connection- and chat-wide limits |
| Throttle conclusion | Keep the designed ~1 msg/s per-chat throttle. If it failed at that pace or a slower one, enable 4 waits for TL classification and reviewed throttle change/retest. Never raise the throttle from a successful burst |
| Coverage gap | Internal count>1 burst and/or near-1 s pacing not measured; distinguish every NOT RUN/NOT EVALUABLE stage |
| Enable-4 disposition | Accepted by TL with owner decision reference if required / blocked pending complete evidence |

Sources: TF §12 LV-16, §13 PQ-03; [README §6 enable 4](../README.md#6-release-deploy-and-flags); [LIVE-P3 §3.4](../specs/LIVE-P3.md#34-deploy-and-enable-step-verification). Do not close PQ-03 merely because no 429 occurred. A 429 is not itself a failed observation; ignoring the stop or retry_after would invalidate the run.

**Guard:** “LV-16 done” needs accepted dated results at the intended load, safe stop handling and a throttle conclusion. The startup-paced substitute alone does not close an unmeasured internal high-rate/pacing requirement. Until TL resolves that gap and accepts the evidence, leave LV-16 incomplete and `autonomy.send` off; `p3-verify release` does not inspect this documentary gate. CP-G, CP-A, clean shadow and ≥24 h notice remain separate enable-4 guards.

AGENTS §2/§16 and TF §12 currently reserve the Verified label for owner-executed live tests; older P3 role tables also say owner runs LV-16. TL must reconcile those references with the owner's specific OD-27 update. Record a **TL-executed observation**, not a fabricated owner-run or self-awarded Verified label. QA-P3 changes none of those files.
