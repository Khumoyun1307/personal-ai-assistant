# M00 S00.3 — search spike (ADR-10)

Throwaway code (AGENTS §5): never imported by `src/`. The shipped normalizer
lands in `src/assistant/memory/` at M03 with its own tests and a
`normalizer_version`. All samples here are hand-written and synthetic — no real
chat data, ever (AGENTS §9).

```
docker compose -f compose.dev.yml run --rm app python spikes/search/spike.py
```

Uses the `assistant_test` database, creates `spike_messages`, drops it again.

## Result (2026-10-17, `pgvector/pgvector:pg18`) — 6/6 passed

All four ways of writing the same Uzbek sentence collapse to one key:

| Written as | Normalized |
|---|---|
| `Oʻzbekiston Respublikasi gʻalaba qozondi` (new Latin) | `özbekiston respublikasi ğalaba qozondi` |
| `O'zbekiston Respublikasi g'alaba qozondi` (1995 Latin) | *(identical)* |
| `O\`zbekiston ...` (backtick, phone keyboards) | *(identical)* |
| `Ўзбекистон Республикаси ғалаба қозонди` (Cyrillic) | *(identical)* |

A query typed in **any** of those scripts retrieves text stored in **all** of
them, via `to_tsvector('simple', search_key)`. `ts_debug` confirms `özbekiston`
and `ğalaba` survive as whole tokens rather than being split at the non-ASCII
letter. Russian transliterates consistently on both sides (`завтра` → `zavtra`,
retrievable); English passes through unchanged apart from casing.

## Caveat for S03.4

`show_trgm('ğalaba')` returns `['0x9e987f', '0x3c13cc', '0x4978cc', 'aba',
'ala', 'ba ', 'lab']` — pg_trgm **hashes** trigrams containing multibyte
characters instead of storing them literally. Fuzzy matching still worked here,
but it means similarity around the canonical `ö`/`ğ` letters is hash-based and
in principle collision-prone, and scores are lower than for pure ASCII.

This is an input to the M03 retrieval thresholds (S03.4), not a reason to change
ARCHITECTURE §11.2 now: if fuzzy recall on Uzbek falls short there, the
alternatives to measure are an ASCII-only canonical form (`oʻ` → `o`, `gʻ` → `g`,
losing the vowel distinction) or query-variant generation. Decision belongs with
the owner at M03 (AGENTS §2).
