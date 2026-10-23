"""The visibility report — "What can you see?" (FR-ONB-08, ARCHITECTURE §17).

One content-free read model, built from the database, rendered twice: as the
`/status` command (adapter) and as the `get_status` tool result (agent). Both
must say the same thing, so neither computes anything itself.

Content-free means: no contact names, no message text — counts, states,
times and ids only. The report therefore never taints a run and needs no
`ai_managed` consent to be shown to the model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import connections
from assistant.accounts.models import Account
from assistant.accounts.service import CONSENT_AI_CONTROL, CONSENT_AI_MANAGED, active_consent
from assistant.conversations.coverage import status_gap_filter
from assistant.conversations.models import Conversation, CoverageGap
from assistant.conversations.service import PAUSE_INGEST_ERROR
from assistant.inbox.processor import inbox_backlog_for
from assistant.inbox.watchdog import poller_heartbeat_at


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    present: bool
    status: str | None = None
    tier: str | None = None
    rights: tuple[str, ...] = ()
    connected_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GapRange:
    certainty: str
    at_risk_from: datetime
    at_risk_to: datetime


@dataclass(frozen=True, slots=True)
class CoverageStatus:
    conversations: int
    by_mode: dict[str, int]
    coverage_started_at: datetime | None
    unresolved_possible: int
    unresolved_definite: int
    delayed: int
    latest_gap: GapRange | None
    paused_by_ingest_error: int


@dataclass(frozen=True, slots=True)
class ConsentStatus:
    control_chat: bool
    managed_chats: bool
    policy_version: str | None


@dataclass(frozen=True, slots=True)
class AutonomyStatus:
    paused: bool
    conversations_paused: dict[str, int]


@dataclass(frozen=True, slots=True)
class InboxStatus:
    pending: int
    failed: int
    poller_heartbeat_age_seconds: int | None


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    month_to_date_usd: str
    monthly_limit_usd: str | None


@dataclass(frozen=True, slots=True)
class StatusReport:
    account_status: str
    timezone: str
    connection: ConnectionStatus
    coverage: CoverageStatus
    consent: ConsentStatus
    autonomy: AutonomyStatus
    inbox: InboxStatus
    budget: BudgetStatus

    @property
    def coverage_caveat(self) -> str | None:
        """The honest-coverage line (§6 invariant 11), or None when clean."""
        cov = self.coverage
        parts: list[str] = []
        if cov.paused_by_ingest_error:
            parts.append(
                f"{cov.paused_by_ingest_error} chat(s) could not be processed and are "
                "paused for automation"
            )
        if cov.unresolved_definite or cov.unresolved_possible:
            parts.append(
                f"{cov.unresolved_definite} definite and {cov.unresolved_possible} possible "
                "coverage gap(s): some messages may be missing"
            )
        return "⚠️ " + "; ".join(parts) + "." if parts else None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready, for the tool result. Datetimes become ISO strings."""

        def convert(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list | tuple):
                return [convert(v) for v in value]
            return value

        data = convert(asdict(self))
        data["coverage_caveat"] = self.coverage_caveat
        return dict(data)


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def build_status(
    session: AsyncSession,
    account: Account,
    now: datetime,
    *,
    monthly_budget_usd: Decimal | None,
    in_flight_rows: int = 0,
) -> StatusReport:
    """`in_flight_rows`: inbox rows the caller knows are being handled right
    now (the `/status` command itself), so they do not read as a backlog."""
    account_id = account.id
    connection = await connections.connection_for_account(session, account_id)
    connection_status = (
        ConnectionStatus(False)
        if connection is None
        else ConnectionStatus(
            True,
            status=connection.status,
            tier=connection.capability_tier,
            rights=tuple(sorted(connection.rights or [])),
            connected_at=connection.connected_at,
        )
    )

    modes = await session.execute(
        select(Conversation.mode, func.count())
        .where(Conversation.account_id == account_id)
        .group_by(Conversation.mode)
    )
    by_mode = {str(mode): int(count) for mode, count in modes.all()}
    started = (
        await session.execute(
            select(func.min(Conversation.coverage_started_at)).where(
                Conversation.account_id == account_id
            )
        )
    ).scalar_one()
    paused_rows = await session.execute(
        select(Conversation.autonomy_paused_reason, func.count())
        .where(Conversation.account_id == account_id)
        .where(Conversation.autonomy_paused_reason.is_not(None))
        .group_by(Conversation.autonomy_paused_reason)
    )
    paused_by_reason = {str(reason): int(count) for reason, count in paused_rows.all()}
    # Outage and restore gaps older than a week no longer warn; dead-letter
    # gaps warn until resolved (M02 D57, `coverage.STATUS_GAP_WINDOW`).
    gap_counts = await session.execute(
        select(CoverageGap.certainty, func.count())
        .where(CoverageGap.account_id == account_id)
        .where(status_gap_filter(now))
        .group_by(CoverageGap.certainty)
    )
    gaps = {str(certainty): int(count) for certainty, count in gap_counts.all()}
    delayed = (
        await session.execute(
            select(func.count())
            .select_from(CoverageGap)
            .where(CoverageGap.account_id == account_id)
            .where(CoverageGap.resolved_at.is_(None))
            .where(CoverageGap.certainty == "delayed")
        )
    ).scalar_one()
    latest = (
        await session.execute(
            select(CoverageGap)
            .where(CoverageGap.account_id == account_id)
            .where(status_gap_filter(now))
            .order_by(CoverageGap.at_risk_to.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    coverage = CoverageStatus(
        conversations=sum(by_mode.values()),
        by_mode=by_mode,
        coverage_started_at=started,
        unresolved_possible=gaps.get("possible", 0),
        unresolved_definite=gaps.get("definite", 0),
        delayed=int(delayed),
        latest_gap=None
        if latest is None
        else GapRange(latest.certainty, latest.at_risk_from, latest.at_risk_to),
        paused_by_ingest_error=paused_by_reason.get(PAUSE_INGEST_ERROR, 0),
    )

    control = await active_consent(session, account_id, kind=CONSENT_AI_CONTROL)
    managed = await active_consent(session, account_id, kind=CONSENT_AI_MANAGED)
    consent = ConsentStatus(
        control_chat=control.granted,
        managed_chats=managed.granted,
        policy_version=managed.policy_version or control.policy_version,
    )

    backlog = await inbox_backlog_for(session, account_id)
    heartbeat = await poller_heartbeat_at(session)
    age = None if heartbeat is None else max(int((now - heartbeat).total_seconds()), 0)
    inbox = InboxStatus(
        pending=max(backlog.get("pending", 0) - in_flight_rows, 0),
        failed=backlog.get("failed", 0),
        poller_heartbeat_age_seconds=age,
    )

    spent = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(cost_estimate), 0) FROM app.llm_calls "
                "WHERE account_id = :account_id AND created_at >= :since"
            ),
            {"account_id": account_id, "since": month_start(now)},
        )
    ).scalar_one()
    budget = BudgetStatus(
        month_to_date_usd=f"{Decimal(spent):.4f}",
        monthly_limit_usd=None if monthly_budget_usd is None else f"{monthly_budget_usd}",
    )

    return StatusReport(
        account_status=account.status,
        timezone=account.timezone,
        connection=connection_status,
        coverage=coverage,
        consent=consent,
        autonomy=AutonomyStatus(
            paused=account.autonomy_paused, conversations_paused=paused_by_reason
        ),
        inbox=inbox,
        budget=budget,
    )


def heartbeat_is_fresh(report: StatusReport, within: timedelta) -> bool:
    age = report.inbox.poller_heartbeat_age_seconds
    return age is not None and age <= within.total_seconds()
