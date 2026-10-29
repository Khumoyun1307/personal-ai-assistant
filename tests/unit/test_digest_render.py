"""S05.3b (QA B3 on #117): the digest is rendered as parts that are each
exactly one control-chat message after the gateway's sanitising, so the
gateway never splits one again and `DigestDelivery` knows which parts reached
the owner (a partial delivery is never reported as "not delivered").

Synthetic names and reasons only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assistant.inbox.digest import ChatLine, DigestContent
from assistant.telegram.control.digest import CONTINUED_TEXT, render_digest
from assistant.telegram.render import render_control_message

END = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)


def content(chats: int) -> DigestContent:
    # `&` and `<…>` grow on escaping (twice: the renderer, then the gateway),
    # so a part that fits before escaping may not fit after it.
    return DigestContent(
        account_id=1,
        period_start=END - timedelta(days=1),
        period_end=END,
        timezone="Asia/Tashkent",
        needs_you=tuple(
            ChatLine(index, f"Synthetic <b>&</b> contact {index:03d}", "Synthetic & <why> " * 12)
            for index in range(chats)
        ),
    )


def test_a_short_digest_is_one_part() -> None:
    [part] = render_digest(content(3))
    assert CONTINUED_TEXT not in part
    assert part.startswith("📋 <b>Digest</b>")


def test_every_part_of_a_long_digest_is_one_message_after_escaping() -> None:
    parts = render_digest(content(120))
    assert len(parts) > 2
    for part in parts:
        assert len(render_control_message(part)) == 1
        assert part == part.strip("\n")
    assert all(part.endswith(f"\n{CONTINUED_TEXT}") for part in parts[:-1])
    assert not parts[-1].endswith(CONTINUED_TEXT)
    # Every line is in exactly one part, in order.
    joined = "\n".join(parts)
    positions = [joined.find(f"contact {index:03d}") for index in range(120)]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)
    assert all(joined.count(f"contact {index:03d}") == 1 for index in range(120))
