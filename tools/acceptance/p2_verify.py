"""Content-free P2 views and the M04 send-history trial check.

Run in the dev container or through deploy/server.sh p2-verify. Only ids,
states, counts, timings and selected audit codes may reach stdout.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
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
    trial = modes.add_parser("trial-check")
    trial.add_argument("--since", type=aware, default=datetime(1970, 1, 1, tzinfo=UTC))
    trial.add_argument("--until", type=aware, default=datetime.now(UTC))
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
        # One stable snapshot: a view or the audit must not straddle Tx B.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
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
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"app.{table}",)).fetchone()
        present = bool(row and row[0])
        print(f"table={table} present={'yes' if present else 'no'}")
    for state, count in conn.execute(
        "SELECT state, count(*) FROM app.actions GROUP BY state ORDER BY state"
    ):
        print(f"state={state} count={count}")


def actions(conn: psycopg.Connection[Any], since: datetime) -> None:
    rows = conn.execute(
        """
        SELECT a.id, a.account_id, a.conversation_id, a.origin, a.state, a.state_reason,
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
    for (
        identity,
        account,
        chat,
        origin,
        state,
        reason,
        version,
        card,
        match,
        telegram,
        attempts,
    ) in rows:
        print(
            f"action={identity} account={account} conversation={chat or '-'} "
            f"origin={origin} state={state} "
            f"reason={reason or '-'} version={version} card_state={card or '-'} "
            f"hash_match={match} telegram_id={'yes' if telegram else 'no'} attempts={attempts}"
        )
    for account, identity, at in conn.execute(
        """SELECT account_id, subject_id, at FROM app.audit_log
        WHERE subject_type = 'connection' AND event = 'connection_marked_stale'
          AND at >= %s ORDER BY at, id""",
        (since,),
    ):
        print(
            f"account={account} connection={identity or '-'} "
            f"event=connection_marked_stale utc={local(at)}"
        )


def action(conn: psycopg.Connection[Any], identity: int) -> None:
    rows = conn.execute(
        """
        SELECT e.event, e.at, o.timezone, e.details, a.account_id FROM app.audit_log e
        JOIN app.actions a ON a.id = e.subject_id AND a.account_id = e.account_id
        JOIN app.accounts o ON o.id = a.account_id
        WHERE e.subject_type = 'action' AND a.id = %s ORDER BY e.at, e.id
        """,
        (identity,),
    )
    found = False
    for event, at, timezone, values, account in rows:
        found = True
        print(
            f"action={identity} event={event} account={account} utc={local(at)} "
            f"local={local(at, timezone)} {details(values)}"
        )
    if not found:
        print(f"action={identity} events=0")


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
        account_id,
        extract(epoch FROM sent_at - created_at) FROM app.notifications
        WHERE created_at >= %s ORDER BY id""",
        (since,),
    ).fetchall()
    for (kind, status, reason), count in sorted(
        Counter((r[1], r[4], r[5] or "-") for r in rows).items()
    ):
        print(f"kind={kind} status={status} reason={reason} count={count}")
    for identity, kind, cls, priority, status, _reason, account, seconds in rows:
        print(
            f"notification={identity} account={account} kind={kind} "
            f"class={cls} priority={priority} "
            f"status={status} latency_seconds={seconds if seconds is not None else '-'}"
        )


def tasks(conn: psycopg.Connection[Any], since: datetime) -> None:
    rows = conn.execute(
        """SELECT id, conversation_id, kind, status, closed_reason, created_at, closed_at,
        account_id
        FROM app.tasks WHERE created_at >= %s ORDER BY id""",
        (since,),
    ).fetchall()
    for (kind, status, reason), count in sorted(
        Counter((r[2], r[3], r[4] or "-") for r in rows).items()
    ):
        print(f"kind={kind} status={status} closed_reason={reason} count={count}")
    for identity, chat, kind, status, _reason, created, closed, account in rows:
        print(
            f"task={identity} account={account} conversation={chat or '-'} "
            f"kind={kind} status={status} "
            f"created={local(created)} closed={local(closed)}"
        )


def digests(conn: psycopg.Connection[Any], since: datetime) -> None:
    rows = conn.execute(
        """SELECT d.id, d.period_start, d.period_end, d.status, d.sent_at, a.timezone,
        d.account_id
        FROM app.digests d JOIN app.accounts a ON a.id = d.account_id
        WHERE d.created_at >= %s ORDER BY d.id""",
        (since,),
    )
    for identity, start, end, status, sent, zone, account in rows:
        print(
            f"digest={identity} account={account} start={local(start)} "
            f"end={local(end)} status={status} "
            f"sent_utc={local(sent)} sent_local={local(sent, zone)}"
        )


TRIAL_CHECKS = (
    "approved_before_claim",
    "hash_match",
    "one_send_per_action",
    "no_send_after_terminal",
    "recipient_matches",
    "no_orphan_sends",
)
_TRIAL_EVENTS = (
    "action_approved",
    "action_claimed",
    "action_succeeded",
    "action_failed",
    "action_delivery_uncertain",
    "action_rate_limited",
    "action_late_result",
    "action_outdated",
    "action_superseded",
    "action_expired",
    "action_rejected",
    "action_cancelled",
    "action_blocked",
)
_NO_SEND_STATES = frozenset({"outdated", "expired", "rejected", "cancelled", "blocked", "failed"})
_TERMINAL_EVENTS = {
    "action_outdated": "outdated",
    "action_superseded": "outdated",
    "action_expired": "expired",
    "action_rejected": "rejected",
    "action_cancelled": "cancelled",
    "action_blocked": "blocked",
    "action_failed": "failed",
}


@dataclass(frozen=True)
class _TrialAction:
    id: int
    account: int
    conversation: int
    origin: str
    state: str
    telegram: bool
    hashes_match: bool
    at: datetime
    zone: str
    recipient_valid: bool
    current_claim_present: bool
    text_missing: bool = False
    message_deleted_after_success: bool = False


@dataclass(frozen=True)
class _TrialEvent:
    action: int
    id: int
    at: datetime
    name: str
    version: int | None
    attempt: str | None
    before: str | None
    after: str | None
    actor: str

    @property
    def order(self) -> tuple[datetime, int]:
        return self.at, self.id


def _trial_history(
    action: _TrialAction, events: list[_TrialEvent], fail: Callable[[str, str], None]
) -> None:
    """Inspect only the structural projection, never content or arbitrary JSON."""
    approvals = [
        event
        for event in events
        if event.name == "action_approved"
        and event.version is not None
        and event.before == "awaiting_approval"
        and event.after == "approved"
        and event.actor == "owner"
    ]
    if action.origin == "delegation_auto":
        fail("approved_before_claim", "autonomous_origin")
    elif action.origin not in {"owner_dictated", "agent_proposal"}:
        fail("approved_before_claim", "unknown_origin")
    if action.state not in STATES:
        fail("one_send_per_action", "unknown_state")
    claims: dict[str, _TrialEvent] = {}
    rate_limited: set[str] = set()
    outcomes: dict[str, _TrialEvent] = {}
    late_results: set[str] = set()
    definite_non_delivery: set[str] = set()
    last_claim: str | None = None
    terminal: _TrialEvent | None = None
    success = uncertain = False
    for event in events:
        if event.name == "action_claimed":
            if (
                event.version is None
                or event.before != "approved"
                or event.after != "executing"
                or not any(
                    approved.version == event.version and approved.order < event.order
                    for approved in approvals
                )
            ):
                fail("approved_before_claim", "missing_matching_approval")
            if terminal is not None:
                fail("no_send_after_terminal", "claim_after_terminal")
            if event.attempt is None:
                fail("one_send_per_action", "missing_claim_attempt")
                continue
            if event.attempt in claims or (
                last_claim is not None and last_claim not in rate_limited
            ):
                fail("one_send_per_action", "claim_without_definite_retry")
            claims[event.attempt] = event
            last_claim = event.attempt
            continue
        if event.name in {
            "action_succeeded",
            "action_failed",
            "action_delivery_uncertain",
            "action_rate_limited",
            "action_late_result",
        } or (event.name == "action_expired" and event.before == "executing"):
            claim = claims.get(event.attempt) if event.attempt is not None else None
            matching = not (
                claim is None
                or event.version is None
                or claim.version != event.version
                or claim.order >= event.order
            )
            if not matching:
                fail("one_send_per_action", "missing_matching_claim")
            latest = event.attempt is not None and event.attempt == last_claim
            if not latest:
                fail("one_send_per_action", "outcome_not_latest_claim")
            allowed = {
                "action_succeeded": ("executing", "succeeded"),
                "action_failed": ("executing", "failed"),
                "action_delivery_uncertain": ("executing", "delivery_uncertain"),
                "action_rate_limited": ("executing", "approved"),
                "action_expired": ("executing", "expired"),
            }
            if event.name == "action_late_result":
                valid = event.before == "delivery_uncertain" and event.after in {
                    "succeeded",
                    "failed",
                }
            else:
                valid = (event.before, event.after) == allowed[event.name]
            if not valid:
                fail("one_send_per_action", "invalid_outcome")
            previous = outcomes.get(event.attempt) if event.attempt is not None else None
            closing = previous is None
            if event.name == "action_late_result":
                closing = (
                    previous is not None
                    and previous.name == "action_delivery_uncertain"
                    and event.attempt not in late_results
                )
                if not closing:
                    fail("one_send_per_action", "invalid_late_result")
            elif not closing:
                fail("one_send_per_action", "multiple_outcomes")
            # A malformed outcome cannot prove non-delivery or close a claim.
            if matching and latest and valid and closing and event.attempt is not None:
                outcomes[event.attempt] = event
                if event.name == "action_late_result":
                    late_results.add(event.attempt)
                if event.name == "action_rate_limited":
                    rate_limited.add(event.attempt)
                if event.after in {"approved", "failed", "expired"}:
                    definite_non_delivery.add(event.attempt)
            if event.after == "succeeded":
                success = True
                if terminal is not None:
                    fail("no_send_after_terminal", "success_after_terminal")
            if event.after == "delivery_uncertain":
                uncertain = True
        if event.name in _TERMINAL_EVENTS:
            if event.after != _TERMINAL_EVENTS[event.name]:
                fail("no_send_after_terminal", "invalid_terminal_event")
            terminal = event
        elif event.after in _NO_SEND_STATES:
            terminal = event
    for attempt in claims:
        in_flight = (
            action.state == "executing" and action.current_claim_present and attempt == last_claim
        )
        if attempt not in outcomes and not in_flight:
            fail("one_send_per_action", "missing_outcome")
    if len(claims.keys() - definite_non_delivery) > 1:
        fail("one_send_per_action", "multiple_possible_deliveries")
    if action.state in {"approved", "scheduled"} and last_claim is not None:
        last_outcome = outcomes.get(last_claim)
        if last_outcome is None or last_outcome.name != "action_rate_limited":
            fail("one_send_per_action", "rearmed_without_rate_limit")
    if action.state == "succeeded" and not success:
        fail("one_send_per_action", "missing_success_outcome")
    if action.state == "delivery_uncertain" and not uncertain:
        fail("one_send_per_action", "missing_uncertain_outcome")
    if action.state == "executing" and not action.current_claim_present:
        # The send may already be in flight. An older 429 claim is no proof
        # of authorization for the current committed attempt/version.
        fail("one_send_per_action", "missing_current_claim")


def trial_check(conn: psycopg.Connection[Any], since: datetime, until: datetime) -> int:
    """Audit [since, until) on one read-only snapshot, with complete scoped histories."""
    scoped = [
        _TrialAction(*row)
        for row in conn.execute(
            """
            SELECT a.id, a.account_id, a.conversation_id, a.origin, a.state,
                   a.telegram_message_id IS NOT NULL,
                   COALESCE(a.approved_text_hash IS NOT NULL AND a.sent_text_hash IS NOT NULL
                            AND a.approved_text_hash = a.sent_text_hash, false),
                   COALESCE(a.attempt_started_at, a.updated_at, a.created_at), o.timezone,
                   COALESCE(c.account_id = a.account_id, false),
                   EXISTS (
                       SELECT 1 FROM app.audit_log claim
                       WHERE claim.account_id = a.account_id
                         AND claim.subject_type = 'action' AND claim.subject_id = a.id
                         AND claim.event = 'action_claimed'
                         AND claim.details -> 'version' = to_jsonb(a.version)
                         AND jsonb_typeof(claim.details -> 'attempt_id') = 'string'
                         AND replace(lower(claim.details ->> 'attempt_id'), '-', '') =
                             replace(lower(a.attempt_id), '-', '')
                         AND NOT EXISTS (
                             SELECT 1 FROM app.audit_log later
                             WHERE later.account_id = a.account_id
                               AND later.subject_type = 'action' AND later.subject_id = a.id
                               AND later.event = 'action_claimed'
                               AND (later.at, later.id) > (claim.at, claim.id)
                         )
                   ), a.text IS NULL,
                   EXISTS (
                       SELECT 1 FROM app.audit_log deletion
                       JOIN LATERAL (
                           SELECT success.at, success.id FROM app.audit_log success
                           WHERE success.account_id = a.account_id
                             AND success.subject_type = 'action' AND success.subject_id = a.id
                             AND success.event IN ('action_succeeded', 'action_late_result')
                             AND success.details ->> 'to' = 'succeeded'
                           ORDER BY success.at DESC, success.id DESC LIMIT 1
                       ) success ON true
                       WHERE deletion.account_id = a.account_id
                         AND (deletion.at, deletion.id) > (success.at, success.id)
                         AND (deletion.event = 'managed_data_deleted' OR (
                             deletion.event = 'data_deleted' AND (
                                 (deletion.subject_type = 'conversation'
                                  AND deletion.subject_id = a.conversation_id
                                  AND deletion.details ->> 'scope' = 'conversation')
                                 OR (deletion.subject_type = 'contact'
                                     AND deletion.subject_id = c.contact_id
                                     AND deletion.details ->> 'scope' = 'contact')
                                 OR (deletion.subject_type = 'account'
                                     AND deletion.subject_id = a.account_id
                                     AND deletion.details ->> 'scope' = 'everything')
                             )
                         ))
                   )
            FROM app.actions a JOIN app.accounts o ON o.id = a.account_id
            LEFT JOIN app.conversations c ON c.id = a.conversation_id
            WHERE (a.created_at >= %s AND a.created_at < %s) OR EXISTS (
                SELECT 1 FROM app.audit_log e
                WHERE e.account_id = a.account_id AND e.subject_type = 'action'
                  AND e.subject_id = a.id AND e.at >= %s AND e.at < %s
            ) OR EXISTS (
                SELECT 1 FROM app.messages m WHERE m.action_id = a.id
                  AND m.created_at >= %s AND m.created_at < %s
            ) ORDER BY a.id
            """,
            (since, until, since, until, since, until),
        )
    ]
    identities = [action.id for action in scoped]
    histories: dict[int, list[_TrialEvent]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT e.subject_id, e.id, e.at, e.event,
               CASE WHEN jsonb_typeof(e.details -> 'version') = 'number'
                          AND e.details ->> 'version' ~ '^[1-9][0-9]{0,8}$'
                    THEN (e.details ->> 'version')::integer END,
               CASE WHEN jsonb_typeof(e.details -> 'attempt_id') = 'string'
                          AND (e.details ->> 'attempt_id' ~ '^[0-9a-fA-F]{32}$'
                               OR e.details ->> 'attempt_id' ~
                               '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
                    THEN replace(lower(e.details ->> 'attempt_id'), '-', '') END,
               CASE WHEN e.details ->> 'from' = ANY(%s) THEN e.details ->> 'from' END,
               CASE WHEN e.details ->> 'to' = ANY(%s) THEN e.details ->> 'to' END,
               e.actor
        FROM app.audit_log e JOIN app.actions a
          ON a.id = e.subject_id AND a.account_id = e.account_id
        WHERE e.subject_type = 'action' AND a.id = ANY(%s) AND e.event = ANY(%s)
        ORDER BY e.at, e.id
        """,
        (sorted(STATES), sorted(STATES), identities, list(_TRIAL_EVENTS)),
    ):
        event = _TrialEvent(*row)
        histories[event.action].append(event)
    linked: dict[int, list[tuple[int, int, int, str]]] = defaultdict(list)
    for action_id, identity, account, conversation, direction in conn.execute(
        """SELECT action_id, id, account_id, conversation_id, direction FROM app.messages
        WHERE action_id = ANY(%s) ORDER BY id""",
        (identities,),
    ):
        linked[action_id].append((identity, account, conversation, direction))
    violations: dict[str, set[tuple[int, int, str]]] = {name: set() for name in TRIAL_CHECKS}
    for action in scoped:

        def fail(
            check: str, code: str, account: int = action.account, identity: int = action.id
        ) -> None:
            violations[check].add((account, identity, code))

        _trial_history(action, histories[action.id], fail)
        messages = linked[action.id]
        if action.state in {"succeeded", "delivery_uncertain"} and not action.hashes_match:
            fail("hash_match", "missing_or_different_hash")
        if action.state in {"approved", "scheduled"} and action.text_missing:
            fail("hash_match", "sendable_text_missing")
        deleted = (
            action.state == "succeeded" and not messages and action.message_deleted_after_success
        )
        if deleted:
            print(
                f"PASS one_send_per_action account={action.account} action={action.id} "
                "code=message_deleted_after_success"
            )
        if (
            (action.state == "succeeded" and len(messages) != 1 and not deleted)
            or (action.state != "succeeded" and messages)
            or any(message[3] != "assistant_sent" for message in messages)
        ):
            fail("one_send_per_action", "message_count_or_direction")
        if action.state in _NO_SEND_STATES and (action.telegram or messages):
            fail("no_send_after_terminal", "terminal_has_send")
        if not action.recipient_valid or any(
            account != action.account or conversation != action.conversation
            for _, account, conversation, _ in messages
        ):
            fail("recipient_matches", "account_or_conversation_mismatch")
    sends = list(
        conn.execute(
            """
        SELECT m.id, m.account_id, m.action_id,
               COALESCE(a.state = 'succeeded' AND a.account_id = m.account_id, false)
        FROM app.messages m LEFT JOIN app.actions a ON a.id = m.action_id
        WHERE m.direction = 'assistant_sent' AND m.created_at >= %s AND m.created_at < %s
        ORDER BY m.id
        """,
            (since, until),
        )
    )
    for identity, account, _action_id, valid in sends:
        if not valid:
            violations["no_orphan_sends"].add((account, identity, "unsettled_or_missing_action"))
    for check in TRIAL_CHECKS:
        for account, identity, code in sorted(violations[check]):
            key = "message" if check == "no_orphan_sends" else "action"
            print(f"FAIL {check} account={account} {key}={identity} code={code}")
        if violations[check]:
            print(f"FAIL {check} violations={len(violations[check])}")
        else:
            print(f"PASS {check}")
    for state, count in sorted(Counter(action.state for action in scoped).items()):
        print(f"state={state if state in STATES else 'invalid'} count={count}")
    for action in scoped:
        state = action.state if action.state in STATES else "invalid"
        if action.state == "delivery_uncertain":
            print(
                f"account={action.account} action={action.id} state=delivery_uncertain "
                f"utc={local(action.at)} local={local(action.at, action.zone)}"
            )
        else:
            print(f"account={action.account} action={action.id} state={state}")
    print(f"total_sends={len(sends)}")
    total = sum(len(items) for items in violations.values())
    print(f"RESULT: FAIL ({total} violations)" if total else "RESULT: PASS")
    return int(total > 0)


def main(argv: Sequence[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)  # validate before opening any connection
    if args.mode == "trial-check" and args.since >= args.until:
        cli.error("--since must be earlier than --until")
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
                case "trial-check":
                    return trial_check(conn, args.since, args.until)
    except Exception as error:
        # This CLI boundary must hide content even from an unexpected failure.
        print(f"verification_failed type={type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
