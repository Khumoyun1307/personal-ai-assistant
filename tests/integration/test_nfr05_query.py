"""The NFR-05 measurement query (`deploy/sql/nfr05.sql`, `just prod <ip> nfr05 1`).

M05's acceptance (PRD §17 NFR-05, §21 R2 #7) needs triage completion and
urgent-alert delivery latencies, and the share of immediate alerts the owner
marked "not useful". The query runs on the production database, so it reads
no content column; it is run here on the test database exactly as written
(the psql variable `:days` is a bind parameter for SQLAlchemy). Synthetic rows
only.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.privacy.content_columns import CONTENT_COLUMNS

pytestmark = pytest.mark.integration

QUERY = Path(__file__).resolve().parents[2] / "deploy" / "sql" / "nfr05.sql"
MEASURES = ["triage completion", "urgent alert delivery", "immediate alerts marked not useful"]


def _code() -> str:
    """The query without its comments."""
    return "\n".join(line.split("--", 1)[0] for line in QUERY.read_text("utf-8").splitlines())


def test_the_query_reads_no_content_column() -> None:
    code = _code().lower()
    for table, column in sorted(CONTENT_COLUMNS):
        assert not re.search(rf"\b{column}\b", code), f"{table}.{column} named in nfr05.sql"


def test_the_query_is_one_read_only_statement() -> None:
    code = _code().strip()
    assert code.upper().startswith("WITH ")
    assert code.count(";") == 1
    assert code.endswith(";")
    assert not re.search(r"\b(insert|update|delete|truncate|drop|alter|create)\b", code, re.I)


async def _chat(session: AsyncSession) -> tuple[int, int]:
    # Only this test's rows count (the transaction is rolled back afterwards).
    await session.execute(text("DELETE FROM app.notifications"))
    await session.execute(text("DELETE FROM app.agent_runs"))
    await session.execute(text("DELETE FROM app.messages"))
    account = (
        await session.execute(
            text("INSERT INTO app.accounts (telegram_user_id) VALUES (50505) RETURNING id")
        )
    ).scalar_one()
    contact = (
        await session.execute(
            text(
                "INSERT INTO app.contacts (account_id, telegram_user_id) VALUES (:a, 60606) "
                "RETURNING id"
            ),
            {"a": account},
        )
    ).scalar_one()
    conversation = (
        await session.execute(
            text(
                "INSERT INTO app.conversations (account_id, contact_id, business_chat_id) "
                "VALUES (:a, :c, 60606) RETURNING id"
            ),
            {"a": account, "c": contact},
        )
    ).scalar_one()
    return int(account), int(conversation)


async def _message(session: AsyncSession, account: int, conversation: int, ago: float) -> int:
    """A contact message sent `ago` seconds ago (no text: none is needed)."""
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO app.messages (account_id, conversation_id, direction, sent_at, "
                    "content_type) VALUES (:a, :c, 'contact', "
                    "now() - make_interval(secs => CAST(:ago AS float8)), 'photo') RETURNING id"
                ),
                {"a": account, "c": conversation, "ago": ago},
            )
        ).scalar_one()
    )


async def _triage(
    session: AsyncSession,
    account: int,
    conversation: int,
    through: int,
    finished_ago: float,
    *,
    status: str = "succeeded",
) -> None:
    await session.execute(
        text(
            "INSERT INTO app.agent_runs (account_id, run_type, trigger_ref, model, status, "
            "started_at, finished_at) VALUES (:a, 'triage', :ref, 'gpt-6-luna', :status, "
            "now() - make_interval(secs => CAST(:f AS float8) + 5), "
            "now() - make_interval(secs => CAST(:f AS float8)))"
        ),
        {
            "a": account,
            "ref": f"triage:conv:{conversation}:through:{through}",
            "status": status,
            "f": finished_ago,
        },
    )


async def _alert(
    session: AsyncSession,
    account: int,
    key: str,
    *,
    message_ids: list[int],
    sent_ago: float,
    kind: str = "triage_urgent",
    feedback: str | None = None,
    alert_class: str = "standard",
) -> None:
    await session.execute(
        text(
            "INSERT INTO app.notifications (account_id, kind, alert_class, priority, status, "
            "dedupe_key, refs, feedback, sent_at) VALUES (:a, :kind, :class, 'urgent', 'sent', "
            ":key, CAST(:refs AS jsonb), :fb, now() - make_interval(secs => CAST(:s AS float8)))"
        ),
        {
            "a": account,
            "kind": kind,
            "class": alert_class,
            "key": key,
            "refs": json.dumps({"message_ids": message_ids}),
            "fb": feedback,
            "s": sent_ago,
        },
    )


async def _measure(session: AsyncSession, days: int) -> dict[str, Any]:
    rows = (await session.execute(text(QUERY.read_text("utf-8")), {"days": days})).all()
    assert [row[0] for row in rows] == MEASURES
    return {row[0]: row for row in rows}


async def test_triage_latency_is_measured_from_the_newest_burst_message(
    db_session: AsyncSession,
) -> None:
    session = db_session
    account, conversation = await _chat(session)
    # Run k read through a message sent 3600 + 10k s ago and finished 3600 s
    # ago: 10k seconds of latency, k = 1..20.
    for k in range(1, 21):
        through = await _message(session, account, conversation, 3600 + 10 * k)
        await _triage(session, account, conversation, through, 3600)
    # Not measured: a failed run, and one with a malformed trigger_ref.
    other = await _message(session, account, conversation, 4000)
    await _triage(session, account, conversation, other, 3600, status="failed")
    await session.execute(
        text(
            "INSERT INTO app.agent_runs (account_id, run_type, trigger_ref, model, status, "
            "started_at, finished_at) VALUES (:a, 'triage', 'triage:conv:x', 'gpt-6-luna', "
            "'succeeded', now(), now())"
        ),
        {"a": account},
    )

    triage = (await _measure(session, 1))["triage completion"]
    assert (triage.n, triage.p95_limit_s) == (20, 90)
    assert (triage.p50_s, triage.p95_s) == (Decimal("105.0"), Decimal("190.5"))
    assert triage.verdict == "NOT met"


async def test_urgent_alert_latency_uses_the_newest_referenced_message(
    db_session: AsyncSession,
) -> None:
    session = db_session
    account, conversation = await _chat(session)
    older = await _message(session, account, conversation, 600)
    newer = await _message(session, account, conversation, 400)
    # Sent 340 s after the newer message (400 - 60): 340 s, not 540 s.
    await _alert(session, account, "u1", message_ids=[older, newer], sent_ago=60)
    await _alert(session, account, "u2", message_ids=[newer], sent_ago=370)  # 30 s
    # Not measured here: a decision alert and an alert with no message ids.
    await _alert(session, account, "d1", message_ids=[newer], sent_ago=60, kind="triage_decision")
    await _alert(session, account, "u3", message_ids=[], sent_ago=60)

    urgent = (await _measure(session, 1))["urgent alert delivery"]
    assert (urgent.n, urgent.p95_limit_s) == (2, 120)
    assert urgent.p50_s == Decimal("185.0")
    assert urgent.verdict == "NOT met"


async def test_the_share_of_not_useful_alerts_counts_immediate_standard_alerts(
    db_session: AsyncSession,
) -> None:
    session = db_session
    account, conversation = await _chat(session)
    message = await _message(session, account, conversation, 120)
    for i, feedback in enumerate(("not_useful", "useful", None, None)):
        await _alert(
            session, account, f"f{i}", message_ids=[message], sent_ago=60, feedback=feedback
        )
    # A system alert is not the owner's alert fatigue: not counted.
    await _alert(
        session, account, "s1", message_ids=[], sent_ago=60, kind="system", alert_class="system"
    )

    share = (await _measure(session, 1))["immediate alerts marked not useful"]
    assert (share.n, share.not_useful_pct, share.verdict) == (4, Decimal("25.0"), "informational")


async def test_rows_outside_the_window_are_ignored(db_session: AsyncSession) -> None:
    session = db_session
    account, conversation = await _chat(session)
    old = await _message(session, account, conversation, 3 * 86400)
    await _triage(session, account, conversation, old, 2 * 86400)
    await _alert(session, account, "old", message_ids=[old], sent_ago=2 * 86400)

    rows = await _measure(session, 1)
    assert [rows[m].n for m in MEASURES] == [0, 0, 0]
    assert {rows[m].verdict for m in MEASURES} == {"no data"}
    assert (await _measure(session, 3))["triage completion"].n == 1
