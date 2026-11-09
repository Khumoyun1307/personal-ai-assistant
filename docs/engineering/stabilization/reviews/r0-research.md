## 1. Summary

- Reviewed `main` at `70584c7`, read-only. No tests, application runs, production access, or repository changes.
- The principal failures are explainable by the harness: owner intent survives in conversation history but is discarded at the boundary to drafting and exact-text proposals.
- Clarification is treated as another independent request rather than continuation of a persistent messaging task.
- Taint controls correctly restrict authority, but also suppress legitimate owner instructions before the isolated writer receives them.
- Prompts then require drafting despite missing information, encouraging plausible replies that do not express the owner’s request.
- Contact resolution turns weak alternatives into unnecessary ambiguity.
- The preliminary card-state diagnosis needs correction: live state is injected, but terminal transitions and draft content are incompletely represented.
- Existing evaluations exercise real components, yet structurally exclude the central cross-turn dictation failure.
- Recommended target: one conversational planner, durable request state, narrow tool-less workers, and the existing deterministic approval/send boundary.

## 2. Findings

### R0-01 — Clarification breaks the provenance of dictated messages

**Severity:** high  
**Category:** correctness / owner-UX

**Evidence**

- [replies.py:580](../../../../src/assistant/agent/tools/replies.py#L580): `_provenance` recognizes owner text only from the current turn:

  ```python
  if turn is not None and text_occurs_in(text, turn.own_words):
      return "owner"
  ```

- The fallback searches six earlier **assistant** replies, not earlier owner turns.
- [replies.py:103](../../../../src/assistant/agent/tools/replies.py#L103) directs rejected text toward composition:

  > “to write a message from what the owner asked, use propose_reply with mode 'new_message'”

- [runtime.py:449](../../../../src/assistant/agent/runtime.py#L449) gives tools an `OwnerTurn` containing only the current turn.

**Concrete failure scenario**

1. Owner: “Tell Alex: The appointment is cancelled.”
2. Assistant asks which Alex.
3. Owner identifies the recipient.
4. The planner still sees the original dictation in history, but proposing that exact text fails provenance validation because the current turn contains only the identification.

If an earlier assistant reply happened to repeat the text, it can instead pass as an **assistant suggestion**, changing its origin.

**Root cause**

The provenance unit is a turn; the user’s task spans turns. There is no authoritative request object binding original text, recipient clarification, and eventual proposal.

**Recommended fix**

Persist a messaging request with source-turn references and exact text spans. Recipient clarification should complete that request. `propose_message` should reference the verified source span rather than regenerate its text.

Preserve the distinction between an owner dictation and an assistant suggestion. Do not solve this by accepting arbitrary text anywhere in history.

**Touches:** PRD FR-DRF-08/R-05; PF-12/D4; §6 invariants 3b, 4 and 12 when implementing provenance and scope.  
**Confidence:** confirmed by reading code; exact production trajectory not independently reproduced.

---

### R0-02 — The isolated writer loses legitimate owner instructions

**Severity:** high  
**Category:** owner-UX / correctness

**Evidence**

- [replies.py:395](../../../../src/assistant/agent/tools/replies.py#L395):

  ```python
  instruction = turn.instruction if turn is not None else None
  passed_guidance = None if ctx.tainted else guidance
  ```

- [registry.py:173](../../../../src/assistant/agent/tools/registry.py#L173):

  ```python
  if self.third_party or self.quoted or self.contains_managed:
      return None
  ```

- [context.py:117](../../../../src/assistant/agent/context.py#L117):

  ```python
  tainted = direct or any(carries_third_party(t) for t in history)
  ```

- [draft_context.py:194](../../../../src/assistant/agent/draft_context.py#L194) adds `inputs.instruction`; no earlier owner-request turns are supplied.
- [drafts.py:423](../../../../src/assistant/agent/tools/drafts.py#L423) rejects a non-append revision when `turn.instruction is None`.

**Concrete failure scenario**

After dictation and recipient clarification, the writer receives “just write it,” the recipient’s chat, and no original dictation. The planner’s reconstructed guidance is dropped because resolving the contact already tainted the run.

Another case: the owner quotes a contact’s question and adds “decline politely and suggest Monday.” The separately stored owner words exist, but `OwnerTurn.instruction` rejects the entire mixed turn.

**Root cause**

Three different concepts are conflated:

1. Whether a run has encountered untrusted material.
2. Which specific text was authored by the owner.
3. Which content the writer may receive under isolation rules.

Run-level taint is being used to discard instructions, rather than only to restrict authority.

**Recommended fix**

Give the writer verified owner-authored instruction spans from the active request, including relevant clarification turns. Keep quotes and contact text in separate untrusted fields.

Continue rejecting arbitrary planner-generated guidance from a tainted context. The replacement should be source-backed intent, not a trusted paraphrase of the entire planner context.

**Touches:** R2/E3; D2/PF-11; §6 invariants 3b, 4 and 9. D2 explicitly requires amendment for historical and mixed quoted turns.

**Forward risk:** [P3 decisions:330](../../../../docs/engineering/p3/decisions.md#L330), P3-T132, explicitly accepts tainted follow-ups receiving no nudge guidance, producing generic or “not needed” nudges. The same usability failure is already planned for another workflow.

**Confidence:** confirmed by reading code and decisions.

---

### R0-03 — Missing message content is converted into an instruction to invent a draft

**Severity:** high  
**Category:** owner-UX / correctness

**Evidence**

- [owner_request.v10.md:40](../../../../src/assistant/agent/prompts/owner_request.v10.md#L40):

  > “an explicit request to reply, answer, draft or write to someone is always carried out”

- [draft_owner.v3.md:8](../../../../src/assistant/agent/prompts/draft_owner.v3.md#L8):

  > “Where the request is silent, use … what the conversation needs. With no request, write the reply the conversation calls for.”

- [replies.py:146](../../../../src/assistant/agent/tools/replies.py#L146) provides modes, guidance and word bounds, but no explicit `needs_input` outcome or content-completeness contract.

**Concrete failure scenario**

“Write to Alex” identifies an operation and possibly a recipient, but gives no topic or text. Instead of asking “What should I write?”, the system drafts from Alex’s recent conversation.

The same fallback turns “write a NEW message” into a new standalone message whose substance still comes from the old chat.

**Root cause**

The fix for an inappropriate “no reply needed” veto overcorrected. It removed the distinction between:

- refusing a sufficiently specified owner request; and
- asking for information needed to fulfill an underspecified request.

**Recommended fix**

Add a first-class clarification result and persist the missing slot. Distinguish:

- exact dictated text;
- compose from an owner-supplied topic/instruction;
- reply to a specified or contextually clear message;
- missing content.

“No content supplied” should normally ask what to write. An explicit “draft an appropriate reply to his last message” supplies enough intent.

**Touches:** D1/PF-10; FR-CTL-03; FR-DRF-01. Clarify D1 without restoring the “no reply needed” veto.  
**Confidence:** confirmed prompt behavior; individual model outputs remain nondeterministic.

---

### R0-04 — Weak alternatives force clarification even when a strong match exists

**Severity:** high  
**Category:** owner-UX / correctness

**Evidence**

- [read.py:570](../../../../src/assistant/conversations/read.py#L570) accumulates matching contacts across strong and weak match kinds, then returns the ranked list:

  ```python
  found.sort(key=_ranking)
  return found[: max(1, limit)]
  ```

- [conversations.py:374](../../../../src/assistant/agent/tools/conversations.py#L374) labels individual candidates `weak` or `match`.
- [owner_request.v10.md:58](../../../../src/assistant/agent/prompts/owner_request.v10.md#L58):

  > “When `resolve_contact` returns several equal candidates or any candidate with `confidence` ‘weak’, list them and ask…”

- [conversations.py:391](../../../../src/assistant/agent/tools/conversations.py#L391) records references to **all** candidates; those references do not identify the owner’s selected recipient.

**Concrete failure scenario**

A first name strongly matches one observed chat partner. An unrelated longer name qualifies as a weak short-form candidate. The prompt requires asking about both.

After the owner chooses, a later request resolves again. History contains candidate references and prose, but no authoritative binding of the clarification to the original request.

**Root cause**

Candidate discovery and recipient selection share one undifferentiated list. A safety rule intended to prevent selecting weak matches treats their mere presence as ambiguity.

**Recommended fix**

Return a deterministic resolution classification, such as:

- unique strong match;
- genuinely ambiguous strong matches;
- weak suggestions only;
- no match.

Do not let weaker alternatives veto a unique sufficiently strong result. Define strong-match ambiguity carefully, including full-token names, rather than treating rank alone as identity certainty.

Retain the owner’s selection for the active request. Persistent aliases should require explicit owner intent; do not silently create permanent memory.

**Touches:** D8/PF-03; FR-CTL-03; FR-DRF-08; §6 invariant 7.  
**Confidence:** confirmed by reading code and prompt.

---

### R0-05 — Live card context exists, but does not fully ground conversational claims

**Severity:** high  
**Category:** correctness / owner-UX

**Evidence**

- [runtime.py:418](../../../../src/assistant/agent/runtime.py#L418) loads live cards at run start.
- [context.py:143](../../../../src/assistant/agent/context.py#L143) injects action ID, conversation ID, version, state, origin and mode.
- [activity.py:271](../../../../src/assistant/actions/activity.py#L271) includes only `LIVE_CARD` states.
- [replies.py:316](../../../../src/assistant/agent/tools/replies.py#L316) returns proposal metadata, not the generated draft text.
- [runtime.py:536](../../../../src/assistant/agent/runtime.py#L536) postprocesses citations and chat-access notices; it does not validate action-status or draft-content claims.
- [drafts.py:545](../../../../src/assistant/agent/tools/drafts.py#L545) provides the needed state and text through `get_draft`, when the planner calls it.

**Concrete failure scenario**

A draft becomes outdated between turns. The next context says there are no live cards, but does not supply the transition explaining what happened to the previously discussed action. Earlier assistant text still describes a pending draft.

Alternatively, the planner reports that its requested topic is in a card without reading the separate writer’s actual output. The card contains different content.

**Root cause**

There is a partial application-state snapshot, not a complete account of the active task and its artifact. State is captured once at run start; content verification depends on optional tool selection and prompt obedience.

**Recommended fix**

Inject the active request’s relevant action state, including recent terminal transitions and supersession links. Require a fresh artifact read before answering questions about its content.

Render consequential status statements from application receipts. Keep snapshot versions and re-read before reporting a state that can change during a long run.

**Touches:** PF-15, D3; FR-CTL-06; §6 invariant 11.  
**Confidence:** confirmed architectural gap; the reported contradictions are consistent with it but were not reproduced.

**Correction to preliminary diagnosis:** “The model is not told live card state between turns” is false for this tree.

---

### R0-06 — The drafting eval cannot express the central cross-turn dictation requirement

**Severity:** high  
**Category:** reliability / maintainability

**Evidence**

- [owner_drafting.py:269](../../../../evals/owner_drafting.py#L269) models a case as seeded history plus one evaluated `turn`.
- [owner_drafting.py:332](../../../../evals/owner_drafting.py#L332):

  ```python
  if card.origin == "owner_dictated" and not _occurs(card.exact_text, self.turn):
      raise ValueError("a dictated exact_text must be in the owner's message")
  ```

- [owner_drafting.py:922](../../../../evals/owner_drafting.py#L922) invokes the runtime for that evaluated turn.
- [medium eval:30](../../../../docs/evals/2026-10-30-owner-drafting-effort-medium-gpt-6-luna.md#L30) reports 27/31 deterministic passes, informational, with no owner-confirmed items.
- [eval README:119](../../../../docs/evals/README.md#L119) records the later four-case `draft_owner.v3` confirmation run.

**Concrete failure scenario**

A reviewer attempts to add:

> earlier owner turn contains dictation → current turn clarifies recipient → expect an owner-dictated card with the earlier text.

The dataset rejects the case before the model runs.

**Root cause**

The evaluator encodes the implementation restriction as a validity rule. Seeded history can test some follow-ups, but it does not test the assistant’s actual clarification question followed by a real continuation.

**Recommended fix**

Add executable multi-turn scenarios with persistent state and simulated card/message events. Grade the resulting action rows, exact text, recipient, versions and conversational claims.

Keep deterministic safety checks separate from linguistic judges. Cover clarification, interruption, topic switches, stale cards, revocation, and failures. Re-run the full affected set after prompt/schema changes; targeted confirmation runs are useful but establish narrower evidence.

**Touches:** AGENTS §8/§11; ADR-16; milestone quality gates.  
**Confidence:** confirmed by reading evaluator and reports.

---

### R0-07 — “Exact text” provenance permits changed wording

**Severity:** medium  
**Category:** correctness

**Evidence**

- [replies.py:560](../../../../src/assistant/agent/tools/replies.py#L560) normalizes case, quotes, whitespace and `x`/`h`.
- [replies.py:573](../../../../src/assistant/agent/tools/replies.py#L573):

  ```python
  return bool(needle) and needle in normalise_for_match(source)
  ```

- [replies.py:639](../../../../src/assistant/agent/tools/replies.py#L639) stages `args.text`, rather than recovering the original source substring.
- [drafts.py:420](../../../../src/assistant/agent/tools/drafts.py#L420) uses the same check for literal appends.

**Concrete failure scenario**

The owner types `Xabar`. The model supplies `habar`. The matching normalization can accept it, and the changed string is placed on an “owner dictated” card.

Case, punctuation and whitespace changes can also pass even where those differences matter to the owner.

**Root cause**

A tolerant lookup comparator is being used as proof of verbatim authorship.

**Recommended fix**

Use normalization only to locate a potential span. Recover and stage the original characters from the verified source. Prefer source-turn ID plus offsets, validated by deterministic code.

**Touches:** R-05/FR-DRF-08; PF-12/D4; literal append contract.  
**Confidence:** confirmed by reading code.

---

### R0-08 — Nested writer calls escape the advertised owner-run budget

**Severity:** medium  
**Category:** reliability / cost

**Evidence**

- [runtime.py:461](../../../../src/assistant/agent/runtime.py#L461) creates the owner-run deadline.
- [runtime.py:523](../../../../src/assistant/agent/runtime.py#L523) awaits tools without passing their remaining deadline or token allowance.
- [runtime.py:558](../../../../src/assistant/agent/runtime.py#L558) counts usage from the owner model’s calls.
- [drafting.py:729](../../../../src/assistant/agent/drafting.py#L729) creates separate tool-less requests with the writer’s own timeout.
- [drafting.py:647](../../../../src/assistant/agent/drafting.py#L647) can retry for word bounds; `_parse_owner` can also retry truncation.
- [replies.py:455](../../../../src/assistant/agent/tools/replies.py#L455) can repeat drafting after a stale result.

**Concrete failure scenario**

Near the five-minute owner-run deadline, a proposal starts a writer call. Writer retries and a stale-chat retry continue beyond the remaining owner deadline. Their tokens are metered globally but are not included in `state.tokens`.

**Root cause**

Budgets belong to individual runtime components rather than the complete user operation.

**Recommended fix**

Pass a shared operation budget to nested calls: deadline, remaining tokens/cost, and cancellation. Allocate retries from that budget. Report staged cards accurately if the outer conversational response fails afterward.

**Touches:** AGENTS §8 bounded iterations/tokens/wall time; ARCHITECTURE §7.2.  
**Confidence:** confirmed by reading code; overrun magnitude not measured.

---

### R0-09 — Promoting a route to a stronger model can make its budget cost disappear

**Severity:** medium  
**Category:** cost / ops

**Evidence**

- [metering.py:28](../../../../src/assistant/llm/metering.py#L28) prices only `gpt-6-luna`.
- [metering.py:49](../../../../src/assistant/llm/metering.py#L49) returns `None` for other models.
- [persistence.py:99](../../../../src/assistant/agent/persistence.py#L99) sums `LlmCall.cost_estimate`; an all-null result becomes zero.
- [config.py:206](../../../../src/assistant/config.py#L206) checks known processor mapping, not price coverage.
- [model_routes.py:21](../../../../src/assistant/core/model_routes.py#L21) recognizes model families by prefixes.

**Concrete failure scenario**

An approved route changes to a stronger `gpt-*` model. Consent checks pass and usage rows are recorded, but their cost estimates are null. Monthly budget calculations omit that spend.

**Root cause**

Model routing is extensible; the pricing registry and budget admission checks are not kept in step with that extensibility.

**Recommended fix**

Require a verified price entry for every enabled production model before route promotion. Unknown price must produce an explicit operational failure or conservative reservation, not zero effective spend. Add operation-level reservations for concurrent and nested calls.

**Touches:** ADR-07a; AGENTS §8 model-choice decisions; NFR-10 budget checks.  
**Confidence:** confirmed conditional defect. The supplied production configuration uses Luna, so this is not evidence of current uncounted production spend.

## 3. Rule interactions

| Individually reasonable rules | Combined behavior | Required correction |
|---|---|---|
| Resolve before proposing; show weak guesses; never choose weak candidates | One strong match plus a weak alternative repeatedly asks for clarification | Separate real ambiguity from optional suggestions |
| Exact-text provenance; only current-turn owner text accepted | Asking a necessary question destroys dictation provenance | Preserve source-backed requests across turns |
| Contact names taint; tainted guidance is dropped; writer sees only current owner turn | Normal recipient resolution disables the planner’s contextual handoff | Pass verified owner instruction spans, not unrestricted planner prose |
| Explicit drafting must not be vetoed; missing instructions fall back to conversation needs | Underspecified requests produce invented reply-like messages | Add `needs_input` without restoring the “no reply needed” veto |
| A bare name is a search; no persistent clarification task | A recipient answer can be reinterpreted as an unrelated search | Give pending clarification context precedence |
| One live card per chat; proposals replace cards; main model does not see generated text | Repeated attempts can replace cards while chat descriptions refer to earlier content | Bind reporting and revisions to action IDs and versions |
| Live-card injection excludes terminal cards; history preserves previous assistant claims | An outdated card vanishes from the snapshot without an explanatory event | Include relevant terminal transitions and fresh receipts |
| Taint decays after its source leaves the history window | Authority restrictions depend on a coarse window rather than particular values | Preserve conservative mutation gates; improve field-level provenance separately |
| Route upgrades allowed; only Luna priced | A quality improvement can weaken cost accounting | Make pricing coverage part of model admission |
| P3 follow-ups inherit taint; tainted nudge guidance is discarded | The planned workflow knowingly produces generic nudges | Resolve the intent-transfer contract before extending it |

## 4. Part A — Production harness patterns, 2025–2026

### A1. Single agent, handoffs, and workers

There is no general result that more agents are better. Anthropic recommends beginning with simple compositions and adding orchestration when the task warrants its cost and complexity. Its orchestrator-workers pattern addresses tasks whose subtasks are discovered dynamically. That is different from inserting another model into every short interaction. [Building effective agents](https://www.anthropic.com/research/building-effective-agents)

OpenAI distinguishes two ownership patterns:

| Pattern | Conversational ownership | Suitable use |
|---|---|---|
| Handoff | A specialist takes over the conversation | A branch needing different instructions, tools or policy |
| Agents as tools | The manager keeps responsibility for the answer | Bounded classification, summarization or other specialist work |

These are orchestration contracts, not different forms of shared consciousness. [OpenAI orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration)

Context sharing must be explicit. SDK handoffs normally expose previous conversation history, subject to filtering. By contrast, a nested `Agent.as_tool()` run does **not** automatically inherit its parent’s conversation state. [SDK handoffs](https://openai.github.io/openai-agents-python/handoffs/), [SDK tools](https://openai.github.io/openai-agents-python/tools/)

Shared application state is another separate concept: the SDK’s local context object is not automatically sent to the model. A database row, Python object or common model name does not make information visible in inference. [SDK context management](https://openai.github.io/openai-agents-python/context/)

Anthropic reports benefits from parallel agents for broad research, but also coordination limitations and substantially greater token use. Those measurements concern its research system, not a general benchmark proving superiority for messaging assistants. [Multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system)

**Application here:** one conversational owner is appropriate. Isolated readers/writers are justified by data boundaries, provided their input contract preserves the complete relevant owner request.

### A2. Context, memory, and multi-turn coherence

API requests do not implicitly remember earlier requests. Multi-turn behavior requires supplying prior messages or using an explicit conversation-state mechanism. [OpenAI conversation state](https://developers.openai.com/api/docs/guides/conversation-state)

A useful distinction is:

- **Conversation history:** what was said.
- **Working state:** what task is active, what is missing, which artifact is being discussed.
- **Long-term memory:** durable preferences or facts.
- **Live domain state:** what is currently true of cards, actions, consent and jobs.

LangGraph documents thread-scoped persisted state, read at each step, separately from long-term memory across threads. [Memory overview](https://docs.langchain.com/oss/python/concepts/memory)

Compaction and structured notes can preserve relevant context while reducing stale material. Compaction is lossy, so important unresolved requirements must be deliberately retained. Anthropic describes both techniques and warns about losing subtle details. [Effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)

Durable artifacts are useful even with compaction: Anthropic’s long-running harness work explicitly addresses sessions that otherwise start without enough knowledge of earlier progress. [Effective harnesses](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)

**Application here:** “him,” “that message,” and answers to clarification questions should resolve against an active request and artifact references. Exact dictation should remain an immutable source reference; it should never depend on a generated conversation summary.

**Verification limit:** public documentation does not establish ChatGPT’s or Claude chat’s complete internal memory, routing, or referent-resolution implementation. Their observed coherence is not evidence that using the same model family reproduces their harness.

### A3. Tools and approvals

Tool quality includes names, descriptions, arguments, output information, and actionable errors. Anthropic specifically recommends meaningful results and errors that explain how the agent can correct a call. [Writing effective tools](https://www.anthropic.com/research/writing-tools-for-agents)

Strict schemas address argument structure; they do not establish that the chosen recipient or message matches user intent. OpenAI advises evaluating different tool-set sizes and gives fewer than twenty initially available functions as a **soft suggestion**, not a correctness threshold. [Function calling](https://developers.openai.com/api/docs/guides/function-calling)

Persisted interrupts support asking for missing information, review, editing and approval. Resumption still requires careful side-effect handling: approval does not itself make an operation idempotent. [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

**Application here:**

- Make `needs_input` an explicit workflow outcome.
- Return machine-readable recovery choices: clarify recipient, supply content, refresh outdated artifact.
- Preserve the existing application-side validation and durable send claim.
- Consider smaller task-specific tool surfaces. The default registry contains 26 definitions; actual exposure depends on flags and consent. Its effect on accuracy is a hypothesis to evaluate, not a proven cause of these incidents.
- Do not replace approvals with a model saying the owner approved.

### A4. Prompt-injection defenses and their usability costs

| Pattern | Security mechanism | Limitation or usability cost |
|---|---|---|
| Dual LLM | A privileged planner avoids reading untrusted text; a quarantined model processes it, with results handled through references or constrained channels | Context transfer becomes harder; letting free-form worker results back into the privileged context defeats the intended separation |
| CaMeL | Separates trusted control flow from untrusted data and enforces capability/data-flow policies at runtime | Requires explicit policy and execution machinery; it is not merely a taint flag in a prompt |
| Action selector | Maps trusted requests to predefined actions without feeding untrusted action results back into selection | Restricted flexibility and weaker adaptive exploration |
| Plan then execute | Fixes the action plan before reading untrusted results | Less adaptive; a fixed plan can still carry attacker-influenced content in permitted arguments |
| Human approval | A person reviews the concrete effect before execution | Review burden and mistaken approval remain possible |

Sources: [Willison’s dual-LLM proposal](https://simonwillison.net/2023/Apr/25/dual-llm-pattern/), [CaMeL paper](https://arxiv.org/abs/2503.18813), [security design-patterns paper](https://arxiv.org/html/2506.08837v1).

The CaMeL paper reports a utility/security trade-off on AgentDojo; its guarantees are scoped to its design and evaluation, not every arbitrary agent application. [CaMeL](https://arxiv.org/abs/2503.18813)

A relevant October 2026 **preprint** reports that separately protected agents can still lose provenance at agent-to-agent boundaries. This reinforces the need to preserve trust labels through handoffs; it is not independent validation of this repository. [Can CaMeLs Talk?](https://arxiv.org/abs/2610.05640)

OpenAI’s guidance likewise treats structured boundaries and approvals as mitigations while acknowledging that agents can still be tricked. [Agent safety](https://developers.openai.com/api/docs/guides/agent-builder-safety)

**What is enough for this product?**

My recommendation is to treat explicit, version-bound approval of the exact recipient and text as the authority to send an owner-requested message, while retaining deterministic consent, isolation and policy checks.

That does **not** establish that:

- the proposed content is accurate;
- the owner understood an injected instruction;
- settings or memory changes are authorized;
- cross-conversation content sharing is permitted;
- the same architecture is safe for autonomy.

The repository’s owner planner reads untrusted text and continues selecting tools. It therefore is **not** a strict dual-LLM or CaMeL implementation. Its principal protection is restricted capabilities plus application confirmation and execution checks.

### A5. Evaluation and observability

Conversational-agent evaluation should combine verifiable end states with interaction quality: whether the operation succeeded, whether clarification was appropriate, and whether the dialogue remained coherent. Anthropic describes multi-turn user simulation, state checks, transcript constraints and rubric grading. [Demystifying evals](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)

LLM judges should be calibrated against human judgments rather than treated as truth. Deterministic checks remain appropriate for exact text, recipients, approval and execution state. [OpenAI evaluation guidance](https://developers.openai.com/api/docs/guides/evaluation-best-practices)

Trajectory analysis helps locate failures in tool choice, orchestration and reporting rather than merely grading the final sentence. [Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)

The Agents SDK can trace model calls, tools, handoffs and guardrails. This is a capability to adapt carefully, not a recommendation to export this repository’s production content. [SDK observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability)

**Application here:** retain content-free production telemetry linking request, parent/child runs, source-turn IDs, action IDs, versions, missing-slot enums, instruction source and transitions. Use full inspectable trajectories only for synthetic evaluations. Do not log real prompts, tool text, or reasoning.

### A6. Model choice and routing

Current OpenAI guidance positions Luna for scoped tasks, triage and frequent automation, and stronger models for more complex work. That supports workload-specific evaluation rather than assuming one route configuration suits all tasks. [Model selection](https://developers.openai.com/api/docs/guides/model-selection)

Official pages currently list `gpt-6-luna` at $0.10/$0.50 and `gpt-6.1-sol` at $2/$10 per million uncached input/output tokens respectively. The latter supports Responses API tool calling and medium reasoning effort. These prices do not establish this application’s task quality or account availability. [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)

**Application here:** evaluate a stronger conversational planner first. A larger model cannot recover text the application never passes to the writer.

## 5. Part B — Harness as built

The active prompt registry selects:

| Work | Active prompt | Execution |
|---|---|---|
| Owner conversation | `owner_request.v10` | Bounded tool loop |
| Owner-requested drafting/revision | `draft_owner.v3` | Tool-less structured call, under `draft_reply` route |
| Proactive Assist drafting | `draft_reply.v1` | Tool-less structured call |
| Summaries | `summarize.v7` | Tool-less, per conversation |
| Triage | `triage.v1` | Tool-less structured call |
| Semantic monitors | `evaluate_monitor.v1` | Tool-less structured call |

Evidence: [prompt_registry.py:58](../../../../src/assistant/agent/prompt_registry.py#L58), [drafting.py:452](../../../../src/assistant/agent/drafting.py#L452).

### Exact-text request: “write to X: \<text\>”

```text
Owner control turn
  |
  v
OwnerRequestRuntime
  context:
    owner profile + permitted owner memories
    up to 20 earlier control turns
    application refs on history
    live approval-card metadata
    current owner turn
  |
  v
LLM complete: owner_request.v10
  |
  +--> resolve_contact(X)
  |      deterministic directory matching
  |      returns wrapped names + candidates
  |      taints the owner run
  |
  v
LLM complete: chooses operation
  |
  +--> propose_message(exact text)
  |      validates current-turn / assistant-suggestion provenance
  |      creates policy-checked action
  |      stages approval card
  |      NO writer-model call
  |
  v
LLM complete: reports tool status
  |
  v
Owner approves the exact card version
  |
  v
actions.execute: fresh policy check + committed claim
  |
  v
OutboundGateway -> Telegram -> recorded outcome
```

This is commonly three planner calls when contact resolution is needed, without retries or extra reads. It can use fewer calls when a valid target ID is already known.

### Composed or reply request

```text
Same owner planner
  |
  +--> propose_reply(mode, target, guidance, bounds)
          |
          +--> application extracts CURRENT OwnerTurn.instruction
          +--> drops model guidance if the owner run is tainted
          |
          v
        LLM parse: draft_owner.v3
          sees:
            one target conversation's recent retained messages
            that conversation's running summary
            permitted contact/owner notes and reply preferences
            mode and optional answered-message ID
            CURRENT usable owner instruction
            guidance only if allowed
          does NOT see:
            the planner's full conversation history
            the original request merely because it remains in that history
          has NO tools
          |
          v
        validated structured draft
          |
          v
        action + approval card
          |
          v
        factual metadata returned to owner planner
```

Evidence: [runtime.py:402](../../../../src/assistant/agent/runtime.py#L402), [replies.py:443](../../../../src/assistant/agent/tools/replies.py#L443), [draft_context.py:156](../../../../src/assistant/agent/draft_context.py#L156).

Within an owner run, provider output is appended unchanged and tool results are returned before the next call. Across runs, context is rebuilt from stored control turns. The adapter uses `store=False`; consent and metering wrap `LLMClient`. These are useful foundations, not reasons to replace the owned loop. [runtime.py:519](../../../../src/assistant/agent/runtime.py#L519), [openai_adapter.py:103](../../../../src/assistant/llm/openai_adapter.py#L103), [consent.py:54](../../../../src/assistant/llm/consent.py#L54)

## 6. Recommended target harness

### 6.1 Architecture

Retain one conversational planner and the current modular monolith. Introduce an explicit request/workflow state contract.

```text
Owner turn
  |
  v
Application loads:
  active request + verified owner source spans
  selected recipient / pending clarification
  relevant action versions and current states
  permitted recent history and memories
  |
  v
ONE conversational planner
  |
  +--> needs_input(question, missing_slot, request_id)
  |       persist continuation; ask owner
  |
  +--> retrieve scoped information
  |       results remain untrusted; taint restrictions remain
  |
  +--> prepare exact message(source reference)
  |       application copies original characters
  |       no writer model
  |
  +--> prepare composed message(request_id, target)
          application builds a scoped instruction packet
          |
          v
        optional tool-less writer
          verified owner instruction spans
          one conversation's permitted context
          no other conversations or full planner transcript
          |
          v
        validated draft artifact
  |
  v
Application stages versioned approval card
  |
  v
Application-grounded conversational receipt
  |
  v
Owner approves -> existing actions.execute + policy + claim + gateway
```

This is a proposed design, not a request to implement P3 or alter approved scope.

### 6.2 Persistent request state

Represent at least:

- request ID and account ID;
- operation: dictate, compose, reply, revise, advise;
- source owner-turn IDs and validated spans;
- selected conversation/contact;
- whether recipient selection is confirmed;
- missing slot and pending question;
- answered-message reference, when applicable;
- associated action ID/version;
- lifecycle: collecting input, prepared, awaiting approval, completed, cancelled or superseded.

Store references rather than unnecessary duplicated content. Any new content-bearing state needs retention, deletion-cascade and consent treatment under AGENTS §9.

The application must distinguish an owner clarification from a new task. A short name following “which recipient?” should complete that recipient slot. A clear topic switch should suspend or cancel the earlier task, not silently apply its text elsewhere.

### 6.3 Instruction and data channels

Use separate fields for:

1. **Owner instructions:** verified owner-authored spans relevant to this request.
2. **Literal message body:** data to copy, not instructions to the assistant.
3. **Contact evidence:** untrusted, conversation-scoped material.
4. **Application facts:** IDs, versions, states and validated selections.
5. **Model suggestions:** proposals with provenance, never owner authority.

The writer may receive several relevant owner turns, but not the whole owner-control history. A generated summary must not silently promote third-party text into an owner instruction.

### 6.4 Decisions and invariant wording

| Rule or decision | Recommended treatment | Security trade-off |
|---|---|---|
| §6.1–2: application decides; no sending tool | Keep unchanged | No new execution authority |
| §6.3a: tool-less contact-triggered runs | Keep unchanged | Preserves constrained contact processing |
| §6.3b / ADR-08: tainted mutation confirmation | Keep the gate; do not clear taint simply because a new owner turn arrived | Preserves protection from inherited untrusted content |
| §6.3b wording about “change state” | Explicitly clarify that inert request/proposal bookkeeping grants no capabilities; confirmed domain mutations still require the existing gate | Requires clear separation so a workflow update cannot activate settings, delegation, memory or sending |
| R2/E3 and D2/PF-11 | Amend current-turn-only handling to allow verified, scoped owner spans across turns; permit the owner-authored portion beside a separately stored quote | Larger instruction packet; mitigate stale-task selection and cross-chat leakage with source/scope validation |
| PF-12/D4 | Accept verified earlier owner spans belonging to the active request; copy original text | Avoid unrestricted historical substring acceptance |
| D1/PF-10 | Explicit drafting requests are fulfilled once sufficiently specified; `needs_input` is permitted | Adds a necessary question in underspecified cases; does not restore a model veto |
| D8/PF-03 | Weak candidates never auto-selected; their presence does not invalidate a unique strong match | Requires an evaluated deterministic ambiguity policy |
| PF-15 / D3 | Extend artifact state and require grounded content reads | Additional scoped context/read cost; draft text remains untrusted |
| §6.4: isolation | Keep unchanged | Do not share the full planner context with writers |
| §6.6–10, 12–14 | Keep unchanged | Approval, at-most-once execution, consent, takeover, Telegram rules and forbidden capabilities remain |
| ADR-07a / AGENTS §8 model choice | Record any promotion after approved synthetic evaluation | Increased spend; repair pricing coverage first |
| P3-T132 | Revisit before implementing nudges | Avoid repeating generic-output behavior; do not bypass provenance by passing tainted planner prose |

A full single-model writer using all owner-control context would require reconsidering the isolation boundary. I do **not** recommend that as the initial repair.

Likewise, a strict CaMeL-style clean planner would be a larger architectural change: the current planner intentionally reads chat content. Adopting that design would change how it answers open-ended questions and should be a separate owner decision.

### 6.5 Artifact-grounded reporting

Treat cards and chat as two views of the same action.

- Return an application receipt containing action ID, version, origin, mode and actual status.
- Include relevant terminal transitions and replacement relationships in later turns.
- Read the artifact before answering questions about its text or topic.
- Generate consequential state phrases from deterministic receipts.
- Preserve the distinction between “proposal stored,” “card presented,” “awaiting approval,” and “sent.”
- Refresh state after long operations rather than presenting the initial snapshot as timeless.

### 6.6 Evaluation target

Build synthetic multi-turn scenarios covering:

| Scenario | Required end state |
|---|---|
| Dictation → recipient clarification | Original exact text, correct recipient, owner-dictated origin |
| One strong match plus weak alternatives | No unnecessary recipient question |
| “Write to X” without content | Content question; no invented card |
| Explicit new topic beside an old chat question | New-message draft follows owner topic |
| Quoted contact message plus owner instruction | Owner instruction preserved; quote remains untrusted |
| “Shorter,” “him,” “that message” | Correct request/action reference |
| Contact writes or edits while drafting | Accurate outdated/retry outcome and report |
| Owner approves/rejects between turns | Later conversational state matches the database |
| Consent revoked during continuation | No prohibited provider call or effect |
| Topic switch and cancellation | Earlier text never applied to an unintended recipient |
| Worker/provider failure | Accurate failure or partial-state reporting |
| Injection in name, message, summary or draft | No unconfirmed domain mutation or unapproved send |

Grade safety invariants and exact artifact state deterministically. Use calibrated judges for intent preservation, language and conversational quality. Measure repeated-trial reliability, unnecessary questions, successful-task latency, and cost per **successful request**, including clarification and retries.

### 6.7 Model routing recommendation

| Route/work | Recommendation |
|---|---|
| Owner conversational planner | Evaluate `gpt-6.1-sol` at medium effort against Luna on the repaired multi-turn harness |
| Exact dictation and literal appends | Deterministic source copying; no writer model |
| Owner-requested composition | Start by evaluating Luna medium with complete intent packets; compare stronger writer only where quality still misses |
| Triage and monitor classification | Keep Luna unless owner-confirmed scenario results justify promotion |
| Summaries | Keep the present route pending its own quality gate; avoid coupling its model choice to planner changes |
| Proactive Assist drafting | Evaluate separately from owner-requested composition |
| Independent audit model | Do not add by default; it is not a substitute for provenance or policy and P3 OD-29 already makes adoption evidence-dependent |

`draft_owner` and Assist currently share the `draft_reply` model/effort route. If evaluations justify different choices, introduce a separately configurable prompt/workload override rather than increasing every draft’s cost automatically. [bootstrap.py:348](../../../../src/assistant/bootstrap.py#L348), [config.py:112](../../../../src/assistant/config.py#L112)

For the first comparison, use a fixed stronger planner rather than an additional model-based router. Once task success is measurable, cheaper routing can use explicit workload classes and tested escalation conditions.

No production promotion is established by this review. Repair intent transfer and pricing first, then obtain the required evaluation-cost approval and record the quality/cost decision.

## 7. Anything not verified

- No tests were run. Findings are static code/document review, not execution results.
- Production configuration, flags, logs, database state and actual Telegram content were not accessed. Deployment/model statements come from the supplied brief.
- No claim is made that the send executor, consent system or all policy rules received a complete security audit.
- Incident frequency, latency, token usage and the benefit of a stronger planner were not measured.
- The code supports the described failure mechanisms; it does not identify every model choice made in the private incidents.
- The full active `draft_owner.v3` dataset was not established as passing: the repository records a four-case confirmation run following the earlier full v2 runs.
- Public sources do not reveal the full internal harnesses of ChatGPT or Claude chat.
- CaMeL and multi-agent security results were reviewed as published claims, not independently reproduced. The October 2026 composition paper is a preprint.
- One attempted Anthropic Managed Agents article fetch was inaccessible; no recommendation depends on it.
- Current official model documentation was checked, but account access and this product’s quality on those models remain unverified.
- P3 specifications contain future behavior. They were not treated as implemented functionality.
- Repository status remained unchanged after the review.

## 8. Sources

External sources consulted live:

1. [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
2. [OpenAI — Orchestration and handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration)
3. [OpenAI Agents SDK — Context management](https://openai.github.io/openai-agents-python/context/)
4. [OpenAI Agents SDK — Handoffs](https://openai.github.io/openai-agents-python/handoffs/)
5. [OpenAI Agents SDK — Tools](https://openai.github.io/openai-agents-python/tools/)
6. [Anthropic — Multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system)
7. [OpenAI — Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
8. [LangChain — Memory overview](https://docs.langchain.com/oss/python/concepts/memory)
9. [Anthropic — Effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)
10. [Anthropic — Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
11. [Anthropic — Writing effective tools](https://www.anthropic.com/research/writing-tools-for-agents)
12. [OpenAI — Function calling](https://developers.openai.com/api/docs/guides/function-calling)
13. [LangGraph — Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
14. [Simon Willison — Dual LLM pattern](https://simonwillison.net/2023/Apr/25/dual-llm-pattern/)
15. [CaMeL — Defeating Prompt Injections by Design](https://arxiv.org/abs/2503.18813)
16. [Design Patterns for Securing LLM Agents Against Prompt Injections](https://arxiv.org/html/2506.08837v1)
17. [Can CaMeLs Talk? — October 2026 preprint](https://arxiv.org/abs/2610.05640)
18. [OpenAI — Safety in building agents](https://developers.openai.com/api/docs/guides/agent-builder-safety)
19. [Anthropic — Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)
20. [OpenAI — Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
21. [OpenAI — Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
22. [OpenAI — SDK integrations and observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability)
23. [OpenAI — Model selection](https://developers.openai.com/api/docs/guides/model-selection)
24. [OpenAI — GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
25. [OpenAI — GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)