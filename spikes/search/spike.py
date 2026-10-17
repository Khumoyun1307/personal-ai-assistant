"""M00 S00.3 search spike (ARCHITECTURE §11.2, ADR-10).

Throwaway. Answers, against the pinned PostgreSQL 18 image:

  S1  do 1995 Latin, new Latin and Uzbek Cyrillic converge on one search key?
  S2  does `to_tsvector('simple', key)` lex the canonical form usefully
      (ts_debug), and does a query in ANY script retrieve text stored in ANY
      other?
  S3  does pg_trgm give useful similarity for typos and substrings
      (show_trgm), and does it survive the non-ASCII canonical letters?
  S4  do Russian and English survive the same pipeline without damage?

All samples are hand-written and synthetic. No real chat data (AGENTS §9).

Run:  docker compose -f compose.dev.yml run --rm app python spikes/search/spike.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import psycopg
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).parent))
from normalize_proto import normalize  # noqa: E402

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)


# --------------------------------------------------------------- synthetic data
# One sentence, four ways of writing it. Hand-written, not from any real chat.
UZBEK_VARIANTS = {
    "new Latin": "Oʻzbekiston Respublikasi gʻalaba qozondi",
    "1995 Latin": "O'zbekiston Respublikasi g'alaba qozondi",
    "backtick Latin": "O`zbekiston Respublikasi g`alaba qozondi",
    "Cyrillic": "Ўзбекистон Республикаси ғалаба қозонди",
}

CORPUS = [
    ("uz-new", "Ertaga soat uchda uchrashamizmi? Oʻzbekiston Respublikasi gʻalaba qozondi"),
    ("uz-1995", "Ertaga soat uchda uchrashamizmi? O'zbekiston Respublikasi g'alaba qozondi"),
    ("uz-cyr", "Эртага соат учда учрашамизми? Ўзбекистон Республикаси ғалаба қозонди"),
    ("ru", "Давай встретимся завтра в три часа"),
    ("en", "Shall we meet tomorrow at three?"),
]

QUERIES = {
    "new Latin oʻzbekiston": "oʻzbekiston",
    "1995 Latin o'zbekiston": "o'zbekiston",
    "Cyrillic Ўзбекистон": "Ўзбекистон",
    "new Latin gʻalaba": "gʻalaba",
    "Cyrillic ғалаба": "ғалаба",
}


def dsn() -> str:
    return (
        sa.engine.make_url(os.environ["TEST_DATABASE_URL"])
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def main() -> int:
    # ------------------------------------------------------------ S1 convergence
    keys = {label: normalize(text) for label, text in UZBEK_VARIANTS.items()}
    for label, key in keys.items():
        print(f"  {label:16} -> {key}")
    distinct = set(keys.values())
    record(
        "S1 Uzbek 1995 Latin / new Latin / Cyrillic converge",
        len(distinct) == 1,
        f"{len(distinct)} distinct key(s): {sorted(distinct)}",
    )

    with psycopg.connect(dsn(), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.execute("DROP TABLE IF EXISTS spike_messages")
        conn.execute(
            "CREATE TABLE spike_messages ("
            "  id text PRIMARY KEY,"
            "  body text NOT NULL,"
            "  search_key text NOT NULL,"
            "  search_tsv tsvector GENERATED ALWAYS AS "
            "    (to_tsvector('simple', search_key)) STORED)"
        )
        conn.execute(
            "CREATE INDEX spike_messages_tsv ON spike_messages USING gin (search_tsv)"
        )
        conn.execute(
            "CREATE INDEX spike_messages_trgm ON spike_messages "
            "USING gin (search_key gin_trgm_ops)"
        )
        for mid, body in CORPUS:
            conn.execute(
                "INSERT INTO spike_messages (id, body, search_key) VALUES (%s, %s, %s)",
                (mid, body, normalize(body)),
            )

        # ------------------------------------------------- S2 ts_debug + retrieval
        debug = conn.execute(
            "SELECT alias, token FROM ts_debug('simple', %s)", (normalize(UZBEK_VARIANTS["new Latin"]),)
        ).fetchall()
        print("\n  ts_debug tokens:", [token for _, token in debug])
        tokens = {token for _, token in debug}
        record(
            "S2a ts_debug keeps the canonical letters as whole words",
            "özbekiston" in tokens and "ğalaba" in tokens,
            f"tokens={sorted(tokens)}",
        )

        cross_script_ok = True
        detail: list[str] = []
        for label, raw_query in QUERIES.items():
            key = normalize(raw_query)
            rows = conn.execute(
                "SELECT id FROM spike_messages "
                "WHERE search_tsv @@ to_tsquery('simple', %s) ORDER BY id",
                (key.replace(" ", " & ") + ":*",),
            ).fetchall()
            found = {r[0] for r in rows}
            expected = {"uz-new", "uz-1995", "uz-cyr"}
            ok = found == expected
            cross_script_ok &= ok
            detail.append(f"{label} -> {sorted(found)}")
        record(
            "S2b a query in ANY script finds ALL three Uzbek spellings",
            cross_script_ok,
            "; ".join(detail),
        )

        # ------------------------------------------------------------ S3 trigram
        trgm = conn.execute("SELECT show_trgm(%s)", (normalize("gʻalaba"),)).fetchone()
        print("\n  show_trgm('ğalaba'):", trgm[0] if trgm else None)
        typo = normalize("galaba")  # the apostrophe simply omitted, a common typo
        sim: list[tuple[str, float]] = conn.execute(
            "SELECT id, similarity(search_key, %s) AS s FROM spike_messages "
            "ORDER BY s DESC LIMIT 3",
            (typo,),
        ).fetchall()
        best = sim[0] if sim else ("", 0.0)
        record(
            "S3 pg_trgm ranks the Uzbek rows first for an apostrophe-less typo",
            best[0].startswith("uz") and float(best[1]) > 0.0,
            f"top={[(i, round(float(s), 3)) for i, s in sim]}",
        )

        # ------------------------------------------------------- S4 ru/en intact
        ru_key = normalize(CORPUS[3][1])
        en_key = normalize(CORPUS[4][1])
        rows = conn.execute(
            "SELECT id FROM spike_messages "
            "WHERE search_tsv @@ to_tsquery('simple', %s)",
            (normalize("завтра") + ":*",),
        ).fetchall()
        record(
            "S4 Russian transliterates consistently and is retrievable",
            {r[0] for r in rows} == {"ru"},
            f"ru_key={ru_key!r}, query 'завтра' -> {[r[0] for r in rows]}",
        )
        record(
            "S4b English passes through unchanged apart from casing",
            en_key == "shall we meet tomorrow at three?",
            f"en_key={en_key!r}",
        )

        conn.execute("DROP TABLE spike_messages")

    print("\n" + "=" * 74)
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, detail_text in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail_text}")
    print("=" * 74)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
