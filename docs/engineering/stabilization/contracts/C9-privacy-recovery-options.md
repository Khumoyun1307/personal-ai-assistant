# C9 — Post-backup privacy recovery options (I20)

**Owner choice required; no option is selected by this contract.** D-S11 approves fail-closed restore quarantine, not a journal location/service/retention policy. I19 (#238) already supplies quarantine and fresh consent entry points; it cannot reconstruct deletions missing from an old snapshot. Renewed AI consent alone is **not** evidence that deleted data was reconciled. No new processor, storage or external action is authorized here.

## Common recovery gate

```python
class PrivacyRecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restore_id: str  # application UUID, not operator prose
    backup_taken_at: datetime
    reconciled_through: datetime
    mechanism: Literal["independent_journal", "manual", "hybrid"]
    account_id: int
    deletion_reconciliation: Literal["complete", "unknown"]
    consent_reset: bool
    retention_complete: bool
    owner_confirmed_at: datetime | None
    journal_head: str | None


def recovery_may_open(
    evidence: PrivacyRecoveryEvidence | None, *, restore_id: str, account_id: int
) -> bool: ...
```

Wrong restore/account, missing interval, unknown reconciliation or stale/integrity-failed evidence ⇒ false. Current source deletion, pending action cancellation, retention and renewed processor-specific consent all still apply. No automatic restored send. An implementation must account for deterministic renewals after I19 without reopening processing until I20 reconciliation is complete; preserve I19's gate semantics rather than treating a warning as proof. Use a separate structural reconciliation marker if needed, approved with the chosen design.

| Option | Recovery / correctness | Operational trade-offs |
|---|---|---|
| **A — Independent privacy-decision journal** | Append account/scope revocations and deletion tombstones outside the snapshot failure boundary; reconcile continuous authenticated journal through restore cutoff before releasing quarantine. Fresh consent still required. | Best eventual unattended reconciliation; needs durable write/ack protocol, stable identifiers across restore, encryption, integrity, missing-tail detection, retention and key recovery. Same disk/snapshot is not independent. Existing storage might be reused only after design approval; no service selected. |
| **B — Manual reconciliation** | Remain quarantined; owner/operator reissues all relevant post-backup deletion decisions or elects a conservative complete content purge, verifies receipts, then signs recovery evidence. Fresh consent separately. | No new external storage. Reliable only with explicit owner knowledge or complete purge; an “I don't remember” answer cannot certify selective recovery. Longer RTO, human involvement every restore; cannot claim unattended recovery. |
| **C — Journal plus mandatory manual fallback** | Replay a continuous independent journal when available; missing/stale tail never opens processing. Owner chooses documented manual reconciliation/complete purge when continuity cannot be proven. | Most resilient path, more implementation/runbook complexity. Journal failure remains visible and cannot silently downgrade to assumed reconciliation. |

**Recommendation: C**, using A's independent durable journal for ordinary recovery and B's explicit fail-closed fallback. This is a recommendation for the owner to approve, not an implementation decision. If the owner prefers B now, that is viable with a documented manual RTO and no unattended-recovery claim.

## Journal design to approve if A/C chosen

Proposed record sketch (metadata only; **not an allocated database table/file or final schema**):

```python
class PrivacyDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    sequence: int
    decision_id: str  # UUID, idempotent append
    account_recovery_id: str  # stable opaque identity, never username
    scope: Literal["control", "managed", "conversation", "contact", "everything"]
    subject_recovery_id: str | None  # stable opaque ID, not reusable identity PK alone
    operation: Literal["delete", "revoke_ai_control", "revoke_ai_managed"]
    decided_at: datetime
    previous_digest: str
    digest: str
```

| Question for implementation design | Required property / trade-off to approve |
|---|---|
| Write ordering | Privacy success receipt only after journal durability plus database application are settled; crash between stores is idempotently recoverable. Journal append failure must not report deletion/revocation as fully durable. Define pending/outage semantics; never reverse an already applied revocation. |
| Storage/key | Outside restored snapshot and host-loss boundary; authenticated/encrypted, access scoped to required service. New vendor would require §9 approval, policy and consent text before any data flow. Owner keeps recoverable key outside snapshot. |
| Identity | Stable recovery identifiers cannot collide with restored/reused integer IDs. Preserve mapping sufficiently to apply tombstones across supported backups; no names/text in journal. |
| Continuity | Sequence/digest chain plus independently durable high-water mark; missing suffix must be detectable. Hash chain alone cannot prove absence of missing tail. |
| Retention | Tombstone cannot expire while **any restorable** snapshot can resurrect covered data, including late/off-site copies. Reconcile pruning receipts, not nominal “4 weeks”; propose a bounded schedule only after inventory. Journaling policy is retained privacy metadata and must be disclosed. |
| Erasure | Content deletion leaves only minimal necessary tombstones to enforce deletion across backups. Account/everything deletion semantics and final journal purge after last restorable snapshot require explicit documented policy. Never keep content as “evidence.” |
| Failure | Missing key, write outage, corrupt record or unknown cutoff keeps gate closed. No fallback to snapshot consents. Report content-free reason and manual recovery option. |

## Manual design to approve if B/fallback chosen

| Step | Evidence |
|---|---|
| Restore under I19 gate | Restore UUID and backup timestamp; no AI/queued outbound processing. |
| Establish deletions | Explicit reissued scoped deletions backed by owner knowledge, or conservative full content purge; deterministic deletion receipts with counts/IDs only. Unknown selective history stays quarantined. |
| Finish retention/recovery | Successful retention and cancelled/uncertain restored action receipts; failures leave quarantine. |
| Owner confirms reconciliation | Bound to restore/account/cutoff, no pre-restore replay accepted; cancellation preserves quarantine. |
| Fresh consent | Deterministic current consent flow, separately for control/managed, only after reconciliation gate permits it; consent never retroactively approves restored data use. |

No new persistent field is allocated until the owner chooses. I20 must then specify every record/table/column, types, account scoping, encryption, retention, export/deletion and tests in a C9 amendment before code. Required acceptance regardless of option: synthetic snapshot→revoke/delete→restore→attempted processing; lost/stale evidence; repeated restore; multiple accounts; journal loss/partial writes if applicable; retention failure and retry; owner cancellation; zero provider calls while unresolved. Update architecture §10.4/§14.3/§15.6, runbooks and M02/M04 evidence. A4 measures RPO/RTO under the chosen human/automatic recovery constraints.
