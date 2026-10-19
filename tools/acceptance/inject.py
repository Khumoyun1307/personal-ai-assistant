"""M01 acceptance driver: synthetic control-chat updates, written to the inbox.

Outside `src/` and never imported by it (AGENTS §5). A bot cannot send as the
owner into its own chat, so acceptance runs that need many owner messages put
**synthetic** updates straight into `inbound_updates`, exactly as the poller
would after `getUpdates`. Everything after that — routing, consent, the agent,
OpenAI, the reply in the owner's real chat — is the real pipeline, and the
acknowledgement latency is measured from that row's `received_at`, which is
the acceptance definition.

Update ids are taken from a reserved range far above Telegram's, so they can
never collide with a real update. Text is the agent's own synthetic text.

    python tools/acceptance/inject.py owner "Reply with just: ok"
    python tools/acceptance/inject.py latency --count 30
    python tools/acceptance/inject.py check-replies

Run inside the dev container, while `python -m assistant run` is running.
From Git Bash on Windows, set `MSYS_NO_PATHCONV=1` for arguments starting
with `/` (e.g. `/status`), or Git Bash rewrites them into Windows paths.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import time
from typing import Any

from sqlalchemy import func, select, text

from assistant.accounts.models import ControlTurn
from assistant.config import Settings
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import KEY_BOT_ID
from assistant.telegram.ingress.writer import read_state, write_batch

#: Far above real Telegram update ids, which are around 6e8 for this bot.
SYNTHETIC_UPDATE_BASE = 9_000_000_000


def owner_message(owner_id: int, update_id: int, message_text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id % 1_000_000,
            "date": int(time.time()),
            "chat": {"id": owner_id, "type": "private"},
            "from": {"id": owner_id, "is_bot": False, "first_name": "Synthetic"},
            "text": message_text,
        },
    }


class Driver:
    def __init__(self) -> None:
        settings = Settings()  # type: ignore[call-arg]
        if not settings.owner_telegram_ids:
            raise SystemExit("OWNER_TELEGRAM_IDS is empty")
        self.owner_id = settings.owner_telegram_ids[0]
        self.engine = build_engine(settings.database_url.get_secret_value())
        self.factory = build_session_factory(self.engine)

    async def _next_update_id(self) -> int:
        async with self.factory() as session:
            highest = (
                await session.execute(
                    text("SELECT max(update_id) FROM app.inbound_updates WHERE update_id >= :b"),
                    {"b": SYNTHETIC_UPDATE_BASE},
                )
            ).scalar_one()
        return SYNTHETIC_UPDATE_BASE if highest is None else int(highest) + 1

    async def inject(self, message_text: str) -> int:
        # Two injections racing for the same id would silently drop one (the
        # unique key makes a duplicate a no-op), so retry with the next id.
        for _ in range(20):
            async with self.factory() as session, session.begin():
                bot = await read_state(session, KEY_BOT_ID)
                if not bot:
                    raise SystemExit("the assistant has not started on this database yet")
                update_id = await self._next_update_id()
                written = await write_batch(
                    session,
                    bot_id=int(bot["id"]),
                    updates=[owner_message(self.owner_id, update_id, message_text)],
                )
            if written:
                return update_id
        raise SystemExit("could not reserve a synthetic update id")

    async def assistant_turns(self) -> int:
        async with self.factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ControlTurn)
                        .where(ControlTurn.role == "assistant")
                    )
                ).scalar_one()
            )

    async def wait_for_replies(self, target: int, within_s: float = 90) -> bool:
        deadline = time.monotonic() + within_s
        while time.monotonic() < deadline:
            if await self.assistant_turns() >= target:
                return True
            await asyncio.sleep(0.2)
        return False

    async def check_replies(self) -> None:
        """Every owner turn answered exactly once (no loss, no duplicates)."""
        async with self.factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT t.id,
                               (SELECT count(*) FROM app.agent_runs r
                                 WHERE r.trigger_ref = 'turn:' || t.id
                                   AND r.status <> 'failed') AS answered,
                               (SELECT count(*) FROM app.agent_runs r
                                 WHERE r.trigger_ref = 'turn:' || t.id) AS runs
                        FROM app.control_turns t
                        WHERE t.role = 'owner'
                        ORDER BY t.id
                        """
                    )
                )
            ).all()
        unanswered = [row[0] for row in rows if row[1] == 0]
        doubled = [row[0] for row in rows if row[1] > 1]
        print(f"owner turns: {len(rows)}  unanswered: {unanswered}  answered twice: {doubled}")
        print(
            f"turns with more than one run (interrupted + redone): "
            f"{[row[0] for row in rows if row[2] > 1]}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(prog="inject")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("owner")
    one.add_argument("text")
    many = sub.add_parser("latency")
    many.add_argument("--count", type=int, default=30)
    sub.add_parser("check-replies")
    args = parser.parse_args()

    driver = Driver()
    try:
        if args.command == "owner":
            print(f"injected update {await driver.inject(args.text)}")
        elif args.command == "latency":
            # One at a time, each after the previous reply: per-message latency
            # in a normal conversation, not queueing behind a burst.
            for n in itertools.count(1):
                if n > args.count:
                    break
                before = await driver.assistant_turns()
                await driver.inject(f"Reply with only the word ok. ({n})")
                if not await driver.wait_for_replies(before + 1):
                    raise SystemExit(f"no reply to message {n} within 90 s")
                print(f"{n}/{args.count} answered", flush=True)
        else:
            await driver.check_replies()
    finally:
        await driver.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
