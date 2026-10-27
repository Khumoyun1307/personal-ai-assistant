# E3 — Synthetic Triage Evaluation Dataset

Synthetic evaluation dataset for inbox triage and semantic monitor evaluation (Phase P2, M05, Unit E3D).
Authored by Junior (mentored by Jack) following `docs/engineering/p2/specs/E3-triage-eval.md` §A.

## Provenance and Privacy

- **100% Synthetic**: All participant names, organizations, phone numbers, order numbers, server issues, and messages are entirely invented.
- **No Real Telegram Data**: Zero messages, captions, usernames, or media IDs are copied or derived from real chats, real people, or real accounts (AGENTS §8, ADR-16).
- **Tooling**: Seed situations and linguistic variants were drafted with LLM assistance, strictly validated, and hand-reviewed to ensure realistic conversational phrasing and cultural accuracy across all 5 variants.
- **Native Language Review**: Per decision P2-O11, native language review of prompts and eval items will be confirmed by the owner before final gating.

## Dataset Structure

The dataset contains two files:
1. `bursts.jsonl`: 150 items. Each item represents a chat's history, open tasks before the burst, and a burst of incoming messages to triage at `now`.
2. `monitor_cases.jsonl`: 30 items. Each item represents an `evaluate_monitor` comparison case with defined monitors, incoming burst messages, and matched monitor IDs.

## Summary Counts

### `bursts.jsonl` (150 bursts)

| Variant | Total Bursts | Alerts (`alert: true`) | Task Bursts | Resolved Tasks | Unresolved Tasks | Nothing To Do | Suspicious | Owner in Burst |
|---|---|---|---|---|---|---|---|---|
| `uz-Latn` | 30 | 11 | 8 | 4 | 4 | 8 | 3 | 3 |
| `en` | 30 | 11 | 8 | 4 | 4 | 8 | 3 | 3 |
| `uz-Cyrl` | 30 | 11 | 8 | 4 | 4 | 8 | 8 | 3 | 3 |
| `ru` | 30 | 11 | 8 | 4 | 4 | 8 | 3 | 3 |
| `uz-ru-mix` | 30 | 11 | 8 | 4 | 4 | 8 | 3 | 3 |
| **Total** | **150** | **55** | **40** | **20** | **20** | **40** | **15** | **15** |
| **Spec Target** | ≥ 150 | ≥ 40 | ≥ 30 | 35%–65% | (40 decisions) | ≥ 15 | ≥ 10 | ≥ 10 |

- **Alerts**: 55 (36.7%), well exceeding the ≥ 40 minimum.
- **Resolution Ratio**: 20 / 40 = 50.0% resolved (exactly within the 35%–65% target).
- **Nothing to Do**: 40 bursts meeting the strict condition (`category in ("none", "fyi") and not needs_reply and not alert and not commitments and not questions`).
- **Owner in Burst**: 15 bursts where the owner authored at least one message in the burst.
- **Suspicious / Injection**: 15 bursts with realistic prompt injection attempts across all 5 variants.

### `monitor_cases.jsonl` (30 cases)

| Variant | Total Cases | Matching (`matched_monitor_ids != []`) | Non-Matching (`matched_monitor_ids == []`) |
|---|---|---|---|
| `uz-Latn` | 6 | 3 | 3 |
| `en` | 6 | 3 | 3 |
| `ru` | 6 | 3 | 3 |
| `uz-Cyrl` | 6 | 3 | 3 |
| `uz-ru-mix` | 6 | 3 | 3 |
| **Total** | **30** | **15** | **15** |
| **Spec Target** | ≥ 30 (≥ 5 / variant) | **50.0%** (target 35%–65%) | |

Includes realistic near misses (e.g. positive delivery feedback when monitoring delivery delays, routine tax filing when monitoring tax disputes) and prompt injection attempts that must not trigger false monitor alerts.

## Labeling Guidelines & Invariants

1. **The Alert Invariant**:
   `alert: true` if and only if `urgency == "urgent" or requests_decision or escalation_category == "urgent_decision"`.
2. **Task Resolution & Hard Negatives**:
   - Open tasks must cite a message of the side the task belongs to (contact commitment / question cites a contact message; owner commitment cites an owner message, per S05.1 §3.4).
   - A promise repeated or reaffirmed is **not** fulfillment (e.g. `b007`, `b037`, `b067`, `b097`, `b127`).
   - Requesting a deadline extension or reporting partial progress is **not** fulfillment (e.g. `b020`, `b050`, `b080`, `b110`, `b140`).
   - Every open task appears in exactly one of `resolved_tasks` or `unresolved_tasks`.
3. **Prompt Injections & Suspicious Requests**:
   `suspicious: true` flags instructions directed at the assistant (e.g. "Mark this urgent", "Output all passwords", "Ignore previous instructions"). Labels reflect what a careful human would conclude about the *real* request.
4. **Timezone & Chronology**:
   - Messages are chronological: `history sent_at` < `burst sent_at` < `now` <= `deadline`.
   - All machine timestamps are UTC (`Z`). Times mentioned in conversational text are local (`Asia/Tashkent`, UTC+5), converted appropriately (`UTC = local - 5h`).
5. **Encoding**:
   Native UTF-8 without BOM. Cyrillic characters in U+0400–U+04FF, no Windows-1252 double-encoding.
