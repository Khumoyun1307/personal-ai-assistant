"""The NFR-04 measurement query (`deploy/sql/nfr04.sql`, `just prod <ip> nfr04`).

M03's acceptance needs p95 answer times of owner requests from `agent_runs`
timings. The query runs against the production database, so it must read no
content column; it is run here on the test database exactly as written (the
psql variable `:days` is a bind parameter for SQLAlchemy).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.privacy.content_columns import CONTENT_COLUMNS

pytestmark = pytest.mark.integration

QUERY = Path(__file__).resolve().parents[2] / "deploy" / "sql" / "nfr04.sql"


def _code() -> str:
    """The query without its comments."""
    return "\n".join(line.split("--", 1)[0] for line in QUERY.read_text("utf-8").splitlines())


def test_the_query_reads_no_content_column() -> None:
    code = _code().lower()
    for table, column in sorted(CONTENT_COLUMNS):
        assert not re.search(rf"\b{column}\b", code), f"{table}.{column} named in nfr04.sql"


def test_the_query_is_one_read_only_statement() -> None:
    code = _code().strip()
    assert code.upper().startswith("WITH ")
    assert code.count(";") == 1
    assert code.endswith(";")
    assert not re.search(r"\b(insert|update|delete|truncate|drop|alter|create)\b", code, re.I)


async def _account(session: AsyncSession, telegram_user_id: int) -> int:
    # Only this test's runs count (the transaction is rolled back afterwards).
    await session.execute(text("DELETE FROM app.agent_runs"))
    return int(
        (
            await session.execute(
                text("INSERT INTO app.accounts (telegram_user_id) VALUES (:u) RETURNING id"),
                {"u": telegram_user_id},
            )
        ).scalar_one()
    )


async def _run(
    session: AsyncSession,
    account_id: int,
    seconds: float,
    *,
    tools: Sequence[str] = (),
    waited: float = 3,
    ago: str = "1 hour",
    status: str = "succeeded",
    run_type: str = "owner_request",
    running: bool = False,
) -> None:
    """One run started `ago`, taking `seconds`; its owner turn `waited` earlier."""
    turn_id = (
        await session.execute(
            text(
                "INSERT INTO app.control_turns (account_id, role, created_at) VALUES (:a, 'owner', "
                "now() - CAST(:ago AS interval) - make_interval(secs => CAST(:w AS float8))) "
                "RETURNING id"
            ),
            {"a": account_id, "ago": ago, "w": waited},
        )
    ).scalar_one()
    run_id = (
        await session.execute(
            text(
                "INSERT INTO app.agent_runs (account_id, run_type, trigger_ref, model, status, "
                "started_at, finished_at) VALUES (:a, :type, :ref, 'gpt-6-luna', :status, "
                "now() - CAST(:ago AS interval), CASE WHEN :running THEN NULL ELSE "
                "now() - CAST(:ago AS interval) + make_interval(secs => CAST(:s AS float8)) END) "
                "RETURNING id"
            ),
            {
                "a": account_id,
                "type": run_type,
                "ref": f"turn:{turn_id}",
                "status": status,
                "ago": ago,
                "running": running,
                "s": seconds,
            },
        )
    ).scalar_one()
    for seq, tool in enumerate(tools):
        await session.execute(
            text(
                "INSERT INTO app.agent_run_steps (run_id, seq, tool_name, status) "
                "VALUES (:r, :seq, :tool, 'ok')"
            ),
            {"r": run_id, "seq": seq, "tool": tool},
        )


async def _measure(session: AsyncSession, days: int) -> dict[str, Any]:
    rows = (await session.execute(text(QUERY.read_text("utf-8")), {"days": days})).all()
    assert [row[0] for row in rows] == [
        "simple answer",
        "catch-up",
        "attention list",
        "all owner requests",
    ]
    return {row[0]: row for row in rows}


async def test_owner_requests_are_measured_per_class(db_session: AsyncSession) -> None:
    session = db_session
    account_id = await _account(session, 40404)
    # 21 simple answers taking 0..20 s, each 3 s after the owner's message.
    for seconds in range(21):
        await _run(session, account_id, seconds, tools=("search_messages",) if seconds else ())
    # Two catch-ups, 30 s and 70 s (one also listed who waits: still a catch-up).
    await _run(session, account_id, 30, tools=("summarize_period",))
    await _run(session, account_id, 70, tools=("get_attention_list", "summarize_period"))
    # Not measured as answers: a failed request (counted apart), one still
    # running, one older than the window, and a background summary run.
    await _run(session, account_id, 5, status="failed")
    await _run(session, account_id, 5, running=True)
    await _run(session, account_id, 5, ago="40 days")
    await _run(session, account_id, 5, run_type="summarize")

    rows = await _measure(session, 30)

    simple = rows["simple answer"]
    assert (simple.n, simple.not_ok) == (21, 1)
    assert (simple.run_p50_s, simple.run_p95_s) == (Decimal("10.0"), Decimal("19.0"))
    # The reply time adds the 3 s between the owner's message and the run,
    # and the verdict is taken on it: 22 s is over the 20 s limit.
    assert (simple.reply_p50_s, simple.reply_p95_s) == (Decimal("13.0"), Decimal("22.0"))
    assert (simple.reply_max_s, simple.p95_limit_s) == (Decimal("23.0"), 20)
    assert simple.verdict == "NOT met"

    catch_up = rows["catch-up"]
    assert (catch_up.n, catch_up.not_ok, catch_up.p95_limit_s) == (2, 0, 60)
    assert catch_up.reply_p95_s == Decimal("71.0")  # 33 + 0.95 * (73 - 33)
    assert catch_up.verdict == "NOT met"

    attention = rows["attention list"]
    assert (attention.n, attention.run_p95_s, attention.verdict) == (0, None, "no data")

    total = rows["all owner requests"]
    assert (total.n, total.not_ok, total.p95_limit_s) == (23, 1, None)
    assert total.verdict == "enough requests (20 or more)"


async def test_fast_answers_meet_the_limits(db_session: AsyncSession) -> None:
    session = db_session
    account_id = await _account(session, 40405)
    for seconds in (4, 6, 8):
        await _run(session, account_id, seconds, waited=1)
    await _run(session, account_id, 40, waited=1, tools=("get_attention_list",))

    rows = await _measure(session, 7)

    assert (rows["simple answer"].n, rows["simple answer"].verdict) == (3, "met")
    assert (rows["attention list"].n, rows["attention list"].verdict) == (1, "met")
    assert rows["catch-up"].verdict == "no data"
    assert rows["all owner requests"].verdict == "fewer than 20 requests"
