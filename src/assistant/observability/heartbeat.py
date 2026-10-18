"""External dead-man's-switch ping (ARCHITECTURE §17).

Every 5 minutes, only while ingestion is healthy, the app GETs `HEARTBEAT_URL`.
If the pings stop, the external monitor alerts the owner by email or push —
the one failure the bot cannot report itself (the app is down or cannot reach
Telegram).

No data is sent. The URL is a secret (it identifies the monitor), so it is
never logged. Uses the standard library in a thread: no new dependency.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

from assistant.observability.logging import get_logger

logger = get_logger(__name__)

PING_TIMEOUT_SECONDS = 10


def _get(url: str) -> int:
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - operator-configured URL
    with urllib.request.urlopen(request, timeout=PING_TIMEOUT_SECONDS) as response:  # noqa: S310
        return int(response.status)


async def ping(url: str) -> bool:
    """True if the monitor answered 2xx. Failures are logged by type only."""
    if not url.startswith(("https://", "http://")):
        # urllib would also open file:// and other schemes; a monitor is HTTP.
        logger.warning("heartbeat_url_rejected", detail="scheme must be http or https")
        return False
    try:
        status = await asyncio.to_thread(_get, url)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        logger.warning("heartbeat_ping_failed", exc_type=type(error).__name__)
        return False
    ok = 200 <= status < 300
    if not ok:
        logger.warning("heartbeat_ping_rejected", status=status)
    return ok
