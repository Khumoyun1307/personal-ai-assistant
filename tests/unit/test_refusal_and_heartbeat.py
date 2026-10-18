"""The non-owner refusal limiter and the heartbeat URL guard (S01.5)."""

from __future__ import annotations

from datetime import timedelta

from assistant.observability import heartbeat
from assistant.telegram.control.inbox_handlers import RefusalLimiter
from tests.fakes.clock import FakeClock


def test_one_refusal_per_chat_per_window() -> None:
    clock = FakeClock()
    limiter = RefusalLimiter(clock, per_chat_window=timedelta(hours=1))

    assert limiter.allow(7000000002) is True
    assert limiter.allow(7000000002) is False
    assert limiter.allow(7000000003) is True
    clock.advance(hours=1, seconds=1)
    assert limiter.allow(7000000002) is True


def test_a_global_cap_stops_a_flood_of_new_chats() -> None:
    clock = FakeClock()
    limiter = RefusalLimiter(clock, global_window=timedelta(minutes=10), global_limit=3)

    allowed = [limiter.allow(7000000100 + n) for n in range(5)]

    assert allowed == [True, True, True, False, False]
    clock.advance(minutes=11)
    assert limiter.allow(7000000200) is True


def test_the_limiter_keeps_no_raw_chat_ids() -> None:
    limiter = RefusalLimiter(FakeClock())

    limiter.allow(7000000002)

    assert "7000000002" not in repr(limiter._last_by_chat)


async def test_heartbeat_refuses_non_http_urls() -> None:
    assert await heartbeat.ping("file:///etc/passwd") is False
