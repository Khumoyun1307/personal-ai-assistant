# E2 Dataset — Synthetic Draft Reply Situations

Synthetic evaluation dataset for draft reply generation (Phase P2, M04).
Authored by Junior (junior engineer) following `docs/engineering/p2/specs/E2-drafts-eval.md` §A.

## Provenance
- **Type:** 100% Synthetic (invented names, numbers, scenarios; never derived from real Telegram data).
- **Date:** 2026-10-27
- **Part 1 Content:** 60 situations (30 `uz-Latn`, 30 `en`).
- **Variants covered in Part 1:**
  - `uz-Latn`: 30 situations (`s001`–`s030`)
  - `en`: 30 situations (`s031`–`s060`)
- **Part 2 (upcoming):** `uz-Cyrl`, `ru`, `uz-ru-mix` (≥ 90 situations, to reach ≥ 150 total).

## Tag and Coverage Counts (Part 1)
- `no_reply_needed`: 13
- `injection`: 8
- `commitment_bait`: 8
- `sensitive`: 6
- `decision_needed`: 8
- New synthetic conversations (with canaries): 20
- Existing E1 conversation cut points: 40
