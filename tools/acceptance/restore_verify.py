"""Read-only scratch restore gates; only fixed codes, counts and ids reach stdout."""

from __future__ import annotations

import os
import sys
from collections import Counter
from typing import Any

import psycopg
from tools.acceptance.p2_verify import REASONS, open_database

PENDING = ("proposed", "awaiting_approval", "revising", "approved", "scheduled", "executing")


def _id(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def _notice(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("actions"), dict):
        return False
    # Validate before printing anything, including account keys or action ids.
    rows: list[tuple[int, list[int], list[int]]] = []
    for account, groups in value["actions"].items():
        if (
            not isinstance(account, str)
            or not account.isascii()
            or not account.isdecimal()
            or len(account) > 19
            or not _id(int(account))
            or not isinstance(groups, dict)
        ):
            return False
        ids: dict[str, list[int]] = {}
        for kind in ("cancelled", "uncertain"):
            entries = groups.get(kind)
            if not isinstance(entries, list):
                return False
            ids[kind] = []
            for entry in entries:
                if not isinstance(entry, dict) or not _id(entry.get("id")):
                    return False
                ids[kind].append(entry["id"])
        rows.append((int(account), ids["cancelled"], ids["uncertain"]))
    for account_id, cancelled, uncertain in sorted(rows):
        print(f"account={account_id} cancelled={len(cancelled)} uncertain={len(uncertain)}")
        for kind, action_ids in (("cancelled", cancelled), ("uncertain", uncertain)):
            for action_id in sorted(action_ids):
                print(f"account={account_id} {kind}_action={action_id}")
    return True


def verify(conn: psycopg.Connection[Any]) -> int:
    counts = dict(conn.execute("SELECT state, count(*) FROM app.actions GROUP BY state"))
    pending = sum(counts.get(state, 0) for state in PENDING)
    for state in PENDING:
        print(f"state={state} count={counts.get(state, 0)}")
    print(f"pending_actions={pending}")
    reasons: Counter[str] = Counter()
    for reason, count in conn.execute(
        "SELECT state_reason, count(*) FROM app.actions GROUP BY state_reason"
    ):
        reasons[reason if reason in REASONS else "none" if reason is None else "invalid"] += count
    for reason, count in sorted(reasons.items()):
        print(f"state_reason={reason} count={count}")
    jobs = dict(
        conn.execute(
            "SELECT status, count(*) FROM procrastinate.procrastinate_jobs "
            "WHERE status IN ('todo', 'doing') GROUP BY status"
        )
    )
    for status in ("todo", "doing"):
        print(f"job_status={status} count={jobs.get(status, 0)}")
    pending_jobs = sum(jobs.values())
    print(f"pending_jobs={pending_jobs}")
    row = conn.execute(
        "SELECT value FROM app.system_state WHERE key = %s", ("restore_notice_pending",)
    ).fetchone()
    print(f"restore_notice_pending={'yes' if row is not None else 'no'}")
    notice_valid = row is not None and _notice(row[0])
    print(f"restore_notice_valid={'yes' if notice_valid else 'no'}")
    passed = pending == 0 and pending_jobs == 0 and notice_valid
    print("restore_verification=PASS" if passed else "restore_verification=FAIL")
    return int(not passed)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    try:
        with open_database(url) as conn:
            return verify(conn)
    except Exception as error:
        # Operational entrypoint: database errors can contain private row values.
        print(f"restore_verification_failed type={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
