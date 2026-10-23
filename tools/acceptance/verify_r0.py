"""R0 acceptance verification (PRD §21 R0 #2; `tools/acceptance/r0-session.md`).

Checks the store one-to-one against the numbered synthetic script without
printing any message text: the R0 number is extracted inside SQL, and only
numbers, counts, directions and timings reach the console (AGENTS §9).

    python tools/acceptance/verify_r0.py --expect 1-60 --deleted 9,10,19 \
        --edited 2,17 --missing-expected 23,28 --no-text 12,14,15,16

Prod: `just prod-verify-r0 <ip> --expect 1-16 …` — the prod image ships only
src/, so deploy/server.sh mounts the checkout's tools/acceptance/ read-only
into a one-off app container (`compose run --rm --no-deps`). Dev: inside the
dev container. Exit 0 when every check passes, 1 otherwise. Outside `src/`,
never imported by it (AGENTS §5).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import Counter

import psycopg
from psycopg import sql


def parse_numbers(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return out


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", required=True, help="numbers sent, e.g. 1-60")
    parser.add_argument("--deleted", default="", help="numbers deleted (tombstones expected)")
    parser.add_argument("--edited", default="", help="numbers edited with changed text")
    parser.add_argument(
        "--missing-expected",
        default="",
        help="numbers Telegram never delivered (paused, disconnected)",
    )
    parser.add_argument(
        "--no-text", default="", help="numbers sent as media without a caption (matched by kind)"
    )
    parser.add_argument("--chat", type=int, default=None, help="business chat id (default: all)")
    args = parser.parse_args()

    expected = parse_numbers(args.expect)
    deleted = parse_numbers(args.deleted)
    edited = parse_numbers(args.edited)
    missing_expected = parse_numbers(args.missing_expected)
    no_text = parse_numbers(args.no_text)
    matchable = expected - deleted - missing_expected - no_text

    # The optional chat filter is composed, not interpolated; the id is bound.
    chat_filter = sql.SQL("") if args.chat is None else sql.SQL("AND c.business_chat_id = %(chat)s")
    params = {"chat": args.chat}
    failures: list[str] = []

    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        # Numbers are extracted in SQL; Python never sees the text.
        cur.execute(
            sql.SQL(
                """
                SELECT (regexp_match(m.text, '^R0-(\\d{{3}})'))[1]::int AS n,
                       m.direction, m.edited_at IS NOT NULL, m.content_type
                FROM app.messages m JOIN app.conversations c ON c.id = m.conversation_id
                WHERE m.text ~ '^R0-\\d{{3}}' AND m.deleted_at IS NULL {chat_filter}
                ORDER BY 1
                """
            ).format(chat_filter=chat_filter),
            params,
        )
        rows = cur.fetchall()
        cur.execute(
            sql.SQL(
                """
                SELECT count(*) FROM app.messages m
                JOIN app.conversations c ON c.id = m.conversation_id
                WHERE m.deleted_at IS NOT NULL {chat_filter}
                """
            ).format(chat_filter=chat_filter),
            params,
        )
        tombstones = int(cur.fetchone()[0])
        cur.execute(
            sql.SQL(
                """
                SELECT m.content_type, count(*) FROM app.messages m
                JOIN app.conversations c ON c.id = m.conversation_id
                WHERE m.deleted_at IS NULL AND m.direction = 'contact' {chat_filter}
                GROUP BY 1 ORDER BY 1
                """
            ).format(chat_filter=chat_filter),
            params,
        )
        kinds = dict(cur.fetchall())
        cur.execute(
            """
            SELECT extract(epoch FROM (processed_at - to_timestamp(
                     (payload->'business_message'->>'date')::bigint)))
            FROM app.inbound_updates
            WHERE kind = 'business_message' AND status = 'processed' AND payload IS NOT NULL
              AND payload->'business_message'->>'date' ~ '^\\d+$'
            """
        )
        latencies = [float(r[0]) for r in cur.fetchall() if r[0] is not None]

    found = Counter(int(n) for n, _, _, _ in rows)
    directions = Counter(direction for _, direction, _, _ in rows)
    edited_found = {int(n) for n, _, was_edited, _ in rows if was_edited}
    duplicates = sorted(n for n, count in found.items() if count > 1)
    missing = sorted(matchable - set(found))
    unexpected = sorted(set(found) - expected)

    print(f"numbers found: {len(found)} of {len(matchable)} matchable")
    print(f"directions: {dict(sorted(directions.items()))}")
    print(f"missing: {missing or 'none'}")
    print(f"duplicates: {duplicates or 'none'}")
    print(f"unexpected numbers: {unexpected or 'none'}")
    print(f"edited (store): {sorted(edited_found) or 'none'}  expected: {sorted(edited) or 'none'}")
    print(f"tombstones: {tombstones}  expected: {len(deleted)}")
    print(f"contact media kinds: {kinds}")
    if latencies:
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, round(0.95 * len(latencies)) - 1)]
        print(
            f"NFR-02 processed_at - date: n={len(latencies)} "
            f"median={statistics.median(latencies):.2f}s p95={p95:.2f}s (target < 5 s)"
        )
        if p95 >= 5:
            failures.append("NFR-02 p95 >= 5 s")

    if missing:
        failures.append(f"missing numbers {missing}")
    if duplicates:
        failures.append(f"duplicate numbers {duplicates}")
    if not edited <= edited_found:
        failures.append(f"edits not recorded for {sorted(edited - edited_found)}")
    if tombstones < len(deleted):
        failures.append(f"only {tombstones} tombstones for {len(deleted)} deletions")
    if set(found) & missing_expected:
        failures.append(
            f"numbers expected to be lost were stored: {sorted(set(found) & missing_expected)}"
        )

    print("RESULT:", "PASS" if not failures else "FAIL — " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
