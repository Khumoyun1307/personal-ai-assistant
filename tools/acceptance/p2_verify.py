"""Content-free P2 views. Audit-based trial-check follows S04.4 integration.

Run in the dev container or through deploy/server.sh p2-verify. Only ids,
states, counts, timings and selected audit codes may reach stdout.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

TABLES = (
    "actions",
    "tasks",
    "conversation_triage",
    "automations",
    "automation_occurrences",
    "digests",
    "notifications",
)
STATES = frozenset(
    {
        "proposed",
        "awaiting_approval",
        "revising",
        "approved",
        "scheduled",
        "executing",
        "succeeded",
        "failed",
        "delivery_uncertain",
        "rejected",
        "outdated",
        "expired",
        "cancelled",
        "blocked",
    }
)
REASONS = frozenset(
    [
        "superseded",
        "new_contact_message",
        "takeover",
        "owner_edit",
        "source_edited",
        "source_deleted",
        "rights_changed",
        "connection_disabled",
        "connection_invalid",
        "ignore_mode",
        "data_deleted",
        "consent_revoked",
        "ingest_error",
        "restored_from_backup",
        "window_closed",
        "max_age",
        "owner_rejected",
        "policy_denied",
        "sending_disabled",
        "too_long",
        "autonomy_paused",
        "sent",
        "not_delivered",
        "ambiguous",
        "stale_executing",
        "late_result",
    ]
)


def aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO timestamp with timezone") from error


def positive(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise argparse.ArgumentTypeError("expected a positive bigint id")
    return int(value)


def prefix(value: str) -> str:
    if not re.fullmatch(r"P2-[0-9]{1,2}", value):
        raise argparse.ArgumentTypeError("expected P2- followed by one or two digits")
    return value


def numbers(value: str) -> set[int]:
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", value) or len(value) > 1000:
        raise argparse.ArgumentTypeError("expected numbers or ascending ranges separated by commas")
    found: set[int] = set()
    for part in value.split(","):
        lo, _, high = part.partition("-")
        low, upper = int(lo), int(high or lo)
        if not 0 <= low <= upper <= 9999:
            raise argparse.ArgumentTypeError("numbers must be 0-9999 and ranges ascending")
        found.update(range(low, upper + 1))
    return found


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    modes = cli.add_subparsers(dest="mode", required=True)
    modes.add_parser("release")
    for name in ("actions", "notifications", "tasks", "digests"):
        command = modes.add_parser(name)
        command.add_argument("--since", type=aware, default=datetime(1970, 1, 1, tzinfo=UTC))
    modes.add_parser("action").add_argument("id", type=positive)
    script = modes.add_parser("script")
    script.add_argument("prefix", type=prefix)
    script.add_argument("--expect", type=numbers, required=True)
    script.add_argument("--direction", choices=("contact", "owner_manual", "assistant_sent"))
    return cli


@contextmanager
def open_database(url: str) -> Iterator[psycopg.Connection[Any]]:
    # The session default protects every transaction, including caller mistakes.
    with (
        psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1),
            options=(
                "-c default_transaction_read_only=on "
                "-c statement_timeout=10000 -c lock_timeout=1000"
            ),
        ) as conn,
        conn.transaction(),
    ):
        yield conn


def local(moment: datetime | None, zone: str | None = None) -> str:
    if moment is None:
        return "-"
    try:
        timezone = ZoneInfo(zone or "UTC")
    except ZoneInfoNotFoundError, ValueError:
        timezone = ZoneInfo("UTC")
    return moment.astimezone(timezone).isoformat()


def details(values: dict[str, Any]) -> str:
    """Allow-list keys AND their structural value types; never serialize JSON."""
    kept: dict[str, Any] = {}
    for key in ("from", "to"):
        if isinstance(values.get(key), str) and values[key] in STATES:
            kept[key] = values[key]
    if isinstance(values.get("reason"), str) and values["reason"] in REASONS:
        kept["reason"] = values["reason"]
    for key in ("version", "retry_after"):
        value = values.get(key)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            kept[key] = value
    if type(values.get("identity_pending")) is bool:
        kept["identity_pending"] = values["identity_pending"]
    attempt = values.get("attempt_id")
    if isinstance(attempt, str):
        with suppress(ValueError):
            kept["attempt_id"] = str(UUID(attempt))
    return " ".join(f"{key}={value}" for key, value in kept.items())


def release(conn: psycopg.Connection[Any]) -> None:
    for (head,) in conn.execute("SELECT version_num FROM app.alembic_version ORDER BY version_num"):
        print(f"alembic={head}")
    flags = sorted(
        {v for v in os.environ.get("FEATURE_FLAGS", "").replace(" ", "").split(",") if v}
    )
    print("flags=" + ",".join(flags))
    for table in TABLES:
        present = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"app.{table}",)).fetchone()[
            0
        ]
        print(f"table={table} present={'yes' if present else 'no'}")
    for state, count in conn.execute(
        "SELECT state, count(*) FROM app.actions GROUP BY state ORDER BY state"
    ):
        print(f"state={state} count={count}")


def actions(conn: psycopg.Connection[Any], since: datetime) -> None:
    rows = conn.execute(
        """
        SELECT a.id, a.conversation_id, a.origin, a.state, a.state_reason,
               a.version, a.card_state,
               CASE WHEN a.approved_text_hash IS NULL OR a.sent_text_hash IS NULL THEN '-'
                    WHEN a.approved_text_hash = a.sent_text_hash THEN 'yes' ELSE 'no' END,
               a.telegram_message_id IS NOT NULL,
               (SELECT count(*) FROM app.audit_log e WHERE e.account_id = a.account_id
                AND e.subject_type = 'action' AND e.subject_id = a.id
                AND e.event = 'action_claimed')
        FROM app.actions a WHERE a.created_at >= %s ORDER BY a.id
        """,
        (since,),
    )
    for identity, chat, origin, state, reason, version, card, match, telegram, attempts in rows:
        print(
            f"action={identity} conversation={chat} origin={origin} state={state} "
            f"reason={reason or '-'} version={version} card_state={card or '-'} "
            f"hash_match={match} telegram_id={'yes' if telegram else 'no'} attempts={attempts}"
        )


def action(conn: psycopg.Connection[Any], identity: int) -> None:
    rows = conn.execute(
        """
        SELECT e.event, e.at, o.timezone, e.details FROM app.audit_log e
        JOIN app.actions a ON a.id = e.subject_id AND a.account_id = e.account_id
        JOIN app.accounts o ON o.id = a.account_id
        WHERE e.subject_type = 'action' AND a.id = %s ORDER BY e.at, e.id
        """,
        (identity,),
    )
    for event, at, timezone, values in rows:
        print(
            f"action={identity} event={event} utc={local(at)} "
            f"local={local(at, timezone)} {details(values)}"
        )


def script(conn: psycopg.Connection[Any], args: argparse.Namespace) -> None:
    # Only numbered synthetic prefixes are accepted. SQL returns the number,
    # never the message text. Prefix and regex reach SQL as bound parameters.
    pattern = "^P2-(" + args.prefix[3:] + r"[0-9]{1,2})(?:[^0-9]|$)"
    rows = conn.execute(
        """
        SELECT (regexp_match(m.text, %s))[1]::int, m.direction
        FROM app.messages m WHERE m.text ~ %s AND m.deleted_at IS NULL
        ORDER BY 1, 2
        """,
        (pattern, pattern),
    ).fetchall()
    found = {int(n) for n, d in rows if args.direction is None or d == args.direction}
    print(
        f"found={sorted(found)} missing={sorted(args.expect - found)} "
        f"unexpected={sorted(found - args.expect)}"
    )
    directions = sorted(
        {direction for _, direction in rows} | ({args.direction} if args.direction else set())
    )
    for direction in directions:
        found = {int(n) for n, d in rows if d == direction}
        print(
            f"direction={direction} found={sorted(found)} "
            f"missing={sorted(args.expect - found)} unexpected={sorted(found - args.expect)}"
        )


def notifications(conn: psycopg.Connection[Any], since: datetime) -> None:
    rows = conn.execute(
        """SELECT id, kind, alert_class, priority, status, reason_code,
        extract(epoch FROM sent_at - created_at) FROM app.notifications
        WHERE created_at >= %s ORDER BY id""",
        (since,),
    ).fetchall()
    for (kind, status, reason), count in sorted(
        Counter((r[1], r[4], r[5] or "-") for r in rows).items()
    ):
        print(f"kind={kind} status={status} reason={reason} count={count}")
    for identity, kind, cls, priority, status, _reason, seconds in rows:
        print(
            f"notification={identity} kind={kind} class={cls} priority={priority} "
            f"status={status} latency_seconds={seconds if seconds is not None else '-'}"
        )


def tasks(conn: psycopg.Connection[Any], since: datetime) -> None:
    rows = conn.execute(
        """SELECT id, conversation_id, kind, status, closed_reason, created_at, closed_at
        FROM app.tasks WHERE created_at >= %s ORDER BY id""",
        (since,),
    ).fetchall()
    for (kind, status, reason), count in sorted(
        Counter((r[2], r[3], r[4] or "-") for r in rows).items()
    ):
        print(f"kind={kind} status={status} closed_reason={reason} count={count}")
    for identity, chat, kind, status, _reason, created, closed in rows:
        print(
            f"task={identity} conversation={chat} kind={kind} status={status} "
            f"created={local(created)} closed={local(closed)}"
        )


def digests(conn: psycopg.Connection[Any], since: datetime) -> None:
    rows = conn.execute(
        """SELECT d.id, d.period_start, d.period_end, d.status, d.sent_at, a.timezone
        FROM app.digests d JOIN app.accounts a ON a.id = d.account_id
        WHERE d.created_at >= %s ORDER BY d.id""",
        (since,),
    )
    for identity, start, end, status, sent, zone in rows:
        print(
            f"digest={identity} start={local(start)} end={local(end)} status={status} "
            f"sent_utc={local(sent)} sent_local={local(sent, zone)}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)  # validate before opening any connection
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    try:
        with open_database(url) as conn:
            match args.mode:
                case "release":
                    release(conn)
                case "actions":
                    actions(conn, args.since)
                case "action":
                    action(conn, args.id)
                case "script":
                    script(conn, args)
                case "notifications":
                    notifications(conn, args.since)
                case "tasks":
                    tasks(conn, args.since)
                case "digests":
                    digests(conn, args.since)
    except Exception as error:
        # This CLI boundary must hide content even from an unexpected failure.
        print(f"verification_failed type={type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
