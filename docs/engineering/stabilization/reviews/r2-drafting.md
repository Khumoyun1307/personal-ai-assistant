**1. Summary**

- The owner’s difficulties follow from concrete gaps in intent preservation and card lifecycle handling.
- Clarifying turns can erase dictated text and the substance of a requested message.
- “Write to X” is forced into drafting even when the owner has supplied no content.
- Re-draft regenerates a generic reply instead of preserving the original task.
- Two freshness races allow drafts to survive changes made while the drafting model runs.
- AI revisions of dictated cards retain an incorrect provenance classification, affecting consent cancellation and warnings.
- The core committed-claim protocol supports at-most-once sending by static inspection; I found no direct-send agent tool.
- The preliminary claim that owner runs receive no live-card state is outdated: they receive a limited snapshot, which still leaves important gaps.
- Existing tests sometimes explicitly preserve undesirable product behaviour; evals do not adequately cover the reported clarification sequences.

**2. Findings, most severe first**

**R2-01 — A manual reply during owner-requested drafting does not reliably stop the resulting draft**  
**Severity:** high · **Category:** correctness

**Evidence:** `src/assistant/agent/tools/replies.py:456` awaits `drafter.draft_owner()` before creating the proposal at line 476. `src/assistant/actions/proposals.py:100` creates the action’s timing facts after drafting. In `src/assistant/actions/policy_loader.py:198`, owner-requested actions deliberately receive no base-message timestamp. Consequently, `src/assistant/policy/rules.py:138` uses:

> `reference = freshness.base_message_sent_at or ctx.action.created_at`

**Failure scenario:** The owner requests a reply, then answers the contact manually while the model is working. The draft finishes afterward. The manual reply predates the newly created action, so the takeover rule passes. The listener could not invalidate an action that did not yet exist.

**Root cause:** Action creation time substitutes for the time the owner requested the work. The R3 assumption that an owner request postdates the owner’s manual replies fails across a slow model call.

**Recommended fix:** Carry the request’s takeover reference through drafting and proposal creation. Under the conversation lock, reject work overtaken since that request. Add a regression with a manual reply committed during the model call.

**Touches:** AGENTS §6.10; PRD §12.2.5; P2 decision R3 requires amendment.  
**Confidence:** confirmed by reading code; not executed.

---

**R2-02 — Message edits during drafting bypass freshness checks**  
**Severity:** high · **Category:** correctness

**Evidence:** `src/assistant/agent/drafting.py:491` captures the highest message ID before calling the model. `propose_draft()` at line 974 checks only whether a newer contact-message ID exists. `src/assistant/actions/ingestion.py:162` finds existing actions by `Action.base_message_id` when handling an edit. `src/assistant/actions/policy_loader.py:192` checks base deletion/purge, but does not compare the source’s edit version or content hash.

**Failure scenario:** The model reads a synthetic appointment for Tuesday. The contact edits it to Wednesday during generation. There is no action for the edit listener to invalidate, and the message ID remains unchanged. The resulting Tuesday draft can remain approvable, potentially alongside an excerpt showing Wednesday.

An additional limitation survives after proposal creation: edits to other messages used by the draft do nothing. `tests/integration/test_draft_outdating.py:196` explicitly expects that behaviour.

**Root cause:** A message-ID watermark detects arrivals, not changes to the content already read.

**Recommended fix:** Fence drafting with a conversation content revision, advanced on relevant arrivals, edits and deletions; compare it under the proposal lock. Retain sufficient dependency information to invalidate drafts when their source context changes.

**Touches:** AGENTS §6.1; FR-DRF-05; PRD §12.2 relevance checks.  
**Confidence:** confirmed by reading code.

---

**R2-03 — AI-rewritten dictated cards remain classified as owner-dictated**  
**Severity:** high · **Category:** safety

**Evidence:** `src/assistant/telegram/control/approvals.py:1694` uses `owner.text` for instruction-based revisions. At line 1814 it determines provenance from the original action:

> `dictated = before.origin is ActionOrigin.OWNER_DICTATED`

At lines 1822–1840 it leaves dictated cards without draft flags. `src/assistant/actions/lifecycle.py:751` updates text and version without changing origin.

That classification controls more than the header:

- `actions/lifecycle.py:179` cancels pending AI-generated actions only for `AGENT_PROPOSAL`.
- `actions/executor.py:356` sets `ai_generated` from that same origin.
- `approvals.py:581` suppresses warnings for `OWNER_DICTATED`.

**Failure scenario:** The owner dictates a message, then asks for an AI rewrite. The rewritten text still appears as “Message you dictated,” lacks generated-content warnings, survives managed-consent cancellation, and receives the dictated-text execution treatment.

**Root cause:** Action origin is treated as immutable provenance even though revisions can change authorship.

**Recommended fix:** Track provenance per version. An AI instruction rewrite must become generated content for rendering, cancellation and execution checks. Literal replacement can retain owner authorship.

**Touches:** AGENTS §6.7 and §6.11; consent-related action semantics. This is **not** evidence that the provider-call consent wrapper is bypassed.  
**Confidence:** confirmed by reading code.

---

**R2-04 — Clarification breaks both dictation and the substance handed to the drafting model**  
**Severity:** high · **Category:** owner-UX

**Evidence:** `src/assistant/agent/tools/replies.py:580` accepts owner dictation only from:

> `text_occurs_in(text, turn.own_words)`

It searches earlier assistant replies, but never earlier owner instructions. At lines 404–406, drafting receives only the current turn’s instruction, while:

> `passed_guidance = None if ctx.tainted else guidance`

`src/assistant/agent/draft_context.py:194` adds that instruction without control-chat history.

**Failure scenario:** “Tell Alex: [exact text].” The bot asks which Alex. The owner answers the name. The original text no longer qualifies as dictated. If the main model switches to `propose_reply`, the drafting model receives the name clarification instead of the original message. In a tainted run, the main model’s reconstructed guidance is discarded too.

**Root cause:** The system has conversational history for routing, but no durable representation of the message-writing task being clarified.

**Recommended fix:** Preserve a pending drafting request containing recipient selection, mode, original owner-authored spans, exact text and requested constraints. Clarification should fill missing fields. Provenance should refer to those source spans rather than require repetition in the latest turn.

**Touches:** FR-DRF-08, R-05; P2 D2/R2; AGENTS §6.3 and §6.4 must remain intact.  
**Confidence:** confirmed by reading code.

---

**R2-05 — Missing message content is treated as permission to invent a reply**  
**Severity:** high · **Category:** owner-UX

**Evidence:** `src/assistant/agent/prompts/owner_request.v10.md:40` says an explicit request to write is “always carried out.” `draft_owner.v3.md:10` says:

> “With no request, write the reply the conversation calls for.”

`src/assistant/agent/draft_owner_schemas.py:33` provides output fields for a message, but no clarification outcome. `propose_reply_tool()` has no deterministic check for missing substance.

**Failure scenario:** “Write to Alex.” The system resolves Alex and generates something from the chat rather than asking what the owner wants to say. After an instruction is lost, the same fallback produces a plausible but unrelated reply.

**Root cause:** Removing the “no reply needed” veto also removed a distinction between refusing an explicit task and gathering information needed to perform it.

**Recommended fix:** Introduce a clarification outcome before drafting when a standalone message lacks content or purpose. Preserve D1’s intended behaviour for actual requests to answer a conversation.

**Touches:** Owner decision D1; PRD §15 ambiguous-instruction behaviour; no direct send authorization bypass.  
**Confidence:** confirmed routing/schema gap; exact model wording is probabilistic.

---

**R2-06 — Late Assist work explicitly supersedes an owner’s dictated card**  
**Severity:** high · **Category:** owner-UX

**Evidence:** `src/assistant/agent/drafting.py:1169` checks for a covering live card before generation. `_settle()` at line 1221 proposes afterward without repeating that precedence check. `src/assistant/actions/proposals.py:146` supersedes the conversation’s live card regardless of origin.

`tests/integration/test_assist_drafting.py:234` explicitly verifies that an Assist draft finishing after an owner dictates marks the dictated card `outdated/superseded`.

**Failure scenario:** A proactive draft starts. The owner dictates the precise message they want. The proactive model finishes later and replaces that card with its own reply. The owner sees their deliberate instruction become “Outdated” despite no newer contact message.

**Root cause:** “One live card per conversation” is implemented as last-writer-wins, with no priority for explicit owner work.

**Recommended fix:** At settlement, discard proactive work if explicit owner drafting, revision or approval has overtaken it. Keep the single-card constraint, but make owner intent win.

**Touches:** P2 single-card decision; FR-DRF-08; AGENTS §6.11 status clarity.  
**Confidence:** confirmed by code and an explicit existing regression expectation.

---

**R2-07 — Re-draft loses the original mode, content, target and constraints**  
**Severity:** high · **Category:** correctness

**Evidence:** `src/assistant/telegram/control/approvals.py:1324` requests Re-draft using only the conversation and `DraftReason.REDRAFT`. `src/assistant/actions/drafts.py:117` queues only those fields. `src/assistant/agent/drafting.py:1187` then calls generic `drafter.draft()`, which uses `draft_reply.v1`.

The original action ID, owner instruction, `new_message` mode, answered message and word bounds do not travel through this path.

**Failure scenario:** An owner-requested new message becomes outdated. Re-draft produces a reply to the latest contact message instead. An earlier-message answer loses its target; a constrained draft loses its requested length.

There are also silent outcomes: `_skip_code()` at line 1342 rejects a closed estimated window, and failures/`NOT_NEEDED` are audited without an owner-facing completion notice. This contradicts the card’s “a new card will follow” wording at `approvals.py:645`.

**Root cause:** Re-draft reuses the proactive-generation job instead of repeating the selected drafting task.

**Recommended fix:** Queue the source action/request ID, reload its retained task specification, and preserve semantics. Report every terminal no-card outcome. Apply P2-O20 consistently to owner-requested re-drafting.

**Touches:** D1/D3, P2-O20, AGENTS §6.8 and §6.11.  
**Confidence:** confirmed by reading code.

---

**R2-08 — Quoting third-party text also discards the owner’s separate instruction**  
**Severity:** medium · **Category:** owner-UX

**Evidence:** `src/assistant/agent/tools/registry.py:165` already distinguishes `own_words` from quoted content. Nevertheless, `OwnerTurn.instruction` at line 177 returns `None` whenever the turn is quoted or managed. `replies.py:406` also drops guidance because the run is tainted.

`tests/integration/test_owner_drafting.py:169` expects the owner instruction to be absent while a card is still produced.

**Failure scenario:** The owner quotes a contact’s message and writes “Decline politely; say I cannot attend.” The drafting model loses those owner-authored instructions and can generate a different response from the conversation.

**Root cause:** Trust is applied to the entire turn rather than its separately stored authored components.

**Recommended fix:** Seek an owner-approved D2 amendment that preserves the owner’s outer instruction while keeping quoted material untrusted and scoped. Until then, ask a targeted clarification instead of generating without the instruction.

**Touches:** P2 D2; AGENTS §6.3, §6.4 and §6.9.  
**Confidence:** confirmed by reading code.

---

**R2-09 — Card state is supplied, but remains incomplete and can become stale before the assistant speaks**  
**Severity:** medium · **Category:** correctness

**Evidence:** `src/assistant/agent/runtime.py:418` loads live cards. `actions/activity.py:271` includes only `awaiting_approval` and `revising`. `agent/context.py:134` renders IDs/state without text, and displays at most 20 cards.

Proposal results at `tools/replies.py:316` report state plus routing facts; `_drafted_facts()` at line 513 does not report the message’s actual substance. `get_draft` can retrieve text and state, but `_CARD_COLUMNS` in `tools/drafts.py:198` omits `state_reason`.

**Failure scenario:** A proposal result says awaiting approval. A contact update outdates the card before the final assistant response. The model still announces a pending draft. On a later turn, the terminal card is absent from the live snapshot. Explaining its text or status depends on the model choosing `get_draft`; explaining why it became outdated remains under-specified.

**Root cause:** A startup snapshot and ordinary tool result are treated as sufficient grounding for asynchronous card narration.

**Recommended fix:** Ground final status statements in refreshed action facts; make content claims require reading the actual version. Include terminal transitions relevant to the conversation, state reasons and presentation status.

**Touches:** AGENTS §6.11; owner-request rules 19/26.  
**Confidence:** confirmed data gaps; contribution to the reported contradictions is likely.

---

**R2-10 — Any Telegram 400 marks a card synchronized, even when its new text never appeared**  
**Severity:** medium · **Category:** reliability

**Evidence:** `src/assistant/telegram/control/approvals.py:1055` accepts:

> `result.ok or result.error_class is ErrorClass.BAD_REQUEST`

It then calls `_record()` at line 1068. `sync_cards()` at line 1458 subsequently selects only cards whose recorded state differs from the action state.

`tests/integration/test_card_sync.py:153` covers a parse error as a settled refusal.

**Failure scenario:** A short revised card fails to edit because of a parse error or missing message. The database records the current card as shown, although the owner still sees the previous text/token or no card. Periodic synchronization no longer repairs it.

**Root cause:** “Message is not modified” and other edit failures share one broad success-equivalent branch.

**Recommended fix:** Classify “not modified” separately. Record presentation only after success or a narrowly justified equivalent response. Preserve a recoverable unshown state for other failures.

**Touches:** FR-DRF-03/04; AGENTS §6.11.  
**Confidence:** confirmed by reading code.

---

**R2-11 — Uncertain card presentation can strand an approval permanently without an owner recovery path**  
**Severity:** medium · **Category:** reliability

**Evidence:** `src/assistant/telegram/control/approvals.py:960` treats full-text success followed by card refusal as ambiguous. `_pending_presentation()` at line 801 suppresses another presentation for that version. Both sync queries exclude unresolved presentations at lines 1459 and 1465.

`tests/integration/test_approvals.py:568` explicitly leaves an `awaiting_approval` action with no card pointer and verifies that synchronization does nothing.

**Failure scenario:** The owner receives the full draft text, but Telegram rejects the subsequent buttons. The action remains pending without an approvable card. Chat can still describe it as awaiting approval. Restarting does not repair it.

**Root cause:** A replay guard for control-chat presentation has no corresponding owner-visible uncertain state or recovery operation.

**Recommended fix:** Expose “card delivery uncertain” distinctly. Provide an explicit owner-triggered “show current card” recovery with fresh controls, preserving the business action’s at-most-once protection. Changes must respect or amend the recorded #131 presentation decision.

**Touches:** AGENTS §6.11; FR-DRF-03; approval presentation replay decision.  
**Confidence:** confirmed by code and explicit test expectations.

---

**R2-12 — Same-turn deduplication silently ignores different proposal arguments**  
**Severity:** medium · **Category:** correctness

**Evidence:** `src/assistant/agent/tools/replies.py:274` reuses any live action for the same owner turn and conversation. Both proposal tools return it before considering the new request. `tests/integration/test_reply_tools.py:599` submits `SYNTH-X`, then `SYNTH-Y` with a different call ID, and expects the card to remain `SYNTH-X`.

**Failure scenario:** The main model stages a reply, then corrects itself within the same turn and proposes the dictated text or standalone message. The second call reports reuse and leaves the original incorrect card untouched.

**Root cause:** Retry idempotency and distinct operations within one model run share the same key.

**Recommended fix:** Retain exact-call idempotency. For a different request against the turn’s existing card, return an explicit conflict requiring revision, or apply a deliberate revision. Do not report ordinary reuse for semantically different arguments.

**Touches:** ARCHITECTURE §8.3; P2 retry/single-card decisions; AGENTS §6.11.  
**Confidence:** confirmed by reading code and tests.

---

**R2-13 — Earlier-message targeting is limited to the newest 30 messages, with unsafe semantic fallback**  
**Severity:** medium · **Category:** correctness

**Evidence:** `src/assistant/agent/drafting.py:589` limits the drafter to at most 30 retained messages. `answerable_ids()` at line 406 uses that same window. Explicit targets outside it are rejected by `tools/replies.py:439`, although `get_conversation` supports older pages.

`draft_owner_schemas.py:75` rejects an invalid model-selected target; `drafting.py:705` then keeps the newest-message default. During revision, `approvals.py:1753` drops an explicit target from the model context when it leaves that window, while the card can keep its old base ID.

**Failure scenario:** Search finds the older message the owner wants answered, but drafting cannot use its ID. Alternatively, a revision is written without seeing its selected target while the card continues to describe that target.

**Root cause:** Context-window membership substitutes for target validity; semantic targeting failures fall back to a different message.

**Recommended fix:** Load the explicitly selected retained message within account/conversation scope, plus bounded surrounding context. Clarify or fail explicitly when the target cannot be used; do not silently answer another message.

**Touches:** D5/PF-F; FR-DRF-03; AGENTS §6.4 and §6.11.  
**Confidence:** confirmed by reading code.

---

**R2-14 — “Exact dictation” accepts text that differs from the owner’s actual characters**  
**Severity:** medium · **Category:** correctness

**Evidence:** `src/assistant/agent/tools/replies.py:564` applies NFKC, case folding and quote substitutions; line 565 additionally maps every Latin `x` to `h`. `text_occurs_in()` strips punctuation and performs a substring check. `propose_message_tool()` persists `args.text`, rather than extracting the original source span.

**Failure scenario:** An owner’s synthetic code `X1` can match model-supplied `H1`. Case-sensitive values, punctuation and compatibility characters may also change while the card is labelled owner-dictated.

**Root cause:** A fuzzy recognition function is used to establish literal authorship.

**Recommended fix:** Use normalization to locate candidate source spans, then copy the original characters. Otherwise label the result as transformed text and require an explicit interpretation decision.

**Touches:** R-05; FR-DRF-08; AGENTS §6.11.  
**Confidence:** confirmed by reading code.

---

**R2-15 — Draft evals exclude the central clarification failure and do not establish current production quality**  
**Severity:** medium · **Category:** reliability

**Evidence:** `evals/owner_drafting.py:333` requires dictated expected text to occur in the current turn. Thus a case expecting dictation preserved from an earlier owner turn fails dataset validation. At line 921, the harness runs one final owner turn against seeded history rather than exercising the actual clarification exchange.

Recorded evidence is also limited:

- `docs/evals/2026-10-29-e2-drafts-gpt-6-luna.md:38`: 23 drafts flagged for unsupported commitments; line 41: intent agreement 69/146; no owner ratings.
- `docs/evals/2026-10-30-owner-drafting-effort-medium-gpt-6-luna.md:10`: the full run used `draft_owner.v2`.
- The v3 follow-up report at line 3 identifies itself as a four-case trial.
- `docs/evals/README.md:148` keeps E2 pinned to low effort, whereas the documented production default moved to medium.

**Failure scenario:** One-turn dictation and selected earlier-message cases improve, while resolving a recipient, preserving content and recovering a card remain untested as conversations.

**Root cause:** Evaluation follows component contracts that already encode the problematic current-turn restriction.

**Recommended fix:** Add synthetic multi-turn scenarios for the reported incidents, card invalidation during generation, proactive/owner races and failed presentation recovery. Re-measure the active prompts and configured effort with owner-approved spend.

**Touches:** AGENTS §8/§11; ADR-07a and ADR-16. Historical pre-screen results are not proof of current production failure rates.  
**Confidence:** confirmed by reading harness and reports.

---

**R2-16 — The stated maximum throttle wait is not enforced**  
**Severity:** medium · **Category:** reliability

**Evidence:** `src/assistant/telegram/gateway.py:61` declares `MAX_THROTTLE_WAIT_SECONDS = 10`. `_throttle()` at line 176 waits for a per-chat lock and then sleeps until the reserved slot, without enforcing that bound. `bootstrap.py:276` nevertheless uses it to calculate the stale-executing uncertainty threshold.

The executor commits its claim before entering this wait. The gateway also resolves its target before throttling at line 332.

**Failure scenario:** Under sufficient outbound backlog, an action waits longer than the assumed bound. It can be marked delivery-uncertain before its first request starts, then send later. Policy and connection checks are also older than the intended bounded gap.

**Root cause:** A documented timing assumption is used as a correctness bound without enforcement.

**Recommended fix:** Bound total throttle acquisition/reservation time. Prefer waiting for capacity before the final policy claim, followed by prompt dispatch. Distinguish definitely-not-started work from ambiguous network delivery.

**Touches:** ADR-09; AGENTS §6.1 and §6.11. This does not itself create an automatic duplicate-send path.  
**Confidence:** confirmed missing bound; production occurrence is unverified.

---

**R2-17 — Card wording contradicts message mode and ignores the owner’s interface language**  
**Severity:** low · **Category:** owner-UX

**Evidence:** `src/assistant/telegram/control/approvals.py:189` labels requested messages “Draft reply.” `_header()` at line 484 does not inspect `draft_flags["mode"]`. Approval buttons, edit instructions, state reasons and copy instructions are fixed English strings.

**Failure scenario:** A correctly generated standalone `new_message` still appears as “Draft reply.” An Uzbek- or Russian-speaking owner receives localized chat narration beside English operational controls, reinforcing uncertainty about what the system actually did.

**Root cause:** Rendering distinguishes origin but not message intent, and does not use the locale machinery available elsewhere.

**Recommended fix:** Render separate new-message/reply labels and localize card controls and status explanations. Keep recipient identity and exact outgoing text prominent.

**Touches:** Owner language preferences; FR-DRF-03; no direct §6 safety violation.  
**Confidence:** confirmed by reading code.

**3. Rule interactions**

1. **Recipient caution destroys the message being clarified.**  
   `conversations/read.py:598` returns ranked strong and weak matches together. `agent/prompts/owner_request.v10.md:58` requires clarification if *any* candidate is weak, even when the first candidate is a strong match. The clarification then triggers R2-04’s current-turn provenance restriction. Improve candidate dominance rules and retain the selected recipient within the pending drafting task.

2. **Useful chat access disables the main model’s ability to explain the task to the drafter.**  
   Contact resolution returns untrusted names and taints the run (`agent/tools/conversations.py:5`). Historical third-party turns also taint later runs (`agent/context.py:118`). Taint then removes guidance. The remaining current-turn instruction works for self-contained requests but fails for references, clarifications and quoted requests.

3. **“Always carry out the owner’s request” combines with “only pass safe instructions” to generate uninstructed messages.**  
   D1 removes the drafter’s veto; D2/R2 can remove both instruction channels. `draft_owner.v3` resolves that empty-input situation by answering the chat. Each rule has a rationale, but their composition produces the reported unrelated reply.

4. **One card per chat combines with last-writer-wins to undermine deliberate owner work.**  
   The uniqueness constraint is sensible. Allowing every subsequent proposal to supersede the live card means a slower proactive result can replace a newer dictated message. The same constraint also motivates same-turn reuse that ignores corrected arguments.

5. **Conservative outdating combines with destructive Re-draft.**  
   FR-DRF-05 intentionally outdates cards on new contact messages, including standalone dictated messages. Recovery is therefore essential. The available Re-draft path loses the task; dictated outdated cards offer copying but no equivalent task-preserving recovery.

6. **Two models provide isolation, but the current handoff sacrifices task continuity.**  
   The separate tool-less drafter does buy a real boundary: one conversation, restricted memory and no capabilities. Owner approval protects sending, but does not by itself prevent disclosure of unrelated chat data in a proposed message. Simply passing the main model’s whole history would weaken invariant 4. Preserve the isolated drafter, with a structured, provenance-backed drafting request. Any simplification should keep the same isolation and execution gates.

7. **Presentation replay prevention combines with optimistic narration to create invisible pending work.**  
   The presentation claim suppresses duplicate control messages after uncertainty. Proposal results still say awaiting approval, and unresolved presentation is not a distinct owner-facing outcome. The business-send guarantee can remain intact while adding explicit card recovery.

8. **Correct status labels cannot compensate for missing grounding.**  
   Rules 19 and 26 already prohibit unsupported draft claims, and live-card metadata is implemented. Nevertheless, the model lacks draft substance unless it reads it, lacks terminal cards in its startup snapshot, and can receive a proposal result that becomes outdated before its final response.

**4. Anything I could not verify**

- This was a read-only review. I did not modify files, run tests, Docker, migrations or the app, inspect secrets or real chat content, or contact external services.
- I could not establish which code paths caused each production incident, their frequency, deployed configuration values, or the actual production card-delivery outcomes.
- By static inspection, the main delivery safeguards are present: policy re-check in Tx A, committed `approved → executing` claim before Telegram, account-scoped targets, current-connection resolution, plain-text sending, and no automatic retry after ambiguous delivery.
- Definite reply-window refusal becomes `expired/window_closed`; 429 retries follow the separate definite-not-delivered path. This agrees with the recorded P2-O20 decision.
- I found no direct-send agent tool. Revision tokens and conditional transitions also appear to prevent approving an earlier version after a revision.
- Those conclusions are source-level assessments, not fresh runtime verification. The races and recovery gaps above need targeted synthetic regression tests before claiming they are resolved.