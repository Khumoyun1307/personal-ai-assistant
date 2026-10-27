"""Notification enqueue contract; S05.2 implements delivery and policy."""

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.clock import Clock
from assistant.inbox.notification_types import (
    AlertClass,
    NotificationKind,
    NotificationRefs,
    Priority,
)


async def enqueue(
    session: AsyncSession,
    *,
    account_id: int,
    kind: NotificationKind,
    priority: Priority,
    dedupe_key: str,
    refs: NotificationRefs,
    clock: Clock,
    alert_class: AlertClass = AlertClass.STANDARD,
    conversation_id: int | None = None,
    contact_id: int | None = None,
    bypass_quiet_hours: bool = False,
) -> int | None:
    raise NotImplementedError("Notification enqueue is implemented in S05.2")
