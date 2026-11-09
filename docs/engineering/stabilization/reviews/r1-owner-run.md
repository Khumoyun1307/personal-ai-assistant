## 1. Summary

- Reviewed `main` at `70584c7`, including the owner runtime, context, tool catalogue, LLM wrappers, control ingress, relevant tests, and evaluation reports.
- The reported drafting failures have concrete causes in the harness; they cannot reasonably be attributed to model capability alone.
- Recipient clarification breaks exact dictation, while the separate draft writer loses earlier instructions and receives only the latest clarification.
- Prompt rules actively encourage drafting when content is missing and unnecessary clarification when weak contact candidates accompany a strong match.
- Normal tainted sessions also undermine personalization: saved style instructions can be excluded from every future draft.
- Live card state **is partly supplied already**. The remaining problems concern incomplete state, missing Telegram reply references, stale snapshots, and model-written status claims.
- Additional reliability gaps affect queued turns, interruption recovery, delivery history, and enforcement of run limits.
- No tests, application processes, containers, migrations, external APIs, or production systems were run or contacted.

## 2. Findings

### R1-01 — Taint can expire while the model still receives untrusted derived text

**Severity:** high  
**Category:** safety

**Evidence:** [context.py:259](../../../../src/assistant/agent/context.py#L259) distinguishes assistant turns using only:

```python
if turn.role != "owner":
    return bool(turn.third_party)
```

However, `render_turn` at line 477 still wraps a turn when `from_tainted_run=True`. Runtime lines 641–642 persist these flags separately.

The intended consequence is explicitly demonstrated by `tests/integration/test_taint_and_consent_edges.py:89–110`: the request contains an untrusted inherited reply, but `run.tainted is False` and a configure call returns `"ok"` without confirmation.

**Failure scenario:** A contact instruction is repeated in an assistant answer, then repeated again during a later conversation. After the original directly tainted turns leave the history window, an inherited copy remains visible. A model influenced by that copy can now change settings without the confirmation barrier.

**Root cause:** Taint expiry follows the age of the original source rather than the presence of potentially contaminated content. Wrapping remains, but deterministic capability restriction disappears. Memory provenance separately recognizes this problem: `tools/memory.py:149–154` treats `from_tainted_run` as untrusted.

**Recommended fix:** Revisit the approved tradeoff. Keep untrusted derived history behind the confirmation gate, or remove it from actionable context and retain only application-generated workflow facts. Avoid solving permanent taint by allowing untrusted content and unrestricted configure tools to coexist.

**Touches:** AGENTS §6.3b and §6.7; **D56 is explicitly owner-approved**, recorded in `docs/milestones/M03.md:35`. Changing it requires an owner decision.

**Confidence:** Confirmed capability behavior by code and existing test assertions; successful exploitation would depend on model behavior.

### R1-02 — Recipient clarification makes earlier dictated text ineligible for exact-message proposals

**Severity:** high  
**Category:** correctness

**Evidence:** [replies.py:580](../../../../src/assistant/agent/tools/replies.py#L580):

```python
if turn is not None and text_occurs_in(text, turn.own_words):
    return "owner"
```

The only historical fallback searches six **assistant** replies, not earlier owner messages. On failure, lines 103–106 instruct:

> “use propose_reply with mode 'new_message'”

**Failure scenario:** The owner dictates a message, is asked which recipient, and answers with a name. The exact text remains in main-model history, but `propose_message` rejects it because it is absent from the current name-only answer. The recovery instruction redirects the model toward generating replacement text.

**Root cause:** Provenance is tied to the current turn instead of the ongoing request. There is no durable pending dictation linking source text, unresolved recipient, and subsequent clarification.

**Recommended fix:** Preserve an account-scoped pending message request with the original owner source turn and exact text. Let a clarification fill the recipient without changing text provenance. A failed provenance check should preserve the task or ask for clarification, not prescribe regeneration.

**Touches:** PRD FR-DRF-08, R-05; AGENTS §6.3b and §6.11; PF-12. Earlier owner text must retain explicit source provenance.

**Confidence:** Confirmed by reading code.

### R1-03 — The draft writer loses the instruction that a clarification completes

**Severity:** high  
**Category:** owner-UX

**Evidence:** [replies.py:396](../../../../src/assistant/agent/tools/replies.py#L396):

```python
instruction = turn.instruction if turn is not None else None
passed_guidance = None if ctx.tainted else guidance
```

`draft_context.py:194–209` supplies that instruction and optional guidance, with no earlier owner-request history. `resolve_contact` returns managed, untrusted data and therefore taints the run.

**Failure scenario:** “Write a new message asking Alex about Friday’s delivery” → “Which Alex?” → “Alex Smith.” The main model understands the full exchange, but the writer receives “Alex Smith” and the business conversation. Its synthesis of the original instruction is dropped. It therefore has little basis for producing the intended new message.

A related case is quoting a contact while writing an explicit instruction: `OwnerTurn.instruction`, `registry.py:173–179`, discards the owner’s own instruction whenever the turn also carries a quote.

**Root cause:** The main model and writer have different task contexts. The attempt to pass trusted owner wording fixed single-turn drafting but did not represent a request spanning several turns.

The result also understates this loss: `owner_message_used=True` can mean only that the clarification was passed, while the substantive instruction was omitted.

**Recommended fix:** Assemble the writer’s request from source-linked owner-authored turns belonging to the pending task. Keep quoted contact content separate and untrusted. Return facts distinguishing “latest turn supplied” from “complete request supplied.”

**Touches:** AGENTS §6.3b and §6.4; owner decision D2/PF-11, which currently specifies the **current** turn. Extending that contract needs explicit agreement.

**Confidence:** Confirmed information loss; resulting draft wording is model-dependent.

### R1-04 — Missing message content triggers composition instead of a necessary question

**Severity:** high  
**Category:** owner-UX

**Evidence:** [owner_request.v10.md:40](../../../../src/assistant/agent/prompts/owner_request.v10.md#L40):

> “an explicit request to reply, answer, draft or write to someone is always carried out”

`draft_owner.v3.md:8–10` instructs the writer to use what the conversation needs and:

> “With no request, write the reply the conversation calls for.”

The structured drafting result has no “needs clarification” outcome.

**Failure scenario:** “Write to Alex” supplies a recipient but no subject or text. The application drafts from Alex’s conversation, potentially introducing a subject or position the owner never requested.

**Root cause:** A fix for refusing explicit requests as “no reply needed” was broadened into unconditional composition. Replying to an identifiable message and composing an unspecified new message are treated alike.

**Recommended fix:** Require a sufficient content brief for a new message. Ask “What should I write?” when neither text nor purpose is available. Preserve automatic drafting for clear requests such as “answer his question” where a specific message supplies the subject.

**Touches:** PRD FR-CTL-03; AGENTS §6.11; owner decision D1. Clarify D1’s intended scope before changing behavior.

**Confidence:** Confirmed prompt and schema behavior; individual model choices remain probabilistic.

### R1-05 — Weak alternatives force ambiguity even when one contact clearly matches

**Severity:** high  
**Category:** owner-UX

**Evidence:** [read.py:570](../../../../src/assistant/conversations/read.py#L570) collects strong and weak matches together, sorts them, then returns the first `limit` entries without suppressing weak alternatives.

`owner_request.v10.md:58` says:

> “several equal candidates or any candidate with confidence 'weak'”

must trigger confirmation. The tool description repeats that condition at `tools/conversations.py:403–406`.

**Failure scenario:** One observed contact has the requested first name, while another merely shares a three-letter name prefix. The owner must choose between them. The next request can repeat the same question.

**Root cause:** “A weak candidate is not safe to select” became “any weak candidate makes the entire result ambiguous.” Furthermore, `ToolRefs` records all returned candidate IDs; it does not record which candidate the owner subsequently selected.

**Recommended fix:** Return a deterministic resolution verdict distinguishing a unique strong match, multiple plausible matches, and guesses only. Preserve the selected recipient within the pending workflow. Keep weak suggestions available when no adequate match exists.

**Touches:** PRD FR-CTL-03 and FR-DRF-08; PF-03 and rule 27. Recipient approval remains necessary under R-05.

**Confidence:** Confirmed by reading matching, tool-result, and prompt code.

### R1-06 — A confirmed style instruction can be saved successfully but never affect drafts

**Severity:** high  
**Category:** correctness

**Evidence:** [memory.py:239](../../../../src/assistant/agent/tools/memory.py#L239) obtains whole-run provenance and saves:

```text
tainted=origin.tainted,
managed=origin.tainted,
```

`run_origin` includes inherited untrusted history and confirmed held calls. Meanwhile, [items.py:504](../../../../src/assistant/memory/items.py#L504) excludes owner preferences and instructions from contact-facing context when either flag is set:

```python
& ~IS_TAINTED
& ~IS_MANAGED
```

**Failure scenario:** After discussing a contact, the owner says “Remember: keep my replies short and avoid emojis,” then confirms the card. The bot reports that it saved the instruction. Future drafts exclude it because the surrounding session was tainted.

**Root cause:** Run-level contamination is used as content provenance. Even a confirmed, owner-authored style instruction remains classified as potentially contact-derived. Owner context shows only a placeholder for such memories, and reply writers exclude them entirely.

**Recommended fix:** Introduce a narrowly defined path for saving verified owner-authored preferences from source text, with explicit applicability. Preserve existing exclusion rules for genuinely contact-derived material. Explain unusable applicability rather than reporting an apparently effective preference change.

**Touches:** PRD personalization requirements; AGENTS §6.3b, §6.4, §6.9 and §6.11; M03 D70 and the memory safety decisions.

**Confidence:** Confirmed by tracing save provenance through draft-memory selection.

### R1-07 — Telegram’s explicit reply target is discarded before the agent sees the request

**Severity:** high  
**Category:** correctness

**Evidence:** [inbox_handlers.py:229](../../../../src/assistant/telegram/control/inbox_handlers.py#L229) constructs `IncomingMessage` without the replied-to message ID. The dataclass at `handlers.py:79–103` has no corresponding field.

`third_party_text`, lines 65–81, deliberately ignores quotes of the assistant’s or owner’s own messages; those quotes are not otherwise preserved as conversational references.

**Failure scenario:** Two draft cards exist. The owner replies directly to the older card with “Make this shorter.” The bot sees those words and a general list of live cards, but loses the explicit card selection Telegram supplied. It can revise the wrong draft or ask an unnecessary question.

The same applies to “use that wording” as a reply to an older assistant suggestion outside the history window.

**Root cause:** Control ingress retains text and third-party quote provenance, but omits first-party conversational references.

**Recommended fix:** Preserve the reply-to control message ID and resolve it account-locally to its action or control turn. Supply that explicit target as application metadata. Retrieve any referenced content under its original consent and trust classification.

**Touches:** AGENTS §6.3b, §6.9 and §6.12; owner decision D3/PF-13.

**Confidence:** Confirmed by reading ingress and model types.

### R1-08 — Queued owner messages can miss an assistant answer that was just produced

**Severity:** high  
**Category:** reliability

**Evidence:** [persistence.py:44](../../../../src/assistant/agent/persistence.py#L44):

```python
statement = self._select().where(ControlTurn.id < turn_id)
```

The dispatcher stores incoming turns immediately, while `runtime.py:628` creates assistant turns only after processing finishes. The account job lock serializes execution but does not fix this ID-based history boundary.

**Failure scenario:** Owner message A starts a slow run. Owner message B arrives and receives its database ID. Run A then finishes and stores assistant reply A with a higher ID. When B runs, it excludes reply A—even though that reply has just been sent.

For example, a rapid follow-up such as “Actually, use the second option” may not see the options the preceding run generated.

**Root cause:** Database insertion order is treated as conversational execution order.

**Recommended fix:** Associate assistant replies with their triggering owner turns and construct history from completed preceding requests, excluding future owner turns. Add coverage for owner bursts arriving before earlier runs finish.

**Touches:** PRD FR-CTL-01; AGENTS §13’s owner-turn serialization requirement.

**Confidence:** Confirmed ordering defect by reading the query and persistence paths.

### R1-09 — Run limits do not bound nested tools, and the monthly budget is checked only at owner-run start

**Severity:** high  
**Category:** reliability

**Evidence:** [runtime.py:464](../../../../src/assistant/agent/runtime.py#L464) checks time and tokens only at the next loop iteration. Line 523 executes the entire tool batch first.

`ToolExecutor.execute_all`, `executor.py:131–133`, executes all returned calls without a shared deadline. Draft requests use their own 60-second timeout at `drafting.py:729–740`; their tokens do not enter `_RunState.tokens`.

The owner monthly check occurs in `_start`; `_call` does not repeat it. Its truncation retry also reuses the original timeout.

**Failure scenario:** A near-deadline response requests several drafting or summarization tools. They continue running—and may create cards—past the nominal five-minute limit. Nested calls can spend substantially more than the advertised per-run token budget. An owner run that crosses the monthly budget can continue making main-model calls.

**Root cause:** Limits belong to the outer loop rather than a shared execution budget. Consent is enforced on every call, but monetary limits are not enforced at that same boundary.

**Recommended fix:** Pass a shared deadline and usage allowance through tool services and nested model calls. Recheck before every costly call and effect. Apply a per-call monthly budget gate with appropriate reservation for concurrent work. On exhaustion, report completed partial work factually.

**Touches:** ARCHITECTURE §7.2/§7.4, NFR-10, AGENTS §8; §6.11 when partial work exists.

**Confidence:** Confirmed enforcement gaps; production latency and overspend were not measured.

### R1-10 — Interrupted-run recovery can permanently suppress an unanswered turn

**Severity:** medium  
**Category:** reliability

**Evidence:** [runtime.py:352](../../../../src/assistant/agent/runtime.py#L352):

```python
if any(run.status != "running" for run in previous):
    return RunOutcome(RunStatus.SKIPPED, note="already_answered", ...)
```

The next lines mark interrupted attempts `"failed"` before creating a replacement run.

Separately, tool execution at line 523 has no outer exception finalizer; the executor handles `NotInAccountError` and `LookupError`, but other unexpected failures escape.

**Failure scenario:** Attempt A is interrupted. Attempt B marks A failed and starts recovery, then is interrupted too. Attempt C sees failed A and returns `"already_answered"`, although neither attempt answered the owner.

An unexpected tool exception can also leave a run `"running"` without an owner-facing failure notice.

**Root cause:** “Any terminal attempt exists” is equated with “the request has been answered.” Recovery and terminal delivery are not represented separately.

**Recommended fix:** Track request completion independently from attempt outcomes. Superseded interrupted attempts must not suppress later recovery. Add an exception/cancellation finalizer that closes attempts and records factual partial effects without blindly replaying them.

**Touches:** AGENTS §6.11 and §13; ARCHITECTURE recovery guarantees.

**Confidence:** Confirmed by reading lifecycle conditions. The existing interruption test covers only one recovery attempt.

### R1-11 — Live-state support is partial; historical prose can still contradict cards and confirmations

**Severity:** medium  
**Category:** correctness

**Evidence:** [runtime.py:418](../../../../src/assistant/agent/runtime.py#L418) already loads `live_cards`. `context.py:134–156` supplies IDs, version, state, origin, and mode.

However, `actions/activity.py:271` selects only `LIVE_CARD` states. A card disappearing from that list does not explain whether it was sent, rejected, expired, or outdated.

Confirmation application sends its result directly at `confirmations.py:588–590`; it does not append a control-history event. `_reply_text`, `runtime.py:536–550`, validates citations but not action-status claims.

**Failure scenario:** An earlier assistant answer says a draft awaits approval. The owner rejects it or it becomes outdated. A later model sees the old statement and an absent card, but no explicit transition explaining what happened. Similarly, it can keep treating a confirmed setting change as pending.

**Root cause:** Working context covers live approval cards, while conversation history contains historical statements. There is no unified state receipt for referenced terminal actions and configuration confirmations.

**Recommended fix:** Supply current facts for recently referenced actions and confirmations, including terminal state and reason. Produce action-status acknowledgments from structured receipts. Require `get_draft` or activity lookup when discussing draft content or historical outcome.

**Touches:** AGENTS §6.11, RG-20, PF-15 and prompt rules 2/19/26.

**Confidence:** Confirmed state omissions; the particular production contradictions cannot be reproduced from this read-only review.

### R1-12 — The bare-phrase search rule conflicts with clarification and editing flows

**Severity:** medium  
**Category:** owner-UX

**Evidence:** [owner_request.v10.md:20](../../../../src/assistant/agent/prompts/owner_request.v10.md#L20) declares that a message consisting only of a name, number, address, phrase, or keywords:

> “is a search of the owner's chats”

and says never to treat it as something to draft or send.

Rule 18 simultaneously routes “shorter,” “N words,” and draft changes to revision, while rule 26 asks the model to use working context for follow-ups.

**Failure scenario:** After “Which Alex?” the owner answers “Alex Smith.” Or, after a length question, answers “20.” Or requests “shorter.” The standalone search rule competes directly with the active task.

**Root cause:** A retrieval fix was written as a universal interpretation rule without a continuation exception.

**Recommended fix:** Interpret pending clarification and explicit reply targets before standalone search. Scope the bare-phrase default to messages without an active conversational task. Represent the pending question explicitly rather than asking the model to resolve contradictory rules.

**Touches:** PRD FR-CTL-01/03; owner decision D109. Narrowing this approved rule requires agreement.

**Confidence:** Confirmed prompt conflict; observed routing depends on model interpretation.

### R1-13 — “Exact” dictation provenance accepts changed wording and then persists it

**Severity:** medium  
**Category:** correctness

**Evidence:** [replies.py:560](../../../../src/assistant/agent/tools/replies.py#L560) normalizes text through NFKC, case folding, quote folding, whitespace collapsing, and:

```python
folded.replace("x", "h")
```

`text_occurs_in` then performs substring matching. `propose_message_tool` persists the supplied `args.text`, rather than copying the matched source span.

**Failure scenario:** A candidate containing “Mah” can pass against owner text containing “Max”; capitalization and punctuation changes can also pass. The card can still be labelled `owner_dictated`, although the text differs.

The same substring test does not prove that a matching span is message content rather than command wording.

**Root cause:** A forgiving matching algorithm is being used as an exact-content guarantee.

**Recommended fix:** Use normalization only to locate a source span, then copy the original span verbatim. Validate task intent separately from text occurrence. Do not label model-modified wording as dictated text.

**Touches:** PRD FR-DRF-08 and R-05; AGENTS §6.11; PF-12.

**Confidence:** Confirmed by reading normalization and persistence code.

### R1-14 — Chat can change settings that it cannot reliably read back

**Severity:** medium  
**Category:** owner-UX

**Evidence:** [context.py:71](../../../../src/assistant/agent/context.py#L71) preloads only answer language and timezone as profile settings.

`StatusReport`, `observability/status.py:121–132`, contains connection, consent, autonomy, budget, actions, and notification counts, but not digest time, quiet hours, reply tone/style, or the other preference values.

`update_preferences` accepts those values, but rejects an all-null request at `tools/modes.py:359–363`.

**Failure scenario:** The owner changes digest time through the settings panel and later asks “What time is my digest?” The main model has no current read interface for that value. It must admit the limitation or rely on stale conversation text.

**Root cause:** PR #235 adds write capability without a corresponding settings read model. Panel changes and Undo events are also outside owner-run history.

**Recommended fix:** Add a content-free current-settings view to the owner context or a read tool. Use it for questions and relative changes. Do not use a mutation tool merely to inspect state.

**Touches:** PRD FR-CTL-01/02 and FR-MEM-02; AGENTS §6.11.

**Confidence:** Confirmed against the current catalogue and status schema.

### R1-15 — Undelivered assistant replies are stored as ordinary conversational history

**Severity:** medium  
**Category:** reliability

**Evidence:** [runtime.py:618](../../../../src/assistant/agent/runtime.py#L618) sends the reply, then stores a normal assistant `ControlTurn` regardless of `delivery.ok`.

It leaves the run status as the supplied status, potentially `"succeeded"`, and only adds `"reply_not_delivered"` to `error_class`. `GatewayControlChannel` supplies `Delivery.uncertain`, but `_finish` does not preserve that distinction in the turn.

**Failure scenario:** Telegram definitely refuses a reply containing suggestions. The next owner request receives those unseen suggestions as ordinary assistant history. It can refer to something the owner never received. A transient database failure after the send introduces a separate replay/duplication window.

**Root cause:** Generated content, request completion, and delivered conversation state are conflated.

**Recommended fix:** Record delivery outcome explicitly. Mark definitely unshown assistant text as such or exclude it from ordinary dialogue history. Preserve uncertain delivery without automatic resend, and distinguish completed reasoning from delivered response.

**Touches:** AGENTS §6.11; control-message delivery accounting and at-most-once expectations.

**Confidence:** Confirmed; `test_agent_runtime.py:486–496` checks only the error field.

### R1-16 — Tests validate prescribed tool sequences more than realistic multi-turn behavior

**Severity:** medium  
**Category:** maintainability

**Evidence:** [p2_drive.py:212](../../../../tests/scenarios/p2_drive.py#L212) implements owner scenarios by queuing an explicitly chosen tool and a fixed final response:

```text
tool_response(("qa-call", tool, args(**values))),
text_response("P2 synthetic owner-run completion."),
```

`test_owner_drafting.py:110–115` similarly scripts resolve → propose → acknowledgment. Prompt tests mainly assert required wording.

The paid drafting harness seeds history and performs one current `runtime.run` per case at `evals/owner_drafting.py:921–922`. Its current-version follow-up report covers four selected cases, not a complete multi-turn interaction.

**Failure scenario:** Tests pass because the test author supplies the intended tool, target, and response. They do not expose whether the model searches after a name clarification, loses earlier dictation, repeatedly resolves a chosen recipient, or makes a false statement about a changed card.

**Root cause:** Strong plumbing coverage is being treated as broader evidence of conversational competence. Seeded history does not exercise the process that generates, persists, and interprets successive turns.

**Recommended fix:** Add deterministic regressions for the state/provenance defects above. Separately add synthetic, actual multi-turn behavioral evaluations with owner-approved API spend: dictation → recipient clarification, missing content, two simultaneous cards, reply-to-card edits, queued owner bursts, and card transitions between turns. Measure unnecessary questions, instruction preservation, wrong-card selection, and truthful outcome reporting.

The reports should distinguish historical results: the full medium-effort run used `draft_owner.v2` and passed 27/31 deterministic cases; the `v3` follow-up passed its four selected cases. Neither establishes industrial readiness.

**Touches:** AGENTS §8 and §11; owner decision D10; evaluation claims and release acceptance.

**Confidence:** Confirmed by reading helpers, tests, evaluation harness, and recorded reports.

## 3. Rule interactions

| Individually reasonable rules | Combined behavior |
|---|---|
| Resolve the recipient first; contact names taint the run; tainted guidance is dropped | The normal recipient-resolution path removes the main model’s explanation of the task before drafting. |
| Exact dictation must have provenance; provenance checks the current turn; clarify ambiguous recipients | The clarification needed for recipient safety destroys eligibility for exact dictation. |
| Explicit writing requests must be fulfilled; the writer always drafts; missing details come from the conversation | “Write to X” becomes an invented reply instead of a content question. |
| Weak matches require confirmation; return useful weak alternatives alongside strong matches | A unique strong match becomes an unnecessary choice between a real match and a guess. |
| Bare names/numbers/phrases mean search; follow-ups use recent context | Answers to clarification questions compete with an unconditional search instruction. |
| Tainted configure calls require confirmation; confirmed memories retain taint; drafts exclude tainted owner preferences | A successfully confirmed style instruction can have no effect on drafting. |
| Taint must decay; inherited assistant replies remain wrapped and visible | Untrusted text can remain after the deterministic confirmation gate has switched off. |
| Content-free live cards; historical prose retained; button outcomes are not chat turns | The model knows which cards remain live but may not know what happened to a missing card or confirmed change. |
| Serialize owner jobs; history contains only IDs below the current owner turn | Execution order is correct while conversational history is incomplete during rapid input. |
| Bound outer iterations/time/tokens; tools run separate model calls | A bounded main loop does not provide a bounded end-to-end request. |

The catalogue contains **26 tools**, all restricted to `owner_request`, each with its own release flag and the default `CONTROL_ONLY` tier:

- **11 read tools:** status, message search, contact resolution, conversation/list views, summaries, attention, activity, memory list, monitor list, and draft read.
- **10 configure tools:** memory save/forget, preferences, conversation mode, three deletion tools, contact priority, monitor create/stop.
- **3 propose tools:** reply composition, exact-message proposal, and draft revision.
- **2 control tools:** pause and resume.

Deletion and monitor creation always require confirmation; retention changes conditionally require it; tainted configure/control calls require it except pause; resume is always card-only. Proposal and revision tools rely on approval cards. I found no direct-send tool in this catalogue.

The overlap itself is manageable. The larger problem is that the 28-rule prompt carries task routing, conversational state interpretation, recovery advice, safety instructions, and presentation rules together. The v9→v10 diff adds several useful incident fixes, but preserves older unconditional rules that conflict with those additions.

## 4. Anything I could not verify

- No production conversation, configuration, database row, log, secret, backup, or dump was inspected. Production deployment and model settings are taken from the brief.
- No finding was reproduced by executing code. “Confirmed” means the stated branch or information loss follows from source inspection, sometimes with an existing test explicitly asserting it.
- Actual latency, spend, unnecessary-question frequency, and model adherence to conflicting rules remain unmeasured.
- The exact production sequence behind incident 4 cannot be established. The current tree already includes PF-15 live-card metadata and `get_draft`; “no live state is supplied” would be an inaccurate diagnosis.
- Recorded evaluations provide useful synthetic evidence, but no reviewed report establishes the multi-turn behaviors identified here.
- Telegram and OpenAI behavior was not externally rechecked; this review makes no new platform-verification claim.
- The business execution pipeline was followed only as needed to understand owner tools and cards. This is not a complete audit of sending, scheduling, policy, or tenant isolation.