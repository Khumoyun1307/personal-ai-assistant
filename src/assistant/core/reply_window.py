"""Pure Telegram reply-window calculation shared by conversations and policy."""

from datetime import datetime, timedelta

REPLY_WINDOW = timedelta(hours=24)
SAFETY_MARGIN = timedelta(minutes=5)


def closes_at(anchor: datetime | None) -> datetime | None:
    return None if anchor is None else anchor + REPLY_WINDOW


def is_open(anchor: datetime | None, now: datetime) -> bool:
    close = closes_at(anchor)
    return close is not None and now < close - SAFETY_MARGIN
