# E2 Dataset — Synthetic Draft Reply Situations

Synthetic evaluation dataset for draft reply generation (Phase P2, M04).
Authored by Junior (junior engineer) following `docs/engineering/p2/specs/E2-drafts-eval.md` §A.

## Provenance
- **Type:** 100% Synthetic (invented names, phone numbers, addresses, scenarios; never derived from real Telegram data, AGENTS §8, ADR-16).
- **Date:** 2026-10-27
- **Methodology:** Authored with seed scenarios structured per spec §A; an LLM assisted in expanding seed scenarios into multi-turn conversations and natural phrasing per §A.5 step 3, with every line and fact manually verified against source messages.
- **Canaries:** Natural, distinctive invented strings (room numbers, order codes, street addresses, ticket numbers; ≥ 5 characters) embedded once per new synthetic conversation to verify leak isolation.
- **Total Situations:** 150 situations (30 per variant across all 5 variants).
- **Variants Covered:**
  - `uz-Latn`: 30 situations (`s001`–`s030`)
  - `en`: 30 situations (`s031`–`s060`)
  - `uz-Cyrl`: 30 situations (`s061`–`s090`)
  - `ru`: 30 situations (`s091`–`s120`)
  - `uz-ru-mix`: 30 situations (`s121`–`s150`)

## Coverage & Tag Counts

### Per-Variant Summary
- **uz-Latn:** 30 situations (10 new convs with canaries `TX-8419`..`2-tor ko'cha 14-uy`, 20 E1 cuts)
  - Tags: `no_reply_needed`=6, `injection`=4, `commitment_bait`=4, `sensitive`=3, `decision_needed`=4, `mixed_apostrophes`=3, `no_apostrophe`=2
  - Tones: `formal`=9, `friendly`=12, `neutral`=9
  - Styles: `brief`=24, `detailed`=6
- **en:** 30 situations (10 new convs with canaries `PR-94021`..`Oakridge Medical`, 20 E1 cuts)
  - Tags: `no_reply_needed`=6, `injection`=4, `commitment_bait`=4, `sensitive`=3, `decision_needed`=4
  - Tones: `formal`=9, `friendly`=10, `neutral`=11
  - Styles: `brief`=22, `detailed`=8
- **uz-Cyrl:** 30 situations (12 new convs with canaries `КТ-1094`..`ID-3391`, 18 E1 cuts)
  - Tags: `no_reply_needed`=5, `injection`=4, `commitment_bait`=4, `sensitive`=3, `decision_needed`=4
  - Tones: `formal`=9, `friendly`=12, `neutral`=9
  - Styles: `brief`=25, `detailed`=5
- **ru:** 30 situations (11 new convs with canaries `ТТН-7714`..`БД-0914`, 19 E1 cuts)
  - Tags: `no_reply_needed`=5, `injection`=4, `commitment_bait`=4, `sensitive`=3, `decision_needed`=4
  - Tones: `formal`=10, `friendly`=11, `neutral`=9
  - Styles: `brief`=25, `detailed`=5
- **uz-ru-mix:** 30 situations (12 new convs with canaries `N-8841`..`TIK-9930`, 18 E1 cuts)
  - Tags: `no_reply_needed`=5, `injection`=4, `commitment_bait`=4, `sensitive`=3, `decision_needed`=4
  - Tones: `formal`=8, `friendly`=14, `neutral`=8
  - Styles: `brief`=25, `detailed`=5

### Overall Counts
- Total situations: 150
- New synthetic conversations (with unique natural canaries): 55
- Existing E1 conversation cut points: 95 (at most 3 cuts per E1 conversation, all cut points distinct contact messages)
- Minimum coverage targets from E2 spec §A.4 (>= 5 `no_reply_needed`, >= 4 `injection`, >= 4 `commitment_bait`, >= 3 `sensitive`, >= 4 `decision_needed`, >= 5 each tone, >= 5 each style, >= 8 new conversations) are satisfied across every variant.
