"""Process-wide state that belongs to no tenant (ARCHITECTURE §10.3).

Deliberately has no `account_id`: the poller heartbeat, the bot id and the
restore flag are properties of the deployment, not of an owner. It is the only
table exempt from tenant scoping, which is why it lives here rather than in a
domain package.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt

#: Well-known keys, named here so they are not spelled differently per module.
KEY_BOT_ID = "bot_id"
KEY_POLLER_HEARTBEAT = "poller_heartbeat"
KEY_RESTORE_PENDING = "restore_pending"
KEY_NORMALIZER_VERSION = "normalizer_version"
#: When the poller last completed a `getUpdates` and committed what it got.
#: Unlike the heartbeat it is never written at startup, so the interval
#: between two values is a real outage (ARCHITECTURE §6.6).
KEY_LAST_POLL_OK = "last_poll_ok"


class SystemState(Base):
    """A small key/value store for deployment state."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    updated_at: Mapped[CreatedAt]
