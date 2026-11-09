# C8 — Effective trusted owner preferences (H13)

D-S2/D-S3/D-S6 distinguish **source authority** from whole-run taint. An explicit owner preference can be captured precisely even after reading a contact, while the saving tool still needs its tainted-run confirmation. Confirming a model paraphrase does not make contact-derived material a global instruction.

```python
class CapturePreferenceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: int | None
    request_version: int | None
    sources: list[SourceSpan]  # C1 exact owner spans; no messaging request required
    kind: Literal["preference", "style", "instruction"]
    scope: Literal["owner", "contact", "conversation"]
    contact_id: int | None
    conversation_id: int | None
    applies_to_replies: bool
    replaces_memory_id: int | None


@dataclass(frozen=True)
class EffectivePreference:
    memory_id: int
    content: str
    scope: Literal["owner", "contact", "conversation"]
    kind: Literal["preference", "style", "instruction"]
    applies_to_replies: bool
    verification: Literal["owner_source_exact", "legacy_unverified"]


async def capture_owner_preference(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int,
    tool_call_id: str,
    args: CapturePreferenceArgs,
    now: datetime,
) -> PreferenceReceipt: ...
async def effective_owner_preferences(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int | None,
    purpose: Literal["owner_request", "assist"],
    managed_allowed: bool,
) -> tuple[EffectivePreference, ...]: ...
```

Implement this as a strict source-backed variant of existing `remember`, retaining its CONFIGURE class, owner_request-only runs, CONTROL_ONLY tier, `tool.remember` default off, idempotency and Undo. Do not add an unregistered side channel. If a separate tool is necessary, a contract amendment must specify its flag/catalogue entries before implementation.

| Capture check | Required behavior |
|---|---|
| Authority | C1 span validator checks account, owner role, retained own-text field and source hash/offsets; never forwarded/quote/suggestion/model paraphrase. If request IDs are supplied, both must be present and sources belong to that request. Otherwise only the current owner turn or an explicitly owner-selected retained owner turn is eligible; no invented compose/reply task is needed to save a preference. Copy exact characters into existing `memory_items.content`, max existing 400 characters; no fuzzy proof or automatic summarization. |
| Applicability | Scope/FK pair strict; owner scope sets neither FK. `applies_to_replies` only for instruction; preference/style inherently usable for eligible reply style. Do not infer broad global applicability from contact note. |
| Confirmation | If model context contains untrusted text, existing confirmation shows copied full preference and applicability before save. Revalidate sources at apply time. Taint still gates mutation; source precision controls future instruction eligibility. |
| No source proof | Existing remember path stays conservative with run-origin provenance. Never reset all tainted/managed memories to trusted, including historical confirmed notes. |
| Result | `PreferenceReceipt(memory_id: int, saved: bool, scope, applies_to_replies: bool, effective_for: tuple[owner_chat\|selected_contact_replies\|all_replies,...], reason: fixed_code\|None)`; do not promise it affects all drafts if excluded. |

## Backward-compatible persistence

No new content column; `memory_items.content` remains the only saved preference text. Add strict optional fields to **existing `memory_items.provenance jsonb`**:

| Key | Type / nullability | Meaning |
|---|---|---|
| `owner_capture` | object NULL/absent for legacy | Exact-proof record below; absent does not establish trust. |
| `owner_capture.method` | Literal owner_source_exact, required | Set by application validation only. |
| `owner_capture.turn_ids` | list[int], required | Account-scoped source IDs; original snippets not copied here. |
| `owner_capture.source_spans` | list[SourceSpan], required | Validated C1 turn/field/start/end/hash references while source remains retained; no copied prose. |
| `owner_capture.content_sha256` | str, required | Integrity of exact stored memory content; not authorization alone. |
| `owner_capture.confirmed_at` | UTC datetime string NULL | Required non-NULL when tainted mutation was held; records owner confirmation. |

Existing `tainted`, `managed`, `refs` describe **the stored content's origins**, not unrelated read-tool activity, only on this proved path. Source-exact owner style may be `tainted=false, managed=false`; notes about/quoting a contact or mixed/unclassified material keep conservative flags and scopes. Do not strip genuine dependency refs. Confirmation receipt remains linked to the original tainted run for audit.

| Retention / deletion / consent | Requirement |
|---|---|
| Saved text | Existing curated-memory retention: until forgotten/deleted; archived 24 h Undo. Owner explicitly requests persistence; ordinary control-turn expiry may remove source references without deleting an independently saved exact preference. Proof records hash/method, not recoverable source prose. |
| Source reference cleanup | Remove dangling source/turn IDs when retention clears them; keep method + stored-content integrity + confirmation metadata as evidence of validation at capture. Never reload expired text. Request deletion does not resurrect/duplicate forgotten preference. |
| Scope deletion | Contact/conversation deletion follows existing scope and genuine origin refs. Managed-data deletion removes managed-origin items; independently captured owner-global style remains. Owner-control/everything/forget policies explicitly exercised in cascade tests; “everything” removes content/proof. |
| Consent | Provider reads need ai_control; managed origin additionally ai_managed. Scoped Assist still needs both consents for conversation content. Confirmation is no provider-consent substitute. |

## Effective loading and precedence

| Consumer | Inputs and limits |
|---|---|
| Main owner_request chat | Q1 current account settings + verified applicable owner preference/style/instruction source text; selected-contact/conversation preferences apply only to selected request. D-S6 allows multi-chat context but does not globalize scoped preferences. Content travels as provenance-tagged items, not concatenated into a trusted system prompt. |
| Proactive Assist | Existing `contact_facing` isolation: current conversation/contact items and eligible owner preference/style or applies_to_replies instruction. No other chat refs, no owner facts, no main transcript. Verified global style may be included despite unrelated historical run taint. |
| General owner memory | Legacy conservative placeholders remain; contact-derived/model-authored entries cannot become trusted instructions via this loader. |

Explicit current request constraints override saved defaults; scoped owner preference overrides owner-global defaults when applicable; account settings supply fallback. Conflicting same-scope saved instructions without explicit replacement require clarification rather than silently inventing a merge. `replaces_memory_id` validates account, archives old item and uses existing Undo atomically. Q1 `get_preferences` reads current structural account settings after panel/Undo changes; H13 must not invent a separate settings cache.

Update architecture §7.3/§8.2/§10.3/10.4 and M03 D70/M04 supersession notes. Tests: tainted run→exact owner preference→confirm→effective main/Assist; quoted injection cannot globalize; foreign source; scoped leakage; source expiry after independent capture; forget/Undo/replacement; consent-off and legacy provenance. H13 follows H4/H12; I12 follows its shared memory/registry edits.
