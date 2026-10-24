"""Coverage: what the owner can and cannot have seen (ARCHITECTURE §6.6).

Two writers and one reader:

- `record_outage` — the poller noticed that its last successful poll was long
  ago. Messages dated inside an outage longer than Telegram's 24 h retention
  may be gone for good (`possible`); a shorter one only delayed them.
- `record_ingest_failure` lives in `service.py` (a dead letter is `definite`).
- `caveat` — the one-line warning shown while a conversation is paused by an
  ingest error or a possible/definite gap is unresolved (FR-OBS-05, §6
  invariant 11: never report a gap as coverage). `summarize` (search answers)
  counts every unresolved gap; `/status` (`observability.status`) warns only
  about those `status_gap_filter` keeps — outage/restore gaps that ended less
  than `STATUS_GAP_WINDOW` ago, and dead-letter gaps of any age — and counts
  the older ones (`older_gap_filter`) on a plain line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.conversations.models import Conversation, CoverageGap
from assistant.conversations.service import PAUSE_INGEST_ERROR
from assistant.core.clock import Clock

#: Telegram keeps undelivered updates this long (TF §3.1). An outage longer
#: than this, minus a margin, can have lost messages for good.
TELEGRAM_UPDATE_RETENTION = timedelta(hours=24)
OUTAGE_MARGIN = timedelta(minutes=10)
#: Shorter outages are not worth a row: the watchdog already alerted.
OUTAGE_MIN = timedelta(minutes=3)

#: `/status` warns about an outage or restore gap only while its risk window
#: ended less than this long ago (strictly; owner, 2026-10-23, M02 D57): after
#: a week the warning is noise on every `/status`. Older ones are still
#: counted, on a plain "older gaps" line (PRD FR-ONB-08: known gaps are
#: shown). The same window applies to `delayed` outage gaps. Unresolved
#: dead-letter gaps and ingest pauses warn until resolved, whatever their age.
#: Search answers (`summarize`) are not windowed: they count every unresolved
#: gap. S03.2 will decide how answers about a date range (summaries,
#: catch-up) use gaps; date-range overlap is the plan, not this window.
STATUS_GAP_WINDOW = timedelta(days=7)

_WARNING_CERTAINTIES = ("possible", "definite")


def _recent_or_dead_letter(now: datetime) -> ColumnElement[bool]:
    return or_(
        CoverageGap.kind == "dead_letter",
        CoverageGap.at_risk_to > now - STATUS_GAP_WINDOW,
    )


def status_gap_filter(now: datetime) -> ColumnElement[bool]:
    """The unresolved `possible`/`definite` gaps `/status` warns about at `now`."""
    return and_(
        CoverageGap.resolved_at.is_(None),
        CoverageGap.certainty.in_(_WARNING_CERTAINTIES),
        _recent_or_dead_letter(now),
    )


def status_delayed_filter(now: datetime) -> ColumnElement[bool]:
    """The unresolved `delayed` gaps `/status` counts at `now` (same window)."""
    return and_(
        CoverageGap.resolved_at.is_(None),
        CoverageGap.certainty == "delayed",
        _recent_or_dead_letter(now),
    )


def older_gap_filter(now: datetime) -> ColumnElement[bool]:
    """Unresolved gaps of any certainty that fell out of the window: shown on
    `/status` as a count, without a warning."""
    return and_(
        CoverageGap.resolved_at.is_(None),
        CoverageGap.certainty.in_((*_WARNING_CERTAINTIES, "delayed")),
        CoverageGap.kind != "dead_letter",
        CoverageGap.at_risk_to <= now - STATUS_GAP_WINDOW,
    )


async def record_outage(
    session: AsyncSession, *, last_ok: datetime, resumed: datetime, clock: Clock
) -> int:
    """One gap per active account for an outage between two successful polls.

    `possible` if longer than 24 h - 10 min, with the at-risk range
    `[last_ok, resumed - 24 h + 10 min]` (only messages dated in it can have
    expired); otherwise `delayed`. Returns the number of rows written.
    """
    length = resumed - last_ok
    if length < OUTAGE_MIN:
        return 0
    threshold = TELEGRAM_UPDATE_RETENTION - OUTAGE_MARGIN
    if length > threshold:
        certainty = "possible"
        at_risk_to = resumed - threshold
    else:
        certainty = "delayed"
        at_risk_to = resumed
    accounts = await session.execute(select(Account.id).where(Account.status == "active"))
    written = 0
    for account_id in accounts.scalars().all():
        session.add(
            CoverageGap(
                account_id=account_id,
                conversation_id=None,
                kind="outage",
                certainty=certainty,
                at_risk_from=last_ok,
                at_risk_to=max(at_risk_to, last_ok),
                created_at=clock.now(),
            )
        )
        written += 1
    return written


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    paused_by_ingest_error: int
    unresolved_possible_gaps: int
    unresolved_definite_gaps: int

    @property
    def caveat(self) -> str | None:
        """The warning line, or None when coverage is clean."""
        parts: list[str] = []
        if self.paused_by_ingest_error:
            parts.append(
                f"{self.paused_by_ingest_error} chat(s) could not be processed and are "
                "paused for automation"
            )
        if self.unresolved_definite_gaps or self.unresolved_possible_gaps:
            parts.append(
                f"{self.unresolved_definite_gaps} definite and "
                f"{self.unresolved_possible_gaps} possible coverage gap(s): "
                "some messages may be missing"
            )
        return "⚠️ " + "; ".join(parts) + "." if parts else None


async def summarize(session: AsyncSession, account_id: int) -> CoverageSummary:
    paused = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.account_id == account_id)
            .where(Conversation.autonomy_paused_reason == PAUSE_INGEST_ERROR)
        )
    ).scalar_one()
    gaps = await session.execute(
        select(CoverageGap.certainty, func.count())
        .where(CoverageGap.account_id == account_id)
        .where(CoverageGap.resolved_at.is_(None))
        .where(CoverageGap.certainty.in_(["possible", "definite"]))
        .group_by(CoverageGap.certainty)
    )
    counts = dict(gaps.all())
    return CoverageSummary(
        paused_by_ingest_error=int(paused),
        unresolved_possible_gaps=int(counts.get("possible", 0)),
        unresolved_definite_gaps=int(counts.get("definite", 0)),
    )
