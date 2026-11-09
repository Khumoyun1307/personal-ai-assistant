# C5 — Contact-resolution verdict (H5)

D-S5 separates identity evidence from fallback suggestions. This is deterministic application output; model prose and recency never settle identity. No permanent aliases are saved. Existing `match_contacts` may remain a ranked helper; callers consume the verdict, not “any weak candidate means ambiguity.”

```python
ResolutionKind = Literal["unique_strong", "ambiguous_strong", "weak_only", "none"]


@dataclass(frozen=True)
class Resolution:
    verdict: ResolutionKind
    strong: tuple[ContactMatch, ...]
    suggestions: tuple[ContactMatch, ...]
    selected_contact_id: int | None
    selected_conversation_id: int | None
    total_strong: int
    omitted: int
    reason: Literal[
        "exact_identity",
        "full_token",
        "token_prefix",
        "multiple_real",
        "typo_guess",
        "short_form_guess",
        "no_observed_match",
    ]


def resolve_contacts(
    directory: Sequence[DirectoryEntry], query: str, *, limit: int = 5
) -> Resolution: ...
async def bind_resolution(
    session: AsyncSession,
    *,
    account_id: int,
    request_id: int,
    expected_version: int,
    resolution: Resolution,
    owner_choice_turn_id: int | None,
) -> RequestUpdateResult: ...
```

## Evidence policy

| Evidence | Classification |
|---|---|
| Exact observed @username | Strong identity; leading @ handled; unique account-local observed identifier. |
| Full supplied name tokens match observed complete tokens (any order) | Strong; allow script/apostrophe/case normalization **for names only**. A supplied first name may fully match a token in two real names ⇒ two strong. |
| Token prefix | Strong only when each supplied token prefix is ≥3 normalized letters and matches distinct name tokens; count all qualifying contacts before pagination. Query “Kam” matching Kamola and Kamron is ambiguous regardless of recency. |
| Short-form nickname, fuzzy typo, nearest name | Weak suggestion; existing fuzzy threshold can discover candidates but never by itself establishes strong identity. Ask even if sole weak result. |
| Exact adequate name vs weak unrelated prefix/nickname | Unique strong wins; weak suggestions cannot veto it. |
| Several qualifying real identities | ambiguous_strong; no score/last-activity tiebreak selecting a person. Presentation ranking only. |
| No observed match | none; explain coverage and offer/search mentions as information. A mention in text is not a new eligible observed recipient. |

An exact username disambiguates weaker name-prefix evidence. Otherwise full-name exact evidence may narrow prefix-only alternatives when all supplied tokens exactly identify one full name; duplicate full names remain ambiguous. Do not truncate to `limit=1` before computing verdict. Missing conversation, bot chat or unavailable scope is not a sendable resolved recipient; return explicit unavailable at binding, not a guessed alternate.

## Tool result and model guidance

| Verdict | Fixed note / required behavior |
|---|---|
| unique_strong | “One observed contact matches. Use the selected IDs for this request; do not ask solely because weaker suggestions exist.” Bind selected IDs using C1 CAS. |
| ambiguous_strong | “Several real contacts match. Ask which of these contacts; none selected.” Show wrapped real names, no fabricated option; numeric answer maps to persisted/request-associated candidate IDs, not a reordered later list. |
| weak_only | “Suggestions only. Ask whether one is intended; none selected.” Owner choice binds only after fresh scope validation. |
| none | “No observed contact matches. Search mentions if useful; do not invent a recipient.” |

Existing `resolve_contact` arguments remain strict `name: str`, `limit: int|None`; optional `request_id: int|None`, `request_version: int|None` added together if binding through the tool. Otherwise a separate inert C1 update binds the result. Capability READ; existing flag default off, owner_request only, CONTROL_ONLY. Returning names is managed/untrusted and taints even for unique_strong. C1 selection stores **IDs and basis**, never alias/query/name text. Revalidate contact→conversation relation on use. Explicit reply-to-selected artifact beats implicit resolution; mismatch requires new request/explicit correction.

| Locale | Synthetic test input / directory | Required verdict |
|---|---|---|
| uz-Latn | `Kamola` → Kamola Ziyo; Kamariddin Nur | unique_strong; no weak-choice question |
| uz-Cyrl | `Камола Зиё` / `Зиё Камола` → Kamola Ziyo | unique_strong with same IDs |
| ru | `Камола` → Камола Зиё; Камола Сафо | ambiguous_strong |
| en | `Nora Vale` / `Vale Nora` → Nora Vale | unique_strong |
| mixed | `Shuxrat` ↔ `Шуҳрат` observed normalized full token | Same strong matching under tested current name fold, never a dictation fold |
| typo | `Kamloa` → Kamola Ziyo only | weak_only, ask |
| prefix | `Kam` → Kamola Ziyo; Kamron Safi | ambiguous_strong, even limit=1 |

No new content column/table. H5 adds `owner_requests.resolution_candidates jsonb NULL` (structural) because current `pending_inputs` has no general metadata column. Strict shape: `request_version: int`, `candidates: list[CandidatePair]`, where `CandidatePair = {contact_id: int, conversation_id: int|None}`; list order is displayed order, bounded to the tool limit, and selected pair is revalidated before binding. Never infer uniqueness from this truncated list. Expires with question/24 h and is cleared after selection; account/request/scoped deletion removes it. Model exposure needs the existing managed-name gate when accompanying contact candidates. Never place names/query text in JSON. Update architecture §8.2/§10.3/10.4, P2 D8/PF-03 supersession and multilingual journey/retrieval tests. Owner approval still occurs on recipient/full-text card regardless of unique resolution.
