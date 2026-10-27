"""Control-chat /activity command and renderer (S04.5; PRD FR-ACT-01, FR-ACT-02)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.actions.activity import ActivityEntry, activity
from assistant.actions.states import (
    STATE_LABELS,
    STATE_REASON_EXPLANATIONS,
    ActionState,
    StateReason,
)
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.observability.logging import get_logger
from assistant.telegram.control.handlers import HandlerResult, IncomingMessage
from assistant.telegram.gateway import ControlChat, OutboundGateway
from assistant.telegram.render import escape

logger = get_logger(__name__)

MAX_RENDERED_ENTRIES = 30
USAGE_TEXT = "Usage: /activity [today|yesterday|7d]"


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def _format_time(moment: datetime | None, tz: ZoneInfo) -> str:
    if moment is None:
        return ""
    return moment.astimezone(tz).strftime("%H:%M")


def _explain_reason(reason_str: str | None) -> str | None:
    if not reason_str:
        return None
    try:
        return STATE_REASON_EXPLANATIONS.get(StateReason(reason_str))
    except ValueError, KeyError:
        return None


def render_activity(
    entries: Sequence[ActivityEntry],
    *,
    timezone: str,
    period_label: str,
) -> str:
    """Render activity entries for the control chat."""
    if not entries:
        return f"Nothing was sent or drafted {period_label}."

    tz = _zone(timezone)
    shown = entries[:MAX_RENDERED_ENTRIES]
    omitted = len(entries) - MAX_RENDERED_ENTRIES

    blocks: list[str] = []
    for entry in shown:
        moment = entry.executed_at or entry.decided_at or entry.created_at
        time_str = _format_time(moment, tz)
        name_str = escape(
            entry.contact_display_name
            or (f"@{entry.contact_username}" if entry.contact_username else "Unknown")
        )

        if entry.group == "sent":
            label = f"✓ Sent {time_str}"
            if entry.authorization is not None:
                auth_time = _format_time(entry.authorization.approved_at, tz)
                label += (
                    f" (approved by you at {auth_time} (version {entry.authorization.version}))"
                )
            if entry.deleted_in_chat_at is not None:
                del_time = _format_time(entry.deleted_in_chat_at, tz)
                label += f" — deleted in the chat at {del_time} (Telegram does not say by whom)"
        elif entry.group == "awaiting":
            try:
                owner_label = STATE_LABELS[ActionState(entry.state)]
            except ValueError, KeyError:
                owner_label = "Draft"
            label = f"📝 {owner_label}"
        elif entry.group == "not_sent":
            explained = _explain_reason(entry.state_reason)
            if explained:
                reason = explained
            else:
                try:
                    reason = STATE_LABELS[ActionState(entry.state)]
                except ValueError, KeyError:
                    reason = "rejected"
            label = f"✗ Not sent — {reason}"
        elif entry.group == "uncertain":
            label = "⚠ Delivery uncertain — please check the chat"
        elif entry.group == "failed":
            explained = _explain_reason(entry.state_reason)
            reason = explained if explained else "delivery failed"
            label = f"✗ Not sent — {reason}"
        elif entry.group == "blocked":
            explained = _explain_reason(entry.state_reason)
            reason = explained if explained else "sending check did not pass"
            label = f"⛔ Blocked — {reason}"
        else:
            label = entry.group

        header = f"• {time_str} {name_str}: {label}"
        block = f'{header}\n  "{escape(entry.excerpt)}"' if entry.excerpt else header
        blocks.append(block)

    if omitted > 0:
        blocks.append(f"…and {omitted} more — ask me for a narrower period.")

    return "\n\n".join(blocks)


async def handle_activity(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    message: IncomingMessage,
    argument: str,
) -> HandlerResult:
    """Handle /activity [today|yesterday|7d]."""
    arg = argument.strip().lower()
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)

    if arg not in ("", "today", "yesterday", "7d"):
        result = await gateway.send_text(target, USAGE_TEXT)
        return HandlerResult(False, "activity", result, note="usage")

    tz = _zone(account.timezone)
    now_utc = clock.now()
    now_local = now_utc.astimezone(tz)
    today_date = now_local.date()

    if arg in ("", "today"):
        period_label = "today"
        start_local = datetime.combine(today_date, time.min, tzinfo=tz)
        since = start_local.astimezone(now_utc.tzinfo)
        until = now_utc
    elif arg == "yesterday":
        period_label = "yesterday"
        yesterday_date = today_date - timedelta(days=1)
        start_local = datetime.combine(yesterday_date, time.min, tzinfo=tz)
        end_local = datetime.combine(today_date, time.min, tzinfo=tz) - timedelta(microseconds=1)
        since = start_local.astimezone(now_utc.tzinfo)
        until = end_local.astimezone(now_utc.tzinfo)
    else:  # "7d"
        period_label = "in the last 7 days"
        start_local = datetime.combine(today_date - timedelta(days=7), time.min, tzinfo=tz)
        since = start_local.astimezone(now_utc.tzinfo)
        until = now_utc

    entries = await activity(session, account_id=account.id, since=since, until=until)
    logger.info("activity_command_executed", account_id=account.id, count=len(entries))

    text = render_activity(entries, timezone=account.timezone, period_label=period_label)
    result = await gateway.send_text(target, text)
    return HandlerResult(True, "activity", result, note=arg or "today")
