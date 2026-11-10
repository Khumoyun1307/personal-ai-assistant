# Current release and privacy record

Updated **2026-11-09**, stabilization I32 first pass (D-S13/D-S15). Repository evidence plus owner-supplied lead deployment log and content-free release verifier output, 2026-11-09; no server, published page or real data accessed by this editor. Maintain after each release, settings push, policy publication and acceptance decision; preserve dated history in milestone reports. A merge is not a deployment.

## Production: last recorded, not live-verified

| Item | Last recorded value | Source and date |
|---|---|---|
| Production SHA | **`70584c7`**, deployed 2026-11-09 ~16:05 UTC: #235 digest default off plus the 2026-10-30 playtest fixes #227–#234. Re-check with `just prod-status <ip>`. | Lead deployment log, 2026-11-09; healthcheck OK. |
| Pre-deploy backup | `20261109T160314Z`, off-site synced. | Lead deployment log, 2026-11-09. |
| Alembic revision | **`0014`**. | Release verifier output, 2026-11-09. |
| Enabled flags | `actions.send`, `assist.drafts`, `digest.daily`, `monitors.run`, `notifications.deliver`, `summary.refresh`, `tool.create_monitor`, `tool.delete_contact_data`, `tool.delete_conversation_data`, `tool.delete_everything`, `tool.forget`, `tool.get_activity`, `tool.get_attention_list`, `tool.get_conversation`, `tool.get_draft`, `tool.get_status`, `tool.list_conversations`, `tool.list_memories`, `tool.list_monitors`, `tool.pause_autonomy`, `tool.propose_message`, `tool.propose_reply`, `tool.remember`, `tool.resolve_contact`, `tool.resume_autonomy`, `tool.revise_draft`, `tool.search_messages`, `tool.set_contact_priority`, `tool.set_conversation_mode`, `tool.stop_monitor`, `tool.summarize_period`, `tool.update_preferences`, `triage.assist_trigger`, `triage.run`. | Lead deployment log and release verifier output, 2026-11-09 ~16:12 UTC: owner ran `prod-env-push` adding `tool.revise_draft` and `tool.get_draft`; app restarted. |
| Routes / models / efforts | All routes **`gpt-6-luna`**, no model overrides set. `LLM_OWNER_REQUEST_EFFORT` / `LLM_DRAFT_REPLY_EFFORT` were absent from production settings, so code defaults **`medium`** apply. Other source defaults: `summarize` / `triage` / `evaluate_monitor` effort `low`; remaining routes omit effort. | Lead deployment log / content-free settings evidence, 2026-11-09; [settings defaults](../src/assistant/config.py). D-S7 is a later comparison, not a model switch. |
| Release identity since I22 | **`deploy/release-state` does not exist on the server yet**: this deploy predates I22 (#247). The next deploy via the new flow creates it. | Lead deployment log, 2026-11-09; [deployment runbook](../deploy/runbooks/deploy.md). |

In an owner-authorized window, run `just prod-status <ip>` and `just prod <ip> p2-verify release`; record dated SHA, database revision, flags and route settings here. The verifier uses a fresh container with current Compose settings; also verify the running app was restarted ([runbook](../deploy/runbooks/deploy.md)).

## On main, not deployed

Production remains at **`70584c7` (#235)**. Local main snapshot: **`60dc71e` (#247), 2026-11-09**, also containing #248 and #249. **All changes below (#236–#249) are on main, not deployed.** #227–#234 and #235 are already deployed. “None” means no additional settings/flag step identified; code still needs an authorized deploy.

| PR / unit | Change | Settings push / flag action |
|---|---|---|
| [#236](https://github.com/Khumoyun1307/personal-ai-assistant/pull/236) Q1 | Read effective owner preferences. | Not enabled in production; add `tool.get_preferences` and push settings after the next deploy to offer the tool. |
| [#237](https://github.com/Khumoyun1307/personal-ai-assistant/pull/237) I21 | Overdue backup catch-up and restart-safe upload retry. | None; deploy updated backup service. |
| [#238](https://github.com/Khumoyun1307/personal-ai-assistant/pull/238) I19 | Keep AI processing quarantined after restore. | None; I20 reconciliation remains required. |
| [#239](https://github.com/Khumoyun1307/personal-ai-assistant/pull/239) I7 | Deliver held important alerts even with digest off. | None. |
| [#240](https://github.com/Khumoyun1307/personal-ai-assistant/pull/240) P0-1 | Record stabilization decisions and P3 hold. | None (docs). |
| [#241](https://github.com/Khumoyun1307/personal-ai-assistant/pull/241) Q4 | Report conflicting same-turn proposals truthfully. | None. |
| [#242](https://github.com/Khumoyun1307/personal-ai-assistant/pull/242) Q3 | Explain factual live-card state. | None; uses existing `tool.get_draft` flag. |
| [#243](https://github.com/Khumoyun1307/personal-ai-assistant/pull/243) I18 | Share a supported-settings inventory with production push. | No overrides set in recorded production settings; medium owner/draft defaults apply. Future overrides need a settings push/restart and verification. |
| [#244](https://github.com/Khumoyun1307/personal-ai-assistant/pull/244) I5 | Centre search excerpts on matching tokens. | None. |
| [#245](https://github.com/Khumoyun1307/personal-ai-assistant/pull/245) P0-2 | Specify contracts, cases and D-S15 hybrid recovery. | None (docs; design is not implementation). |
| [#246](https://github.com/Khumoyun1307/personal-ai-assistant/pull/246) Q2 | Make help, status and card headings truthful. | None. |
| [#247](https://github.com/Khumoyun1307/personal-ai-assistant/pull/247) I22 | Deploy an explicit CI-verified SHA with recorded artifacts. | No flag; follow selected-release protocol and rollback limits. |
| [#248](https://github.com/Khumoyun1307/personal-ai-assistant/pull/248) Q5 | Record card edits as shown only after successful presentation. | None. |
| [#249](https://github.com/Khumoyun1307/personal-ai-assistant/pull/249) H5 part A | Use unique real contact matches without unnecessary questions. | None; durable request binding remains later work. |

Open stabilization PRs are **not deployed**; once merged, classify them as **on main, not deployed** until deployment. Open PR numbers/status could not be refreshed because GitHub was unreachable during this edit; the table lists locally confirmed merges only.

## Privacy/legal: open owner compliance item

| State | Evidence / consequence |
|---|---|
| Code policy **v5** | [accounts/service.py](../src/assistant/accounts/service.py), inspected 2026-11-09. Unchanged by I32. |
| Approved policy **v6**, pending publication | [Policy status/history](legal/privacy-policy.md), 2026-10-30: v6 covers M04/M05 processing not named in v5; v5 published 2026-10-26. **The published policy may still be v5 while M04/M05 processing is enabled.** Publication and notice were not live-checked. This compliance gap belongs to the owner and is not resolved by this record. |
| D-S15 / I20 journal | [D-S15](engineering/stabilization/DECISIONS.md) / [C9](engineering/stabilization/contracts/C9-privacy-recovery-options.md), 2026-11-09: encrypted ids-only deletion/withdrawal metadata in existing Backblaze B2. Same storage does not waive policy/notice review. Applicability is **open; journal use is not cleared**. |

| Owner action | Required result |
|---|---|
| Publish approved v6 | Follow [PV6 §3.4](engineering/p3/specs/PV6.md#34-order-and-gates-od-24-od-25): publish at the existing URL, fill the publication date, verify the live page says v6, record dated evidence. I32 does not authorize publication. |
| Align version and notice | Authorize a separate code/release change aligning `POLICY_VERSION` and the approved notice with publication; deliver and verify the owner notice with opt-out under [AGENTS §16](../AGENTS.md#16-documentation-rules). Record resolution of the already-enabled M04/M05 scope gap; a future notice does not establish past compliance. |
| Check journal before I20 use | Review ids, purpose, retention, export/deletion and replay against the policy with I20; record whether approved text covers retained privacy metadata. Any policy edit needs a version bump; processing-scope growth needs owner notice with opt-out before use. Do not silently amend approved v6. |
| Verify release and settings | Select/authorize the release; verify runtime values above. Review `tool.get_preferences` enablement and any previously omitted overrides before settings push/restart. |
| Review acceptance before P3 | Complete/review the linked milestone and Phase 4 gates. P3 remains on hold; publication alone does not enable it. Later P3 activation also requires PV6's publication/notice waiting order. |

## Acceptance and open risks

| Item | Authoritative link / next evidence |
|---|---|
| M04 in progress | [Acceptance](milestones/M04.md#acceptance-r1-exit-implementation_plan-6): live checks, isolated restore, seven-day trial, owner review; [open items](milestones/M04.md#known-issues-and-open-items) and owner eval/language review. Historical flag-off/trial instructions are not a current production snapshot. |
| M05 in progress | [Acceptance](milestones/M05.md#acceptance-implementation_plan-7), [report/open items](milestones/M05.md#report-filled-at-cp6): confirmed E3 labels/model choice, live performance/monitors, five digest days, owner review. |
| Stabilization / P3 hold | [Phase 4](engineering/stabilization/PLAN.md#phase-4--audit-and-acceptance), [acceptance thresholds](engineering/stabilization/README.md#acceptance-gates). No completed acceptance or P3 resume claimed. |
| R4-15: release/privacy drift | [Finding](engineering/stabilization/reviews/r4-industrial.md#r4-15--documentation-does-not-provide-a-dependable-current-release-picture). Deployment/settings snapshot recorded from the 2026-11-09 lead evidence; publication and notice remain open. |
| R4-01: restore privacy | [Finding](engineering/stabilization/reviews/r4-industrial.md#r4-01--restoring-a-backup-can-restore-revoked-consent-and-deleted-content). I19 quarantine alone cannot reconcile withdrawals/deletions; I20 and recovery evidence remain required. |
| R4-05 / R4-12: release/settings | [Release finding](engineering/stabilization/reviews/r4-industrial.md#r4-05--deployment-is-not-a-reproducible-release-or-a-bounded-rollback), [settings finding](engineering/stabilization/reviews/r4-industrial.md#r4-12--the-supported-environment-push-drops-supported-runtime-settings). I22/I18 are on main, not deployed; next-flow release-state creation and production verification remain open. I23 implementation adds a central compatibility registry for immutable applied revisions and in-file markers for new migrations, drain/backup sequencing, health/worker readiness gates and schema-aware recovery. Its review fix records the exact completed dump basename through atomic backup/deployment markers, preserving `last_success` completion-time readers; restore no longer derives filenames from completion timestamps. This remains unverified work, not a deployed release. Lead container tests, `just smoke-recovery`, CI and an authorized production recovery check remain required; see the deployment runbook. |
| Remaining findings | [Review IDs](engineering/stabilization/review-2026-11-09.md), owning units in [PLAN](engineering/stabilization/PLAN.md). Merges alone do not establish complete journey or recovery acceptance. |
