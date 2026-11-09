## 1. Summary

This is a substantial, safety-conscious prototype, but it is **not industrial-ready**.
The modular monolith, provider port, durable inbox, explicit action lifecycle, and recovery jobs are sound foundations.
The weakest area is preserving the owner’s intent across clarification turns; deterministic safety checks cannot compensate for a drafter receiving the wrong task.
The repository has extensive tests, but production-like conversational evaluation and completed acceptance evidence lag behind implementation.
I found concrete gaps in restore privacy, backup catch-up, deployment reproducibility, configuration management, and incident diagnosis.
One preliminary diagnosis is outdated: owner runs **already receive live card IDs and states**, although that support remains incomplete.
There is no demonstrated need for microservices or a wholesale rewrite.
**Hold P3 implementation until a focused stabilization phase closes the conversational failures and M04/M05 acceptance gaps.** This is a recommendation, not a change to the approved plan.

## 2. Findings

### R4-01 — Restoring a backup can restore revoked consent and deleted content

**Severity:** high  
**Category:** privacy

**Evidence**

- `src/assistant/privacy/post_restore.py:3–5` explicitly acknowledges: “pauses, consent changes, deletions, approvals — [are] undone.”
- `post_restore.py:83–85` pauses autonomy, while the remaining procedure cancels actions, deletes queued jobs, recomputes schedules, records gaps, and sweeps retention. It does not quarantine restored AI consents or reconcile post-backup deletions.
- `src/assistant/llm/consent.py:65–67` authorizes processing from the consent rows currently in the database.
- `post_restore.py:189–209` removes the restore gate before the separately committed retention sweep finishes.

**Failure scenario:** A backup contains active consent and a stored conversation. The owner subsequently revokes consent and deletes that conversation. Restoring the backup recreates both. Once `post-restore` completes, subsequent AI work can use the restored active consent and content without the owner renewing authorization.

**Root cause:** Restore safety protects outbound actions but does not establish a trustworthy privacy state after database rewind. Normal retention cannot reconstruct deletion requests lost with the database.

**Recommended fix:** Quarantine AI processing after restore until consent and deletion state have been reconciled. Preserve a recovery mechanism for revocations/deletions outside the snapshot being restored, or require explicit recovery decisions before processing resumes. Keep the restore gate until all mandatory recovery steps succeed. Add synthetic tests covering backup → revoke/delete → restore → attempted AI processing.

**Touches:** AGENTS §6.7, §6.9; §9 deletion/consent requirements; SEC-09; NFR-09.  
**Confidence:** confirmed by reading code; no production restore was performed.

---

### R4-02 — Clarifications lose the trusted drafting task

**Severity:** high  
**Category:** owner-UX / correctness

**Evidence**

- `src/assistant/agent/tools/replies.py:580–598`: owner dictation is recognized only from `turn.own_words`; earlier assistant suggestions are searched, but earlier owner dictation is not.
- `replies.py:399–406`: the drafting instruction comes from the **current** owner turn; model guidance is removed when `ctx.tainted`.
- `src/assistant/agent/context.py:42,117–118`: up to 20 historical turns participate in inherited taint.
- `src/assistant/agent/draft_context.py:107–110` describes precisely this current-turn-only handoff.

**Failure scenario:** The owner supplies exact text, answers a recipient clarification, then says “just write it.” The orchestration model remembers the earlier text, but the deterministic provenance check rejects it as current dictation. The separate drafter receives the clarification instead of the original task; inherited taint can also remove the orchestration model’s explanatory guidance.

**Root cause:** Conversation history is being used as task memory, while the trusted boundary accepts only the latest turn. There is no durable, provenance-preserving pending composition task.

**Recommended fix:** Represent an unfinished composition explicitly: original owner turn/span, exact text or requested substance, recipient selection, reply/new-message mode, and clarification status. Let clarification turns update that task without replacing its content. Preserve taint protections; do not solve this by trusting model summaries of mixed history.

**Touches:** AGENTS §6.3, §6.11; owner decisions PF-10/PF-11 and D2.  
**Confidence:** confirmed by reading code.

---

### R4-03 — An underspecified request is deliberately converted into invented drafting intent

**Severity:** high  
**Category:** owner-UX / correctness

**Evidence**

- `src/assistant/agent/prompts/owner_request.v10.md:40`: an explicit request to reply, draft, or write “is always carried out.”
- `src/assistant/agent/prompts/draft_owner.v3.md:8–10`: where the request is silent, use preferences and “what the conversation needs”; “With no request, write the reply the conversation calls for.”
- `src/assistant/agent/draft_context.py:139–145` defaults reply mode to the contact’s latest messages.

**Failure scenario:** “Write to X” produces a chat-derived reply even though the owner has supplied neither message content nor a topic. “Write a new message” can still leave the actual subject unspecified.

**Root cause:** Rules designed to prevent an unwanted “no reply needed” refusal also eliminate the distinction between permission to draft and sufficient information to draft.

**Recommended fix:** Define and enforce missing-information behavior before invoking the drafter. Exact dictation, a requested reply, and a new composition require different inputs. For a new composition without substance, ask what to say. Add a structured “needs clarification” outcome rather than forcing every invocation to return message text.

**Touches:** AGENTS §6.11; the product/owner decision needs clarification before changing the mandated behavior.  
**Confidence:** confirmed by reading prompts and code.

---

### R4-04 — Quality evidence does not support expanding into autonomy

**Severity:** high  
**Category:** reliability / process

**Evidence**

- `docs/milestones/M04.md:72–79`: E2 reports 146/150 valid outputs, 23 unsupported commitments, intent matching 69/146, and 134 drafts awaiting owner ratings.
- `docs/evals/2026-10-30-owner-drafting-effort-medium-gpt-6-luna.md:12–14,32–36`: 31 cases, `draft_owner.v2`, no owner-confirmed items, informational metrics.
- The active prompt is `draft_owner.v3`; its recorded confirmation run evaluates only four cases (`…medium…trial-2.md:12–13`).
- `evals/owner_drafting.py:333–338` requires expected owner-dictated text to occur in the current turn. The dataset therefore excludes the exact multi-turn dictation failure described in this brief.
- `docs/milestones/M04.md:58–66` and `M05.md:55–61` leave live acceptance, restore rehearsal, trials, and formal confirmation pending.

**Failure scenario:** Local safety and routing tests remain green while real conversations repeatedly misunderstand the owner. P3 then adds autonomous execution on top of behavior whose intent accuracy is unproven.

**Root cause:** Implementation completion and per-rule correctness have advanced faster than evaluated task completion and owner acceptance.

**Recommended fix:** Make stabilization a release gate: synthetic conversations that actually execute clarification turns, retain task state, change languages, encounter stale cards, receive interleaved contact messages, and recover from restart. Run the complete candidate configuration, review the outputs, and complete the existing M04/M05 acceptance evidence before autonomy work proceeds.

**Touches:** AGENTS §3, §8, §11; ADR-07a’s quality consequence; P2 exit criteria and P3 S06.5.  
**Confidence:** confirmed by repository evidence. Missing records may exist elsewhere; they were not verified.

---

### R4-05 — Deployment is not a reproducible release or a bounded rollback

**Severity:** high  
**Category:** ops / reliability

**Evidence**

- `deploy/server.sh:75–85`: deployment pulls the current branch, re-executes the pulled script, then runs `compose up -d --build --remove-orphans`.
- `deploy/runbooks/deploy.md:31–35`: rollback means reverting on `main` and deploying again; owner verification follows deployment.
- `deploy/compose.prod.yml` uses the mutable `assistant:prod` image and runs migration as a separate service.
- `.github/workflows/ci.yml:141–144`: the aggregate `check` job covers lint and test shards; deployment-script and image jobs are separate.

**Failure scenario:** The approved revision differs from `main` when deployment runs; rebuilding depends on package repositories being available. A migration fails or introduces an incompatible schema while the previous app may still be running. Recovery requires another source change and build, potentially against an already changed schema.

**Root cause:** Deployment follows a development branch rather than a selected release artifact. Migration compatibility, health verification, and rollback are operator procedures rather than one enforced protocol.

**Recommended fix:** Deploy an explicit SHA and immutable image; verify all required CI results for that SHA. Define migration compatibility or stop/drain sequencing, wait for health and structural release checks, retain the previous image, and document schema-aware rollback/forward recovery. Include deployment and image jobs in the required release gate.

**Touches:** AGENTS §12, §15; NFR-08/09.  
**Confidence:** confirmed by reading scripts; actual deployment sequencing was not executed.

---

### R4-06 — Backup scheduling misses downtime and loses upload-retry state on restart

**Severity:** high  
**Category:** reliability / ops

**Evidence**

- `deploy/backup/backup.sh:140–147`: `upload_pending=0` is initialized in memory; a dump runs only when the current UTC hour equals the configured hour.
- `backup.sh:155–165`: pending upload retries exist only in that process.
- `docs/PRODUCT_REQUIREMENTS.md:615`: RPO ≤24 hours.

**Failure scenario:** The backup sidecar is unavailable during the scheduled hour and returns afterward. It waits until the next day instead of producing a catch-up backup. Separately, a restart after an upload failure forgets the hourly retry obligation.

**Root cause:** The backup loop schedules from the wall-clock hour and volatile flags rather than durable last-success/due state.

**Recommended fix:** At startup and each loop, determine whether a successful backup is overdue and catch up once. Derive upload retry work from durable markers/files. Test missed-hour startup and restart between successful dump and failed upload. Record measured restore time and off-site recovery evidence.

**Touches:** NFR-09; ADR-15; AGENTS §13’s durable-work principle.  
**Confidence:** confirmed by reading code.

---

### R4-07 — Contact resolution manufactures ambiguity around a sufficiently strong result

**Severity:** medium  
**Category:** owner-UX

**Evidence**

- `src/assistant/conversations/read.py:594–599` returns the ranked list, including weak short-form matches beside stronger matches.
- `src/assistant/agent/tools/conversations.py:367–384` returns those candidates; its special weak-match warning checks the **top** result.
- `owner_request.v10.md:58` instead asks for confirmation if **any** candidate is weak.

**Failure scenario:** One observed contact matches the supplied first name, but an unrelated short-form candidate is also returned. The prompt requires another question. A later request repeats the resolution because the chosen identity has not become durable task state.

**Root cause:** Ranking, ambiguity policy, and dialogue instructions use different definitions of ambiguity.

**Recommended fix:** Return a deterministic resolution classification: resolved, genuinely ambiguous, or suggestions only. Suppress irrelevant weaker alternatives when stronger evidence uniquely resolves the request. Preserve an owner-confirmed recipient within the pending task; persistent aliases require a separate owner-controlled design.

**Touches:** FR-DRF-08; AGENTS §6.11/12.  
**Confidence:** confirmed by reading code and prompt.

---

### R4-08 — Live card context exists, but remains a partial snapshot

**Severity:** medium  
**Category:** correctness / owner-UX

**Evidence**

- `src/assistant/agent/runtime.py:418–419` loads live cards at run start.
- `src/assistant/actions/activity.py:251–273` selects only live cards.
- `src/assistant/agent/context.py:125,141` displays at most 20.
- `owner_request.v10.md:48,50,57` requires tool-backed draft facts and warns against inferring content.
- `tests/scenarios/test_pf_owner_context_and_prompt.py:70,93` tests live-card context and its disappearance when no longer live.

**Failure scenario:** A card becomes outdated during a run, or an older terminal card is the subject of a follow-up. The model sees an earlier conversational claim plus a snapshot that omits the terminal transition and contains no text. It can still narrate stale or invented draft properties.

**Root cause:** Current state is partly supplied, but conversational claims are not bound to a fresh action version and read result.

**Recommended fix:** Use fresh `get_draft`/activity results for status and content claims, with action/version identifiers. Refresh after mutations and relevant races. Prefer deterministic presentation of action outcomes over unconstrained model narration.

**Touches:** AGENTS §6.11.  
**Confidence:** snapshot limitations confirmed; a specific unsupported claim remains model-dependent.

---

### R4-09 — Monitoring measures liveness better than successful service

**Severity:** medium  
**Category:** observability / reliability

**Evidence**

- `src/assistant/jobs/tasks.py:371–380`: the external heartbeat depends on a fresh poller heartbeat.
- `src/assistant/jobs/app.py:45–49` separates system, outbound, and LLM workers.
- `src/assistant/inbox/backup_check.py:5–7` checks backup freshness once daily.
- `docs/milestones/M05.md:74–76` records unresolved system-alert claims and the absence of an operator view.

**Failure scenario:** Polling remains healthy while LLM jobs consistently fail, outbound work accumulates, or important alert claims remain unresolved. The external monitor continues receiving pings.

**Root cause:** Process/worker liveness is stronger than workload-success monitoring. Existing timing queries and status views are mostly inspected manually.

**Recommended fix:** Add content-free service indicators: queue oldest age, terminal failure rates by fixed code, unanswered owner turns, stale claimed notifications, retention completion, backup/upload age, disk pressure, and end-to-end synthetic completion. Alert on sustained degradation independently of conversational usefulness ratings.

**Touches:** AGENTS §6.11; ARCHITECTURE §17; NFR-04/05/08/09.  
**Confidence:** confirmed by inspected monitoring code; actual alert delivery is unverified.

---

### R4-10 — Persistent traces cannot fully explain a wrong-draft incident

**Severity:** medium  
**Category:** observability

**Evidence**

- `src/assistant/agent/models.py:79–95`: steps retain tool name, argument digest, status, and latency.
- `src/assistant/agent/runtime.py:589–598` persists only those fields.
- `src/assistant/agent/tools/replies.py:447–453` already computes useful structural facts such as instruction source, mode, and target presence.
- `src/assistant/agent/tools/executor.py:169–176` puts diagnostic details into logs rather than the persistent step schema.

**Failure scenario:** Weeks later, a developer can establish that `propose_reply` succeeded, but cannot reliably establish which original owner instruction was used, whether clarification replaced it, which card version the model observed, or which structural decision discarded guidance.

**Root cause:** Privacy-safe traceability is treated primarily as accounting and log messages, not an incident reconstruction model.

**Recommended fix:** Persist a bounded, typed decision trace containing IDs/enums only: original task turn, clarification turns, selected recipient, mode, instruction source, taint source categories, child draft run, observed action/version, and fixed outcome reasons. Provide one content-free incident report command. Reproduce semantic failures using newly authored synthetic scenarios.

**Touches:** AGENTS §9 logging/content restrictions and §6.11; ARCHITECTURE §17.  
**Confidence:** confirmed by reading schemas and recording paths.

**Current debugging limit:** The existing run, step, call, action, and audit records can reconstruct execution and delivery, but cannot prove that the draft expressed the owner’s intended meaning without owner judgment or a synthetic reproduction.

---

### R4-11 — The USD budget is incomplete and does not safely support route upgrades

**Severity:** medium  
**Category:** cost / reliability

**Evidence**

- `src/assistant/llm/metering.py:28–30`: only Luna has a known price.
- `metering.py:49–50`: unknown models return `None`.
- `src/assistant/agent/persistence.py:99–105`: monthly spend sums recorded estimates; null estimates do not contribute.
- `metering.py:3–5,69–76`: failed calls are not recorded by the wrapper.
- `src/assistant/agent/runtime.py:567–574`: budget checks compare already-recorded spend against the limit.
- `evals/live.py:32–35` explicitly disables SDK retries because invisible attempts would escape the eval ledger.

**Failure scenario:** A higher route is configured after a quality miss, but its usage has no USD estimate. Alternatively, concurrent work begins below the remaining budget, or a billed request loses its response. The ledger understates expenditure.

**Root cause:** Retrospective successful-call accounting is being used as a hard budget boundary.

**Recommended fix:** Require a price definition for enabled models, reserve bounded estimated spend before calls, reconcile actual usage afterward, and record attempts whose billing is uncertain. Make the permitted overshoot explicit. Keep provider dashboard limits as an additional boundary.

**Touches:** ADR-07a; AGENTS §8 metering; NFR-10.  
**Confidence:** confirmed by reading code; actual billing discrepancies were not checked.

---

### R4-12 — The supported environment push drops supported runtime settings

**Severity:** medium  
**Category:** ops / correctness

**Evidence**

- `src/assistant/config.py:86–92,114–123` supports route-specific models and drafting/triage/monitor effort settings.
- `deploy/oci/push-env.sh:35–36,172–177` includes only a narrower fixed list, including owner-request effort but excluding those settings.
- `deploy/runbooks/deploy.md:26–28` directs operators to manage production environment through `prod-env-push`.

**Failure scenario:** An operator configures a route upgrade or drafting effort using a supported application setting, then runs the prescribed push operation. The setting is omitted and defaults silently apply.

**Root cause:** Application configuration and deployment configuration maintain separate, drifting inventories.

**Recommended fix:** Derive the deployment allowlist and validation from a reviewed configuration manifest. Add a structural test that supported production settings survive rendering. Report effective non-secret model/effort/flag settings after deployment.

**Touches:** AGENTS §9 environment management; ADR-07a.  
**Confidence:** confirmed by reading code.

---

### R4-13 — Secret and database privilege separation is too broad

**Severity:** medium  
**Category:** security

**Evidence**

- `deploy/compose.prod.yml` loads the same `../.env` into app, migration, and backup services.
- The app’s database URL uses the same PostgreSQL initialization identity.
- `deploy/oci/push-env.sh:124–127` generates that shared database identity.
- Production images correctly run as unprivileged OS users (`deploy/Dockerfile`, `deploy/backup/Dockerfile`), but this does not narrow injected credentials.

**Failure scenario:** Compromising the backup sidecar exposes application/provider credentials it does not need. Compromising the app exposes off-site backup credentials and a database identity with privileges broader than normal application work requires.

**Root cause:** A convenient single environment file doubles as every service’s credential bundle.

**Recommended fix:** Give each service only its required secrets. Separate migration, application, and backup database roles; restrict off-site credentials to the required operations. Preserve the existing no-secret-output and offline decryption-key safeguards.

**Touches:** AGENTS §9; SEC-03 and least-privilege security expectations.  
**Confidence:** shared credential injection confirmed; no exploit or secret exposure was demonstrated.

---

### R4-14 — Tenant-aware persistence is a foundation, not multi-user readiness

**Severity:** medium  
**Category:** reliability / maintainability

**Evidence**

- `src/assistant/db/repository.py` provides account-scoped repositories.
- `src/assistant/jobs/app.py:45–49` gives all work three shared queues with fixed concurrency.
- `src/assistant/telegram/gateway.py:163–189` keeps throttling state in process-local dictionaries.
- `src/assistant/config.py:71,97` makes the budget setting and release flags process-wide.
- `docs/ARCHITECTURE.md:1329` describes per-account budgets among day-one foundations.

**Failure scenario:** A busy account consumes shared LLM slots and delays another owner’s interactive requests. Adding outbound worker replicas bypasses the single process’s throttle coordination. A process-wide flag rollout changes every tenant at once.

**Root cause:** Tenant isolation has been addressed primarily as data scoping; fairness, quotas, rollout control, and replicated execution are still personal-instance assumptions.

**Recommended fix:** Before onboarding additional owners, introduce per-account admission limits and configuration, interactive/background prioritization, capacity tests, and a clear ownership model for distributed throttling. Audit unscoped job/reconciler entry points and database relationship integrity. Keep the modular monolith.

**Touches:** AGENTS §6.12; ARCHITECTURE §19; multi-user-without-rewrite objective.  
**Confidence:** implementation assumptions confirmed; cross-tenant performance effects are likely, not measured.

---

### R4-15 — Documentation does not provide a dependable current release picture

**Severity:** medium  
**Category:** docs / process / privacy

**Evidence**

- `docs/engineering/p2/README.md:5` still says M05 is on `int/m05`; `docs/milestones/M05.md:7` records its merge to `main`.
- `docs/milestones/M04.md:85,98–107` distributes open issues across PRs, phase decisions, and milestone amendments.
- `src/assistant/accounts/service.py:34` still sets `POLICY_VERSION = "v5"`.
- `docs/legal/privacy-policy.md:153` says v6 covers M04/M05 processing not named in v5 and remains pending publication.
- Inventory: **237 files under `docs/`, including 43,628 Markdown lines**. The P3 interface contract alone is 2,110 lines.

**Failure scenario:** A maintainer follows the documented entry point and misunderstands deployed flags, unresolved acceptance obligations, or the privacy-policy publication state. A supposedly closed incident is represented differently across several files.

**Root cause:** Historical records, future designs, live operations, and authoritative decisions coexist without a compact, maintained current-state index.

**Recommended fix:** Maintain one release record linking SHA, migration revision, effective flags/routes, published policy version, acceptance evidence, and outstanding risks. Mark plans as planned and reports as historical. Consolidate accepted behavior into the authoritative documents while preserving decision history. Resolve the M04/M05 policy-scope/publication discrepancy before further expansion.

**Touches:** AGENTS §2, §16; privacy notice requirements.  
**Confidence:** documentation inconsistencies confirmed; the actual published policy was not contacted.

---

### R4-16 — Complexity is concentrated in the product’s most failure-sensitive flows

**Severity:** medium  
**Category:** maintainability

**Evidence**

Static line counts include:

| Module | Lines |
|---|---:|
| `telegram/control/approvals.py` | 2,009 |
| `agent/drafting.py` | 1,372 |
| `telegram/control/handlers.py` | 1,295 |
| `telegram/control/panels.py` | 1,281 |
| `privacy/deletion.py` | 1,020 |
| `actions/executor.py` | 830 |

`handlers.py:719–853` contains a roughly 135-line card-press handler; `drafting.py:450–571` contains a roughly 122-line preparation method. Truncation/retry/schema-call machinery also recurs in `summarize.py:467`, `triage.py:451`, and `monitor_eval.py:590`.

**Failure scenario:** A fix to one draft origin, edit path, or callback lifecycle misses another. Reviewers verify local rules but overlook interactions among card presentation, revision, provenance, outdating, and recovery.

**Root cause:** Package boundaries remain useful, but several files have become application subsystems containing many independently evolving workflows.

**Recommended fix:** Refactor around stable responsibilities: trusted composition state, draft generation, action transitions, card presentation, revision orchestration, and callback dispatch. Preserve the proven executor protocol. Consolidate repeated LLM-call mechanics only where contracts are genuinely identical.

**Touches:** AGENTS §5/10; ARCHITECTURE §5.  
**Confidence:** confirmed by reading and static counts; size alone is not a correctness defect.

**Boundary assessment:** The inspected import contracts match the stated architecture, and a direct-import search found no prohibited imports in the listed domain packages. Import-linter was not run. I found no basis for replacing the architecture.

---

### R4-17 — Prompt retention lacks a managed lifecycle, although old versions are not the main problem

**Severity:** low  
**Category:** maintainability

**Evidence**

There are **23 versioned prompt files** and **six active keys**:

| Key | Active |
|---|---|
| owner_request | v10 |
| summarize | v7 |
| triage | v1 |
| evaluate_monitor | v1 |
| draft_reply | v1 |
| draft_owner | v3 |

- `src/assistant/agent/prompt_registry.py` contains both the active mapping and extensive change history.
- `agent/prompts/README.md:3–5` requires historical versions while retained runs reference them.
- `privacy/retention.py:12` retains agent-run records for one year.

**Failure scenario:** Future cleanup removes a still-referenced prompt or retains incompatible schema handling indefinitely because there is no explicit reference inventory.

**Root cause:** Retention is a manual convention rather than a manifest connecting prompt bytes, schemas, evaluations, and retained references.

**Recommended fix:** Keep immutable historical assets, but use a compact manifest with hashes, schema versions, active status, and associated eval evidence. Generate a content-free reference report before archival or deletion. An archive directory is reasonable if the loader still resolves retained labels. Do not delete versions based only on inactivity.

**Touches:** AGENTS §8 prompt immutability/retention.  
**Confidence:** confirmed by reading files; actual retained database references were not inspected.

Old prompt files are not all sent to the model and are not a meaningful runtime-cost problem.

## 3. Rule interactions

1. **Taint safety + current-turn provenance + recipient clarification → lost dictation.**  
   Each rule has a defensible purpose. Together, they remove the original owner instruction precisely when conversation requires another turn. A trusted pending task resolves this without weakening injection protection.

2. **“Always carry out a writing request” + chat-based fallback → unsolicited substance.**  
   Preventing a drafter from vetoing an explicit request also causes it to fill missing intent from the contact’s latest messages. Permission to compose must be distinguished from sufficient information to compose.

3. **Helpful fuzzy suggestions + “ask if any candidate is weak” → excessive clarification.**  
   Discovery results are being mistaken for an ambiguity decision.

4. **Asynchronous card outdating + conversational memory + model-written status prose → contradictory claims.**  
   Live-card context improves this, but a snapshot cannot replace fresh reads for claims about a particular version.

5. **At-most-once delivery + uncertain presentation claims → safe silence.**  
   The deliberate refusal to replay uncertain sends is correct. It must be paired with visible unresolved-state reporting; otherwise an important card or alert can disappear from the owner’s experience without creating a duplicate.

6. **Restore rewind + restored active consent + autonomy-only pause → privacy recovery gap.**  
   Preventing automatic replies after restore does not prevent AI processing or resurrected deleted content.

7. **Synthetic-only testing + scripted FakeLLM calls + narrow incident patches → strong mechanics, weak dialogue assurance.**  
   Synthetic-only data is appropriate. The missing ingredient is executing realistic synthetic conversations through several turns, rather than primarily supplying the tool choices the model ought to make.

8. **Owner-confirmed quality gates + informational deployment evidence + overlapping phase schedules → acceptance debt.**  
   The documents honestly mark many results unconfirmed. That honesty must affect release sequencing. Otherwise increasingly elaborate implementation outruns evidence of usefulness.

9. **Configuration-driven model upgrades + Luna-only pricing + deployment whitelist → ineffective quality escalation.**  
   A route can be supported by application settings yet be dropped by environment tooling or disappear from USD accounting.

10. **Detailed contracts and reviews + scattered amendments → high coordination cost without a clear product oracle.**  
    P2/P3 contain valuable safety work. P3’s 59 work units would nevertheless expand the state space before the basic owner drafting conversation is dependable.

The stabilization sequence should be: repair trusted conversational task state; add complete synthetic journey evaluation; improve structural incident traces; close restore, backup, deployment, and configuration gaps; then complete M04/M05 live acceptance and owner review. Reconsider P3 after those results.

## 4. Anything I could not verify

- No files were changed, and no tests, Docker commands, application runs, migrations, or network services were invoked.
- I did not read `.env`, `var/`, backups, production databases, or real Telegram content.
- Runtime test success, current CI results/durations, deployed SHA/flags, effective production settings, actual backup freshness, and external alert delivery remain unverified.
- The repository contains approximately **51,515 source Python lines** and **71,124 test Python lines**; test inventory includes 90 unit, 111 integration, and 24 scenario files. These indicate substantial investment, not proof of coverage or correctness.
- Existing scenarios cover important races, approval duplication, uncertain delivery, consent, restore safety, quiet hours, and DST. Real-model conversational behavior cannot be established from scripted fakes.
- Dataset sizes are E1 40 conversations, retrieval 38 queries, E2 150 situations, E3 150 bursts, monitors 30 cases, E4 80 cases, and owner drafting 31 cases. Multilingual coverage exists; owner-drafting coverage is narrower than the other sets.
- CI uses five PostgreSQL-backed test shards and includes linting, typing, import contracts, dependency auditing, backup smoke tests, and architecture builds. The roughly 15-minute duration comes from repository instructions, not a measurement in this review.
- I did not establish a cross-account data leak, consent-wrapper bypass during ordinary execution, duplicate business-send defect, or critical remote exploit.
- Public-repository controls are partly external settings. Their current interaction-limit expiry, branch protections, and outside-contributor approval settings cannot be verified from this checkout.
- Dependency vulnerability status, capacity limits, 99% availability, and the four-hour recovery target require current operational evidence.
- Apparent placeholders and blocked consent stubs are often intentional. I did not establish a substantial body of accidentally unreachable code.

## Readiness scorecard

Scale: **1 = prototype; 3 = dependable personal service; 5 = mature industrial product.**

| Area | Current level | Target | Main gap |
|---|---:|---:|---|
| Owner conversation and drafting | 1 | 4 | Intent survives clarification and interleaving |
| Architecture and boundaries | 3 | 4 | Decompose drafting/card hot spots |
| Ingestion durability | 4 | 4 | Current operational evidence |
| Business-send safety | 4 | 5 | Complete live acceptance and recovery evidence |
| Jobs and restart recovery | 3 | 4 | Workload completion visibility and recovery journeys |
| Privacy and consent | 3 | 5 | Restore-time revocation/deletion reconciliation |
| Security | 3 | 4 | Service credentials and database privilege separation |
| Observability and debugging | 2 | 4 | Persistent structural decision traces and service indicators |
| Testing mechanics | 4 | 4 | Preserve strong race and invariant coverage |
| Behavioral evaluation | 2 | 4 | Full multi-turn candidate evaluation and owner confirmation |
| Deployment and rollback | 2 | 4 | Immutable releases and schema-aware recovery |
| Backups and disaster recovery | 2 | 4 | Durable catch-up/retry state and measured restore |
| Cost and routing | 2 | 4 | Complete pricing, attempt accounting, configuration parity |
| Multi-user readiness | 2 | 4 | Fairness, quotas, rollout isolation, replicated throttling |
| Documentation and process | 2 | 4 | One current release picture and enforced acceptance |
| **Overall** | **2** | **4 before P3 expansion** | **Stabilize complete owner journeys and operational recovery** |