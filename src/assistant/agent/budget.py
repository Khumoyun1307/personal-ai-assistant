"""Monthly LLM budget alerts (NFR-10, ARCHITECTURE §7.4).

Soft alerts at 50 % and 80 % of `LLM_MONTHLY_BUDGET_USD`, and one at 100 %
(the runtime's hard stop is separate and always on). Each threshold alerts once
per UTC month, through `notify_owner`.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.persistence import LlmCallRepository, month_start
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.notify import AlertKind, OwnerAlerts

THRESHOLDS_PERCENT = (50, 80, 100)


async def check_budget_thresholds(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    alerts: OwnerAlerts,
    *,
    account_id: int,
    monthly_budget_usd: Decimal | None,
) -> list[int]:
    """Alert for every threshold crossed this month. Returns thresholds alerted now."""
    if monthly_budget_usd is None or monthly_budget_usd <= 0:
        return []
    start = month_start(clock.now())
    async with transaction(session_factory) as uow:
        spent = await LlmCallRepository(uow.session, account_id).spend_since(start)
    sent: list[int] = []
    for percent in THRESHOLDS_PERCENT:
        if spent * 100 < monthly_budget_usd * percent:
            break
        outcome = await alerts.notify_owner(
            account_id,
            AlertKind.BUDGET_THRESHOLD,
            dedupe_key=f"budget:{start:%Y-%m}:{percent}",
            percent=percent,
        )
        if outcome.sent:
            sent.append(percent)
    return sent
