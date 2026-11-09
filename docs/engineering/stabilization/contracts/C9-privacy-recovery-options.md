# C9 — Post-backup privacy recovery contract (I20)

**Decided: D-S15, owner, 2026-11-09 — Option C (hybrid).** I20 implements an encrypted, ids-only journal of deletions and consent withdrawals next to the off-site backups in the **same Backblaze B2 storage already used for backups**, replayed after restore. This selects no new external processor. If journal continuity cannot be proven, documented manual reconciliation or a complete content purge is the fallback. I19 (#238) stays closed until either path completes; renewed AI consent alone is **not** reconciliation evidence. This is an implementation contract, not a claim of deployed journal recovery or authorization to access storage. I32 must check policy/notice treatment for retained privacy metadata before journal use.

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

Wrong restore/account, missing interval, unknown reconciliation or stale/integrity-failed evidence ⇒ false. Current source deletion, pending action cancellation, retention and renewed processor-specific consent all still apply. No automatic restored send. I20 must enforce reconciliation separately from fresh consent: deterministic consent entry points cannot clear unresolved privacy recovery, and reconciliation cannot renew consent. Preserve I19's quarantine semantics rather than treating its warning or a fresh grant as proof.

## Options retained as decision history

The table records the alternatives considered before D-S15; C is now binding.

| Option | Recovery / correctness | Operational trade-offs |
|---|---|---|
| **A — Independent privacy-decision journal** | Append account/scope revocations and deletion tombstones outside the snapshot failure boundary; reconcile continuous authenticated journal through restore cutoff before releasing quarantine. Fresh consent still required. | Best eventual unattended reconciliation; needs durable write/ack protocol, stable identifiers across restore, encryption, integrity, missing-tail detection, retention and key recovery. Same disk/snapshot is not independent. This alternative alone lacked C's mandatory fallback. |
| **B — Manual reconciliation** | Remain quarantined; owner/operator reissues all relevant post-backup deletion decisions or elects a conservative complete content purge, verifies receipts, then signs recovery evidence. Fresh consent separately. | No new external storage. Reliable only with explicit owner knowledge or complete purge; an “I don't remember” answer cannot certify selective recovery. Longer RTO, human involvement every restore; cannot claim unattended recovery. |
| **C — Journal plus mandatory manual fallback (selected)** | Replay a continuous independent journal when available; missing/stale tail never opens processing. Use documented manual reconciliation/complete purge when continuity cannot be proven. | Most resilient path, more implementation/runbook complexity. Journal failure remains visible and cannot silently downgrade to assumed reconciliation. D-S15 places the encrypted journal in existing off-site B2 backup storage. |

The original recommendation was C, using A's durable journal and B's fail-closed fallback. The owner accepted it in D-S15. Manual recovery remains an operational choice for an unresolved restore, not an open architecture decision.

## I20 journal contract (D-S15)

Strict journal record (encrypted privacy metadata only; no Telegram content, names, usernames, message fragments, secrets or model prose):

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
    processor_id: str | None  # validated processor identifier for withdrawal; NULL for deletion
    decided_at: datetime
    previous_digest: str
    digest: str
```

UUID strings are validated UUIDs; sequence is a positive integer; timestamps are aware UTC; digests are fixed-format integrity metadata. `account_recovery_id` scopes every operation, lookup, replay and export. Subject is required only for conversation/contact scope; a withdrawal must name its consent scope and processor and cannot become a grant on replay. IDs-only includes these fixed operation/scope enums, timestamps and integrity fields, never content. I20 fixes the storage serialization and stable-ID mapping before code, with each added local column's type/nullability/account/retention/cascade recorded in ARCHITECTURE §10; no copied text is permitted.

| Contract area | Binding I20 requirement |
|---|---|
| Write ordering | Privacy success receipt only after journal durability plus database application are settled; crash between stores is idempotently recoverable. Journal append failure must not report deletion/revocation as fully durable. Define pending/outage semantics; never reverse an already applied revocation. |
| Storage/key | Encrypted records and authenticated continuity manifest next to the off-site backups in existing Backblaze B2 storage, outside the database snapshot/host-loss boundary. Journal writes have their own durability acknowledgment; the daily backup/upload schedule cannot serve as a decision acknowledgment. Access scoped to required service; recoverable decryption/integrity keys remain outside the restored snapshot. Reuse existing backup storage, no new processor. Do not put subject/account IDs in plaintext object names or logs. |
| Identity | Stable recovery identifiers cannot collide with restored/reused integer IDs. Preserve mapping sufficiently to apply tombstones across supported backups; no names/text in journal. |
| Continuity | Account-scoped sequence/digest chain plus independently durable high-water mark, bound to the backup and replay cutoff. Missing suffix must be detectable; a hash chain alone cannot prove absence of missing tail. Serialize append/manifest advancement without holding database locks across B2 calls. Missing pending-write acknowledgment, unknown head or unsupported pre-journal snapshot requires manual fallback. Restore never rewinds or overwrites the off-site journal/head from the snapshot. |
| Replay | Validate/decrypt all required records and head, then idempotently apply deletions/withdrawals in sequence for the scoped account before reconciliation evidence is complete. Repeat replay/repeated restore cannot restore consent, reintroduce content or duplicate effects. A record for another account never affects this account. Cancel affected pending work and purge derived content through existing privacy cascades. No provider call or business send. |
| Retention | Tombstone cannot expire while **any restorable** snapshot can resurrect covered data, including late/off-site copies. Reconcile pruning receipts, not nominal “4 weeks”; I20 inventories restorable copies and defines a bounded prune schedule tied to verified last-copy expiry. Unknown copy inventory blocks journal pruning. Retained privacy metadata requires I32's policy/notice check: any policy edit requires a version bump; processing-scope growth requires owner notice with opt-out (AGENTS §16). Same B2 processor does not waive this check. |
| Erasure | Content deletion leaves only minimal necessary tombstones to enforce deletion across backups. Account/everything deletion semantics and final journal purge after last restorable snapshot require explicit documented policy. Never keep content as “evidence.” |
| Failure | Missing key, write outage, corrupt record or unknown cutoff keeps gate closed. No fallback to snapshot consents. Report content-free reason and manual recovery option. |

| Storage / consent class | Retention, export and deletion contract |
|---|---|
| Encrypted journal records and continuity manifest | Account-scoped privacy enforcement metadata with fields/types above; retain only while needed to suppress resurrection from restorable copies, then prune with verified receipts. Content deletion retains necessary tombstones; final account erasure includes journal cleanup after the last restorable copy expires. Export describes retained metadata without exposing keys/other accounts. No AI exposure and no ai_control/ai_managed grant needed to enforce deletion/withdrawal. |
| PrivacyRecoveryEvidence (local structural marker/audit) | Strict schema above, account + restore scoped; no content. I20 specifies its persistence in the existing restore/audit seam before code, with no unscoped tenant record. Retain content-free recovery audit under existing 1-year audit retention; scope/account deletion removes sensitive subject links without erasing required replay tombstones. Never reuse an old restore's evidence to open a new restore. No provider exposure. |

## I20 mandatory manual fallback

| Step | Evidence |
|---|---|
| Restore under I19 gate | Restore UUID and backup timestamp; no AI/queued outbound processing. |
| Establish deletions | Explicit reissued scoped deletions backed by owner knowledge, or conservative full content purge; deterministic deletion receipts with counts/IDs only. Unknown selective history stays quarantined. |
| Finish retention/recovery | Successful retention and cancelled/uncertain restored action receipts; failures leave quarantine. |
| Owner confirms reconciliation | Bound to restore/account/cutoff, no pre-restore replay accepted; cancellation preserves quarantine. |
| Fresh consent | Deterministic current consent flow, separately for control/managed, only after reconciliation gate permits it; consent never retroactively approves restored data use. |

I20's implementation PR must specify any additional local record/table/column and stable-ID mapping, types, nullability, account scoping, encryption, retention, export/deletion and tests in this contract/ARCHITECTURE before code. This is implementation detail within D-S15, not a pending owner choice between A/B/C. Required acceptance: synthetic snapshot→revoke/delete→restore→journal replay→attempted processing; lost/stale evidence; repeated replay/restore; multiple accounts; journal loss, missing tail, corrupt/key-unavailable records, partial writes and append outage; manual selective reconciliation and complete-purge fallback; retention failure and retry; owner cancellation; zero provider calls while unresolved; fresh consent alone cannot open I19, reconciliation alone cannot renew consent. Update architecture §10.4/§14.3/§15.6, runbooks and M02/M04 evidence, and coordinate I32's policy/notice check before journal use. A4 measures RPO/RTO for successful replay and human fallback separately; no unconditional unattended-recovery claim.
