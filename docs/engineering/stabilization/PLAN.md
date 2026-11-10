# Stabilization plan — approved 2026-11-09

[Owner decisions](DECISIONS.md) amend [review §5](review-2026-11-09.md#5-implementation-plan). Unit IDs and acceptance criteria are retained below; D-S6 replaces the separate owner-requested writer with composition/revision by the main chat model. This is approved repair scope across M02–M05, not authorization to start P3.

**Status, 2026-11-09 (owner-provided merge/review record):** P0-1 merged **#240**; Q1 merged **#236**; I19 merged **#238**; I21 merged **#237**; I7 in review **#239**; **P0-2 in progress**. **A1 started early (owner, 2026-11-09, D-S16)**; its contract dependencies remain. All other units are approved scope awaiting dependencies/dispatch. A unit merge does not mark any milestone done. Each unit has one implementation agent and one PR to `main`.

### Contract and implementation index

P0-2 specifies the target interfaces; these documents do not claim deployed behavior. [Shared conventions and compatibility seams](contracts/README.md) apply to every unit. [Phase 2 briefs](specs/README.md) give H1–H13 and Q4 exact acceptance, regression-first tests, affected docs and lane dependencies. [A1 synthetic cases](cases/README.md) include the full scenario matrix and four incident-pattern families in Uzbek Latin/Cyrillic, Russian and English.

| Contract | Scope / owning units |
|---|---|
| [C1 — Request state](contracts/C1-request-state.md) | H1/H2 durable sources, continuation, cancellation and retention |
| [C2 — Proposals](contracts/C2-proposals.md) | Q4/H3/H9/H4 main-model text, exact copying, version authorship and revision |
| [C3 — Receipts/card facts](contracts/C3-receipts-and-card-facts.md) | Q3/Q5/H7 current and terminal facts, presentation and final-answer freshness |
| [C4 — Taint lifecycle](contracts/C4-taint-lifecycle.md) | H12 precise context selection and preserved owner instructions |
| [C5 — Contact resolution](contracts/C5-contact-resolution.md) | H5 deterministic verdict and request-bound selection |
| [C6 — Freshness/precedence](contracts/C6-freshness-and-precedence.md) | H10/H11 source revisions, request time and settlement; H10 is a merge prerequisite for I2/I4 |
| [C7 — Coverage/budgets](contracts/C7-coverage-and-budgets.md) | I2/I4/I6 contiguous complete review; I15/I16 shared allowance/admission |
| [C8 — Preferences](contracts/C8-preferences.md) | H13 source-verified owner preferences for main chat and scoped Assist |
| [C9 — Privacy recovery](contracts/C9-privacy-recovery-options.md) | I20 Option C hybrid decided in D-S15: encrypted ids-only journal in existing B2 backup storage; mandatory manual/purge fallback |

Allocate each migration's next free revision from merged main at implementation time: **0014 is currently the head**. P3's planned `0015_p3` is on hold and will rebaseline; it reserves no number. H4/H6 may land disabled against the typed freshness seam; H10's source foundation may prepare early and its runtime hooks follow H7. **I2/I4 merge only after H10**, including its revision migration and ingestion/edit/deletion/retention hooks; their settlement cannot use a stub revision. H10 remains one PR; no earlier foundation unit is allocated. No repaired candidate is activated/released until the fence checks are complete. This preserves the shared-file merge order below.

### Execution rules for every work unit

Owner approval is recorded in D-S1–D-S16 (2026-11-09). The lead reviews, tests and merges; implementation agents hand off uncommitted work. CI is the full-suite gate (docs-only PRs use the existing skip rule). Deploys need the owner's go-ahead.

- One work unit means **one focused PR**, owned by one implementation agent.
- Use branches such as `stabilize/h03-source-spans`; PRs target `main`. Sequence dependent PRs through merged foundations.
- Preserve §6 invariants and their named regressions. Fixes start with a failing regression; tests encoding approved undesirable behavior change only alongside the corresponding approved decision.
- The lead runs short relevant tests in the dev container. Implementation agents in restricted worktrees do not run Docker/tests or contact Telegram, OpenAI or servers; they supply exact relevant test commands and report checks as unrun. **CI remains the full-suite gate.**
- New prompts receive new versions; used versions remain immutable. Schema changes require compatible persistence/migrations and appropriate prompt versions.
- Gated prompt/schema changes require approved synthetic evaluations before shipping. Reports record configuration, dataset/prompt hashes, cost, failures, and owner-confirmation status.
- Every new stored content class receives retention, deletion-cascade, consent, export, and restore treatment, with ARCHITECTURE §10.1/10.4 and sweep/cascade updates in that PR. Prefer source references over duplicated text.
- No production deployment or paid run is authorized by this plan.
- “Parallel” below means logically independent implementation. PRs editing the same runtime, registry, prompt, or card renderer must still be sequenced.

Sizes: **S** = narrow change; **M** = one workflow; **L** = bounded cross-layer change with a contract/migration. An L unit must stay within its stated responsibility: one frozen contract, one bounded feature, one agent and one PR. It cannot absorb adjacent units. The lead returns an overlarge change for an explicit plan split before implementation rather than allowing an omnibus PR.

### Phase 0 — Research and decisions

Research synthesis is preserved in the review. Owner decisions are recorded; P0-2 contract specification is in progress with the linked interfaces and cases ready for review.

| Unit | Goal and scope | Acceptance criteria and verification | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **P0-1 — Decision record and amended plan** | Record D-S1–14, approved repair scope, P3 hold and amended unit/dependency plan; update IMPLEMENTATION_PLAN, P3 README and AGENTS document pointer. **Merged #240.** | Earlier rulings and required document changes are mapped; D-S6 trade-off and P3 parallel-work answer are explicit. Documentation only; no milestone marked done. | Owner decisions; P0-2 freezes against this record. | M |
| **P0-2 — Contracts and acceptance specification** | Freeze [contracts C1–C9](contracts/README.md), [unit briefs](specs/README.md) and [synthetic cases](cases/README.md): request/source state, main-model proposals, authorship, receipts, freshness, coverage, budgets and recovery. **In progress.** | Account/consent/retention boundaries, cancellation/topic switching, strict tool APIs and approved thresholds are reviewable. Cases precede dependent implementation. D-S15 hybrid recovery contract explicit; options retained as history, I32 policy/notice check required. | P0-1 merged; early Q1/I7/I19/I21 use existing interfaces. | M |

### Phase 1 — Owner-UX fixes requiring no new product decision

These repair existing promises without changing drafting authority or approved trust policy.

| Unit | Goal and scope | Acceptance criteria and tests | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **Q1 — Read effective settings** | Add a content-free preference read model through `observability/status`, owner status tooling, and existing preference services. **Merged #236.** | Chat can inspect digest time, quiet hours, cap, bypass, and reply defaults after button changes/Undo. Tests cover effective defaults and account isolation. | Approved stabilization scope; parallel. | S |
| **Q2 — Truthful deterministic UI** | Correct `/help`, `/status`, and mode-sensitive card headings in `handlers.py` and `approvals.py`. | Help accurately scopes pause and lists pause/resume; status includes alert backlog; standalone messages are labelled accordingly. Rendered-output regressions. | Scope approval; sequence shared renderer edits. | S |
| **Q3 — Expose existing card facts** | Extend `get_draft`/activity output with state reason, relevant terminal facts, version, and available presentation outcome. | A read can explain an outdated/rejected/failed card without guessing. Tests cover terminal state, missing text, consent, and foreign-account IDs. | Scope approval; parallel with Q1. | M |
| **Q4 — Honest proposal conflicts** | Fix same-turn differing-argument reuse in `agent/tools/replies.py`. | Exact-call retries remain idempotent. A different proposal returns an explicit existing-draft conflict instead of implying its requested text was staged. | Scope approval; before H3/H8 edits to the same module. | S |
| **Q5 — Accurate card-edit settlement** | Narrow success-equivalent edit handling in `approvals.py` and Telegram error classification. | A parse/missing-message failure is not recorded as a successfully shown state. “Not modified” is handled distinctly. Synthetic sync/recovery regressions. | Scope approval; sequence with Q2/Q3. | M |

Contact selection, missing-content behavior, cross-turn dictation, and taint changes are deliberately in Phase 2 because their current behavior follows recorded decisions.

### Phase 2 — Harness rework

The target is one coherent owner workflow with durable intent and **one main chat model writing owner-requested text** (D-S6):

`owner source turns → active request → main owner_request context → source-copy dictation OR model-written propose/revise → recipient + full-text card → owner tap → policy re-check → committed send claim`.

Main-model context may contain several chats; unintended cross-chat disclosure is an explicit quality risk to evaluate. Contact-triggered Assist/triage/summary/monitor runs retain their existing one-conversation isolation and tool-less outputs. Future delegated writing stays separately scoped. No owner-requested composition or Re-draft routes back through draft_owner.

| Unit | Goal and scope | Acceptance criteria and tests/evals | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **H1 — Durable request foundation** | Add account-scoped request state and migration: source references, operation, selected recipient, missing slot, action/version linkage, lifecycle. | Restart preserves unfinished requests. Deletion/retention removes or invalidates their references. Migration up/down and cross-account/cascade tests pass. No execution authority is added. | P0 contracts; foundational. | L |
| **H2 — Continuations and explicit reply targets** | Connect control ingress, dispatcher, request service, and context assembly. Preserve Telegram reply-to IDs. | Recipient answers, “shorter,” and “that card” resume the correct task. Explicit reply targets win. Topic switches/cancellation never apply old text to a new recipient. | H1, D-S2/4; H3 may prepare separately, but shared registry/context edits merge sequentially. | M |
| **H3 — Literal source copying** | Replace fuzzy authorship proof in `tools/replies.py` and literal append paths with validated source-span references. | Dictation survives recipient clarification and is character-exact, including `X1`, case, punctuation, and Unicode. Missing/deleted/foreign sources fail closed. | H1/H2 and Q4; D-S2. Before H9/H4 and prompt activation. | M |
| **H4 — Main-model composition proposals** | Add a strict propose tool taking text written by the main owner_request model, plus request/recipient/target references. Replace owner-requested draft_owner handoffs; reserve the tool-less writer for proactive Assist. Wire bounded, consent-checked proposal validation and factual action receipts. | Composition uses the main conversation and preserved owner instructions, with quotes untrusted. Generated cards say written by the assistant and show recipient/full text; source-backed dictation keeps H3. No send tool, silent approval or cross-account input. Strict schemas, semantic-invalid/foreign IDs, registry flag/tier/run filtering and idempotency tests; affected journey/retrieval evals before shipping. | H1/H2/H3/H9/H12 and P0-2 proposal contract; Q4 first. D-S6. Tool wiring lands before H6 prompt activation. | M |
| **H5 — Contact-resolution verdict** | Refine `conversations/read.py`, contact tool output, and request selection binding. | Unique adequate strong match is distinguished from genuine ambiguity and weak-only suggestions. Tests cover full names, token order, scripts, typos, and misleading prefixes. No permanent alias is silently saved. | D-S5; request binding depends on H1. | M |
| **H6 — Coherent main-chat prompt** | Add the next unused owner_request version for clarification, search, literal dictation, main-model composition/revision and tool-error recovery. Do not create a replacement draft_owner prompt. | Ask one useful missing-content question; continuations override bare search. Advice/list editing creates no card. Main model supplies its generated text to H4; exact dictation references H3 source spans. New behavior passes full affected journey/retrieval evals. | H2–H5, H9/H12/H13; one prompt steward, sequential activation. H4 must be wired first. | M |
| **H7 — Artifact-grounded reporting** | Add action receipts and refreshed request/action context in runtime and tools. | Status acknowledgments reflect a fresh action/version/presentation outcome. Draft-content claims require reading the actual artifact. Tests change card state between tool result and final response. | H1, Q3; integrates after H6; before H8 runtime work. | M |
| **H8 — Task-preserving Re-draft and targets** | Re-draft queues request/action IDs and resumes the main owner_request model with original mode, owner sources/instructions, selected artifact/version, recipient and constraints. Load explicitly selected older retained targets. Remove owner Re-draft dependence on the isolated writer; Assist generation remains separate. | Main-model Re-draft preserves semantics across restart; unavailable targets report an explicit outcome without falling back to another message. Every no-card outcome is reported. Closed estimated windows follow P2-O20 for owner-requested work. Approval, consent and stale-version regressions plus redraft journey eval. | H1/H4/H6/H7/H9 and H10 target fence; sequence runtime/replies/approvals edits. | M |
| **H9 — Version-specific authorship** | Define generated versus source-copied provenance per action version across revision lifecycle, card rendering, cancellation and execution. Support H4's generated-text proposal contract. | Main-model rewrite of dictation becomes generated and is labelled written by the assistant; literal source replacement/append remains owner-authored only when copied by the application. Consent revocation and stale tokens are tested for both; full text and recipient precede approval. | H1/H3 and P0-2 authorship contract; Q2/Q5 renderer fixes first. Lands before H4; H8 consumes it. D-S2/6. | M |
| **H10 — Source revision and draft freshness** | Introduce a conversation/source revision fence; propagate it through generation and proposal validation. | Relevant edits/deletions during generation cannot yield an approvable stale draft. Revision migration plus ingestion/edit/deletion/retention hooks supply I2/I4 settlement; paused-call edit/delete regressions reject stale results. No lock spans a model call. | P0 contract; context hooks after H7. Must merge before I2/I4/I3/H8/H11; preparatory work alone does not satisfy the prerequisite. | L |
| **H11 — Owner intent precedence** | Carry request-time takeover reference and enforce owner/background precedence at settlement. | Manual reply during generation wins; late Assist cannot replace newer explicit work. Existing one-card, stale approval, and at-most-once regressions remain. | H1/H10, D-S10. | M |
| **H12 — Taint-safe context lifecycle** | Replace D56 decay with context selection/gating: remove stale contact/derived untrusted prose; retain owner source instructions and inert request facts. | Any supplied untrusted prose retains required confirmation, including inherited replies. Owner instructions survive independently of dropped contact text. Adversarial settings/deletion/control tests; no taint bypass through proposal bookkeeping. | H1/H2, D-S3; before H4/H6 activation. Sequence runtime/context edits. | M |
| **H13 — Effective trusted preferences** | Capture explicit owner-authored preferences with validated source/scope/applicability. Load applicable owner preferences into the main chat composition context; keep the proactive Assist preference loader scoped. | Confirmed explicit style preferences affect main-model owner drafts and eligible Assist drafts. Contact-derived/model-paraphrased notes cannot become global instructions. UI explains applicability; consent/account isolation and preference journey tests. | H1/H4/H12 source/context contracts, D-S2/3/6; before H6. Sequence memory/registry edits with I12. | M |

**Phase 2 exit:** the four reported conversation incidents have synthetic end-to-end regressions; the selected candidate passes the agreed owner-journey evaluation before deployment.

### Phase 3 — Industrial hardening

Privacy recovery, backup durability, and alert-loss repairs should start alongside Phase 1 where dependencies permit. Their phase number is not a reason to postpone high-severity work.

#### Review coverage, obligations, and alerts

| Unit | Goal and scope | Acceptance criteria and tests/evals | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **I1 — Evidence-backed task resolution** | Extend triage resolution schema/validation and task application; new triage prompt version. | Unsupported/suspicious resolution leaves tasks open. Evidence IDs are shown, new, scoped, and retained. Adversarial closure regressions and full affected triage eval. | D-S9; parallel with harness work. | M |
| **I2 — Contiguous triage review** | Repair triage burst loading and coverage cursor settlement. | Repeated bounded runs drain a backlog without skipping retained messages; coverage never includes unexamined ranges. Deterministic backlog/restart and edit/delete-during-call stale-settlement tests. Progress deletion/sweep regression retains the verdict while atomically clearing both `review_progress_id` and `review_version`. | P0 coverage contract + H10 merged revision foundation/hooks; coordinate I1/shared triage edits. No merge before H10. | M |
| **I3 — Edited-source reassessment** | Invalidate triage and dependent task/attention freshness on edits/deletions. | A changed already-covered message is re-examined; removed deadlines/requests are not presented as current. Tests cover change during and after review. | H10 source revision. | M |
| **I4 — Reliable semantic-monitor review** | Repair monitor burst ordering, failure settlement, and degraded status in `monitor_eval`/automation/jobs. | No message is silently skipped by limits or invalid output. Retries are bounded; exhausted work remains inspectable/recoverable. Monitor eval, failure/restart and edit/delete-during-call stale-settlement tests. Progress deletion/sweep regression retains any linked verdict/summary while atomically clearing both `review_progress_id` and `review_version`. | P0 coverage contract + H10 merged revision foundation/hooks; parallel with I2 only after H10. | M |
| **I5 — Useful retrieval excerpts** | Match-centered search snippets and explicit retained-message retrieval through existing conversation reads. | A match near the tail is visible and attributable; owner can retrieve the full retained message. Tests cover multiple matches and account/consent boundaries. | Scope approval; parallel. | S |
| **I6 — Complete long-message review** | Add bounded chunk/completeness handling for summary, triage, and semantic-monitor readers. | A deadline beyond character 2,000 is examined or explicitly left unreviewed; cursors never certify a clipped tail. Route-specific regressions and affected eval sets. Progress deletion/sweep after raw-message retention retains the rolling summary while atomically clearing both `review_progress_id` and `review_version`; cover retained verdicts too. | I2/I4 coverage contracts; coordinate prompt versions. | L |
| **I7 — Durable held-alert delivery** | Replace delivery sinks with recoverable defer/inbox semantics; retain monitor identity and explanation. **In review #239.** | Digest-off, quiet hours, hourly cap, restart and one-shot monitor scenarios preserve eventual important-alert delivery. No ambiguous send is replayed. | D-S8; independent of harness; existing delivery contracts until P0-2 freezes deltas. | M |
| **I8 — Correct delayed-alert meaning** | Bind notification rendering to triggering verdict/source and current actionable state. | A resolved condition cannot render as an unexplained current urgent request. Historical alerts retain their original explanation. Digest tests cover supersession. | I7 event contract. | M |
| **I9 — Explicit priority and suppression policy** | Complete urgency mapping; expose derived mutes and reset; scope monitor feedback appropriately. | Every urgency level has tested delivery semantics. Owners can inspect/reset suppression; unrelated new monitors do not inherit unexplained suppression. | D-S8; follows I7. | M |
| **I10 — Complete obligation read model** | Build attention/digest candidates from unanswered chats plus open obligations, including undated and overdue items. | Speaking last does not hide an unfinished promise. Reply-needed, obligation-open, overdue, and no-due-date states are distinct. | D-S9; parallel with I7. | M |
| **I11 — Minimal task governance** | Pull forward only list/complete/cancel/correct/review-stale controls and approved expiry changes. | Owner can correct extracted tasks; age never implies fulfillment. Changes are scoped, confirmed where required, and audited. | D-S9, I10; explicitly remove duplicate scope from M07. | M |
| **I12 — Complete memory management** | Add pagination and targeted lookup to memory discovery/correction/forgetting. | An item beyond the first 50 can be inspected, corrected, and forgotten; provenance and consent remain intact. | Prepare against separate interfaces; shared memory/registry edits merge after H13. | M |

#### Runtime, delivery, and cost

| Unit | Goal and scope | Acceptance criteria and tests | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **I13 — Conversation order and delivery history** | Link assistant responses to owner triggers; persist shown/unshown/uncertain delivery status in runtime/persistence. | Rapid queued turns see completed preceding answers. Definitely unshown text is not ordinary dialogue history. Failure-after-send tests do not blindly replay responses. | P0 lifecycle contract; after H2/H7/H8/H11 runtime edits. | L |
| **I14 — Request/attempt recovery** | Separate completed requests from failed/interrupted attempts; add exception/cancellation finalization. | Two successive interruptions cannot suppress an unanswered request. Partial cards/effects are discovered and reported without duplicate business sends. | I13 identifiers; parallel afterward. | M |
| **I15 — Shared operation limits** | Propagate deadline, cancellation, and token allowance through runtime, tool execution, and nested model work. | Multiple nested calls/retries stop within the shared allowance. Completed partial work is reported truthfully. Fake-clock/usage tests; no timing sleeps. | P0 budget contract, I13/I14; serialize runtime/tool-executor edits. Main composition and remaining Assist calls share limits. | L |
| **I16 — Priced call admission** | Add verified model pricing coverage, bounded spend reservation, attempt records, and reconciliation. | Unknown-price production routes fail admission or use an approved conservative bound. Concurrent/nested requests cannot all spend the same remaining allowance. Uncertain billing is visible. Deletion during dispatch preserves minimal accounting identity/reserve and permits exactly-once late settlement; regression first per C7. | D-S7, I15 contract; before stronger-model promotion. | L |
| **I17 — Bound dispatch waiting** | Enforce throttle acquisition/reservation bounds and distinguish not-started work from ambiguous network delivery. | Long backlog cannot violate the uncertainty threshold assumption; policy/connection facts are refreshed at the correct dispatch boundary. At-most-once regressions remain green. | Independent; send-path review required. | M |

#### Operations, privacy recovery, and diagnosis

| Unit | Goal and scope | Acceptance criteria and tests | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **I18 — Configuration parity** | Unify supported production settings and environment-push allowlist/validation. | Route/effort settings survive a placeholder-only render; deployment reports effective non-secret routes/flags. No secret values appear in output. | Before D-S7 promotion; parallel. | M |
| **I19 — Restore quarantine** | Keep restore startup/content-processing gates until recovery and retention finish; require explicit privacy reconciliation. **Merged #238.** | Restored active consent cannot authorize processing before clearance. Sweep/recovery failure leaves the gate closed. Synthetic restore tests. No unattended-recovery claim before I20. | D-S11; fail closed on existing recovery seam; I20 design is not assumed. | M |
| **I20 — Privacy-decision recovery** | Implement D-S15/C9 Option C: encrypted ids-only deletion/withdrawal journal next to off-site backups in existing Backblaze B2, replay after restore, mandatory manual reconciliation/complete-purge fallback when continuity is unproven. | Backup → revoke/delete → restore cannot resurrect usable deleted content or authorization. Missing/stale/tail-incomplete recovery evidence keeps I19 closed until replay or fallback completes; fresh consent alone is insufficient. Journal retention, encryption, export/deletion and failure semantics follow C9. | D-S11/D-S15, I19 and I21; coordinate I32 policy/notice check before retained privacy metadata is journaled. No new processor. | L |
| **I21 — Durable backups** | Add overdue catch-up and restart-safe upload retry to backup scripts. **Merged #237.** | Startup after the scheduled hour produces an overdue backup once; restart preserves failed-upload work. Placeholder smoke tests cover both. | Independent. Sequence shared deploy script edits with I18/I22/I24. | M |
| **I22 — Selected release artifact** | Deploy an explicit SHA and immutable image identity; verify all required checks for that revision. | A moving `main` cannot change the selected release. Script/image checks join the release gate; previous artifact remains available. | Independent of harness; no new registry required by default. | M |
| **I23 — Migration and release recovery** | Define compatibility/drain sequencing, health wait, and schema-aware rollback/forward recovery. | Failed migration/health prevents release success. A placeholder rehearsal demonstrates previous-compatible or forward recovery and records actual limitations. | I22; relevant migration review. | M |
| **I24 — Least-privilege services** | Separate app, migration, and backup secrets/database roles in Compose/provisioning. | Each service receives only required credentials; application cannot perform migration-only operations. Placeholder structural/integration checks. | Independent; coordinate I18 environment format. | M |
| **I25 — Structural incident trace** | Persist bounded request/run/child/action/version IDs and fixed decision codes; add a content-free incident report. | A synthetic wrong-draft case reveals instruction source, recipient selection, dropped channel, observed version, and outcome. Redaction tests prove no content/secret logging. | H1/H7 contracts; parallel with operations. | M |
| **I26 — Service-completion monitoring** | Add queue age, unanswered turns, failed/degraded reviews, unresolved presentations, retention, backup/upload age, and disk indicators. | Poller health cannot hide stalled owner/LLM/outbound work. Sustained synthetic degradation produces a content-free operational signal. | I14/I19/I25 state contracts; incremental. | M |

#### Owner comprehension and maintainability

| Unit | Goal and scope | Acceptance criteria and tests | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **I27 — Localized approval/recovery cards** | Add explicit owner-triggered show-current-card recovery and localize cards, controls, edit instructions and reasons. | Four supported locales convey the same exact approval/status meaning. Recovery shows the current recipient/text/version without replaying a business action or automatically retrying uncertain presentation. Owner reviews Uzbek/Russian text; rendered tests cover long text, stale tokens and failures. | D-S12, H7/H8/H9/H11; sequence renderer edits. | M |
| **I28 — Localized operational controls** | Localize help/settings/privacy/alerts using the same locale machinery. | Language selection applies to authoritative controls and failure explanations; consent meaning is preserved. | D-S12/13; independent of I27 content files where possible. | M |
| **I29 — Card responsibility extraction** | Extract presentation/recovery and revision orchestration from `approvals.py`, keeping behavior fixed. | Existing lifecycle/presentation regressions pass; interfaces expose action/version/outcome explicitly. No unrelated redesign. | After H7/H8/H9/H10/H11, Q5 and I27 stabilize. | M |
| **I30 — Draft responsibility extraction** | Separate request preparation, generation, and settlement from `drafting.py`. | Source/intent/budget contracts are explicit; race and consent regressions pass. Shared call helpers are extracted only where contracts actually match. | After harness and coverage fixes. | M |
| **I31 — Prompt manifest and archive** | Add immutable asset hashes, schema/eval links, active status, reference reporting, and compatible archive lookup. | Active and historical labels resolve to identical bytes. Referenced versions are never deleted. Archival works without production content inspection. | Prompt steward; coordinate P3 F0-E. | M |
| **I32 — Current release and privacy record** | Consolidate deployed/planned/history views; align policy code, publication procedure, notices, routes, flags, migrations, and acceptance links. Check D-S15 journal retention as privacy metadata with I20. | One compact record describes the candidate release and open risks. Before journal use, record policy/notice applicability: any policy edit requires a version bump; processing-scope growth requires owner notice with opt-out. Existing B2 does not waive this check. Published policy verification/notice occurs only with owner authorization. | D-S13/D-S15; I20 policy check early, final release values after candidate selection. | M |

**Deferred:** R4-14 becomes a documented entry checklist before adding another owner or outbound replica. This stabilization phase does not implement speculative multi-user infrastructure.

### Phase 4 — Audit and acceptance

| Unit | Goal and scope | Acceptance criteria and evidence | Dependencies / parallelism | Size |
|---|---|---|---|---|
| **A1 — Executable journey evaluator** | Extend `evals/owner_drafting` or add a journey target that runs successive turns and application events against persistent synthetic state. | It can express earlier-turn dictation, actual clarification, card transitions, queued input, failures, and restart. Deterministic grader tests reject wrong recipient/text/version/status. | P0 cases; build early against H1 contract. | M |
| **A2 — Candidate configuration comparison** | Evaluate the complete repaired main-chat composition path, proactive Assist separately, affected existing evals, and gpt-6-luna versus gpt-6.1-sol for the main chat model after fixes. No model switch now. | Run-specific owner spend approval; fixed prompts/routes/effort/flags; repeated trials; cost and latency per successful request. Partial trials never count as full gates. | Relevant units merged; I16/I18 before comparison. D-S7; no inherited permission to spend. | M |
| **A3 — Owner playtest and language review** | Execute the script below using the dev bot and synthetic conversations, then a planned owner-only production acceptance window. | Owner assesses intent and language; agents see only synthetic outputs/content-free evidence. M04/M05 live/trial obligations are completed or explicitly accepted as open. | A2 candidate; owner participation. | M |
| **A4 — Operational rehearsal** | Rehearse backup/upload failure, safe restore, privacy reconciliation, failed release, provider outage, card uncertainty, and worker restart. | Measured recovery evidence supports NFR-09; no restored send, revoked processing, silent lost work, or ambiguous-send replay. Production actions require specific authorization. | I19–I26; parallel with A3. | M |
| **A5 — Independent final review** | Review the candidate diff, contracts, synthetic trajectories, CI, operational evidence, and owner findings. | No unresolved critical/high finding; every original ID closed, accepted, or explicitly deferred. Owner reviews M04/M05 reports and decides whether P3 resumes. | A2–A4 and fixes complete. | M |

### Synthetic scenario matrix

Run Uzbek Latin, Uzbek Cyrillic, Russian, and English variants where applicable. Include mixed-language turns and owner-selected reply language.

| Journey | Required result |
|---|---|
| Dictation → ambiguous recipient → selection | Original characters, correct recipient, owner-dictated provenance; no re-paste required. |
| Unique strong contact plus weak suggestions | No unnecessary recipient question. |
| “Write to X” without substance | Ask what to write; create no invented card. |
| New topic beside an old unanswered chat question | Standalone message follows the owner’s topic. |
| Quoted contact text plus owner instruction | Preserve owner instruction; quote remains untrusted and scoped. |
| Two cards; reply directly to one with “shorter” | Revise the explicitly selected card/version. |
| Selected older retained message | Answer that message or explain why unavailable; never silently switch targets. |
| Contact arrival/edit/delete during generation | Discard or clearly outdate affected work. |
| Manual owner reply during generation | Overtaken work cannot emerge as current. |
| Assist finishes after explicit owner proposal | Explicit owner work remains the live task. |
| Approve/reject/outdate between conversational turns | Later status matches current application state. |
| AI rewrite of dictated text → revoke managed consent | Generated version receives correct cancellation/rendering/execution treatment. |
| Multiple rapid owner turns | Correct completed conversation history and request association. |
| Repeated interruption/restart | Recover unanswered work without replaying completed effects. |
| Digest off + quiet hours/cap + one-shot monitor | Important match remains identifiable and eventually deliverable. |
| Oversized backlog and long-message tail | Complete review or explicit recoverable coverage gap. |
| Suspicious text asking to close tasks/change settings | No unsupported task closure or unconfirmed mutation. |
| Restore after deletion/revocation | Restored data/consent remains quarantined until reconciled. |

### Approved acceptance gates (D-S14, owner 2026-11-09)

These approved thresholds supplement existing gates. Paid evaluations still require specific spend approval; no gate is claimed passed by the plan.

| Gate | Approved requirement |
|---|---|
| Safety and exactness | 100% deterministic pass on recipient, exact dictation, account/consent isolation, approval/version, policy, and at-most-once scenarios. Contact-triggered isolation stays unchanged; D-S6's owner-requested proposals are evaluated for unintended disclosure. Zero observed unauthorized effects in the accepted suite. |
| Core incident journeys | All four reported conversation patterns succeed in at least three repeated trials per applicable locale. No requirement to re-paste already supplied dictation. |
| Intent and language | At least 95% acceptable completion per language on the broader owner-confirmed journey set; failures remain visible individually. |
| Clarification quality | No unnecessary question in unique-strong-match core cases; broader unnecessary-clarification rate at most 5%. Necessary missing-content questions count as correct behavior. |
| Reporting honesty | No false “sent,” “done,” “active,” or wrong-artifact content claim in accepted cases. Explicit uncertain/partial outcomes are required where appropriate. |
| Latency and cost | Report complete-request p50/p95 and cost, including clarification/retries/nested calls. Meet existing NFR targets or obtain an explicit documented exception. |
| Operations | Demonstrate backup catch-up, upload recovery, safe privacy reconciliation, schema-aware release recovery, and visible degraded work. Measure RPO/RTO against NFR-09. |
| Release evidence | CI green; affected full evals recorded; owner language/intent review complete; independent review clean; M04/M05 acceptance reports reviewed. |

A finite suite passing these gates is evidence of readiness, not proof that future failures are impossible.

### Owner playtest script

Use synthetic contacts with deliberately similar names and two different chat topics.

1. Dictate a message containing a case-sensitive code, punctuation, and Uzbek characters; clarify the recipient.
2. Say only “write to X”; confirm that the bot asks for substance.
3. Request a new topic unrelated to the contact’s last message.
4. Quote a synthetic contact request and give an explicit refusal instruction.
5. Create two cards; reply directly to one with “shorter,” then change its word limit.
6. Let a contact edit a source while drafting; verify the explanation and Re-draft behavior.
7. Start Assist work, then dictate a different message before it finishes.
8. Approve one card and reject another; later ask what happened and what each contained.
9. Save a style preference after reading a chat; verify its stated applicability and effect.
10. Turn digest off, create a one-shot reply monitor, and exercise quiet-hours delivery.
11. Ask for unfinished obligations after sending “okay”; complete/correct one.
12. Use the visible recovery control for an uncertain/unshown card.

For each step, the owner records: understood intent, correct recipient/text, necessary questions, language quality, truthful status, and whether recovery was understandable.

### Re-sequencing P3

**P3 is on hold (owner, 2026-11-09).** The owner asked about parallel P3 worktrees; the recorded answer is no: DELEG-DRAFT, the next owner_request prompt and planned 0015 schema depend on contracts being replaced here. Resume only after Phase 4 acceptance and the owner's decision. P3's eventual integration-branch workflow is unchanged.

| P3 item | Required amendment |
|---|---|
| **D0 / `int/p3`** | Rebaseline from accepted stabilized `main`. Replace obsolete calendar assumptions with a new owner-approved start gate. |
| **PR-OR10 / `owner_request.v11`** | The plan’s later amendment already recognizes that v10 was consumed on `main`. Stabilization may consume v11 and later versions too. Allocate the next unused version at implementation time; never overwrite or reserve stale numbers. |
| **Prompt steward** | One steward owns active prompt switches, schemas, and affected evaluation sequencing across stabilization and P3. Tool PRs contribute rule changes through that steward. |
| **F0-S schema** | Reallocate migration numbers and regenerate contracts against stabilized schema. Request/source-revision/provenance changes must not collide with the planned `0015` foundation. |
| **F0-E** | Rebase its registry/evaluation refactor on the prompt manifest, journey evaluator, and actual tool registry. Avoid replacing newer work with CP5-era literals. |
| **DELEG-DRAFT** | Keep the separately scoped, tool-less delegated writer; D-S6 does not apply to contact-triggered delegation. Rebase shared budgets, source freshness, provenance and tracing on the main-chat composition repairs before implementation. |
| **M7-FUP / M7-NUDGE** | Revisit **P3-T132**, which currently accepts missing guidance and generic/“not needed” nudges. Use source-backed task intent; do not pass unrestricted tainted planner prose. |
| **M7-TASK / read tools** | Remove or adapt minimal task-governance work pulled forward in I10/I11. Preserve the rest of M07 as future scope. |
| **M6-ESC / generic draft recovery** | Recheck assumptions that reuse REDRAFT semantics and require an estimated open window; align owner-requested paths with P2-O20 and task-preserving recovery. |
| **PV6** | Reconcile policy publication/versioning with current M04/M05 scope before adding M06/M07 processing. |
| **Autonomy gates and shadow trial** | Retain disclosure, isolation, consent, grounding, takeover, approval, and at-most-once safeguards. Re-run against the stabilized contracts; passing owner drafting does not establish autonomous safety. |
| **Integration workflow** | Stabilization PRs go to `main` under the newly approved plan. P3 retains its owner-approved integration-branch rule unless separately amended. |

The existing prompt/schema collision is recorded in [P3 decisions §F6](../p3/decisions.md#f6-spec-text-and-carried-rulings); the accepted nudge information-loss consequence is [P3-T132](../p3/decisions.md#b8-rulings-on-the-fix-rounds-deferred-items-2026-10-29-final-round).

## Merge order and parallel lanes

Dependencies are merge prerequisites, not permission to edit shared files concurrently. A lane holds the named shared-file ownership until its PR merges; the lead rebases the next unit on that result. Preparatory work may use frozen interfaces in separate files.

| Lane / shared files | Required sequence and cross-lane joins |
|---|---|
| Contracts/schema | P0-1 → P0-2 → H1. Allocate each later migration from merged main; do not reserve P3's 0015. H10 (revision migration + all source update hooks, context hooks after H7) → I2/I4/I3. H10 is one merged prerequisite, not an independently merged preparatory slice. I13/I20 schema changes queue with the lead. |
| Owner ingress/context/runtime | H2 → H12 → H4 wiring → H6 activation → H7 → H10 context hooks → H8 → H11 → I13 → I14 → I15 → I16 runtime admission → I25 runtime trace → I26 runtime metrics. H10 may prepare its separate source-revision foundation earlier. |
| replies.py / composition | Q4 → H3 → H9 provenance seam → H4 proposal tool → H6 activation → H7 receipts → H10 freshness hooks → H8 Re-draft → H11 precedence integration → I15 budget propagation → I30 extraction. |
| approvals.py / lifecycle | Q2 → Q5 → H9 → H7 receipts → H8 → H11 → I27 recovery/localization → I29 extraction. H10 freshness hooks precede dependent H8/H11 settlement. No parallel renderer edits. |
| Tool registry | Q1 → Q3 → H2/H3 seams → H9/H4 → H5 → H13 → H6 activation → H7/H8 → I11 → I12. Any registry edit from I9/I15/I25 queues through the same lead slot. |
| Prompt steward | One steward allocates the next unused version and switches active versions. H6 waits for H2–H5/H9/H12/H13. I1 and I6 own new triage/reader versions but queue prompt-registry changes; A1/A2 and I31 use the resulting manifest. Never edit used versions. No new draft_owner prompt for D-S6. |
| Preferences/memory | H13 after H4/H12; I12 follows its memory/registry edits. Applicable preferences reach the main chat and scoped Assist loaders. |
| Triage/tasks | I1 → I2 → I3 → I6 triage handling, with **H10 merged before I2/I3**. I2 may prepare against C7 but cannot merge without the revision migration/update hooks. I10 may prepare its read model separately; I11 follows I10 and I1 resolution semantics. |
| Monitors/alerts | **H10 merged → I4**, independently of triage loader edits thereafter; P0 coverage contract alone is insufficient. I7 (in review #239) → I8 → I9; I6 joins I2/I4 completeness implementations. I10/I11 digest edits queue behind I8. Q2 status/backlog edits coordinate with I26. |
| Restore/privacy | I19 (merged #238) + I21 (merged #237) → D-S15/C9 hybrid I20 implementation → A4. I32 checks journal privacy-metadata policy/notice treatment before journal use, then records final candidate facts later. I19 stays closed until replay or manual/purge reconciliation completes. |
| Backup/deploy | I21 merged #237; I18 configuration, I22 artifact, I24 role separation may prepare disjoint files. Shared server/provision/Compose/environment edits merge I21 → I18 → I22 → I24 → I23; I23 also requires I22 release contract. |
| Evaluation/exit | A1 can build early after P0-2/H1 contracts. A2 follows repaired candidate plus I16/I18; A3/A4 then run in parallel with required owner participation/authorization; A5 follows all evidence and fixes. |

H5 pure matching, H10 freshness foundation, I5 excerpts and disjoint operations work can proceed alongside the owner harness after their listed contracts exist. “Parallel” never overrides shared-file ownership or a row's dependencies.

## Finding coverage and exit record

The lead maintains a per-ID closure/evidence ledger for A5; the following are fix owners by unit, not claims of closure. Aliases in review §3 stay attached to their canonical finding.

| Review findings | Owning units |
|---|---|
| R1-01, R1-02/03, R2-04 | H1/H2/H3/H4/H12 |
| R1-04, R1-05, R1-12, R2-05 | H5/H6 |
| R1-06, R3-06 | H13 |
| R1-07, R1-08, R1-15 | H2/I13 |
| R1-09, R4-11 | I15/I16 |
| R1-10 | I14 |
| R1-11, R2-09 | Q3/H7 |
| R1-13, R2-14 | H3 |
| R1-14, R3-10 | Q1 |
| R1-16, R2-15 | A1/A2/A3 |
| R2-01/02, R2-06 | H10/H11 |
| R2-03 | H9 |
| R2-07/13 | H8 |
| R2-08 | H4/H12/H13 |
| R2-10/11 | Q5/I27 |
| R2-12 | Q4 |
| R2-16 | I17 |
| R2-17, R3-16 | Q2/I27/I28 |
| R3-01, R3-08/13 | I1/I10/I11 |
| R3-02/05/11/14 | I7/I8/I9 |
| R3-03/04/09 | I2/I3/I4 |
| R3-12/15 | I12; I5/I6 |
| R4-01/06 | I19/I20; I21 |
| R4-02/03/04/07/08 | H1–H6/A1–A3 (aliases retained from the synthesis) |
| R4-05/12/13 | I22/I23; I18; I24 |
| R4-09/10 | I25/I26 |
| R4-15/16/17 | I32; I29/I30; I31 |
| R4-14 | Explicitly deferred: separate multi-owner/replica capacity, quotas, rollout and distributed-throttling entry gate before another owner or replica. |

A5 reconciles this table with every raw-report ID, current CI and full eval results, owner findings, M04/M05 live/trial obligations and operational evidence. Any accepted exception or deferral is explicit and owner-reviewed; no unresolved critical/high finding is silently carried into acceptance. No unit completion, deployed behavior or live verification is asserted here.

