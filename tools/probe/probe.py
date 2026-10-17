"""M00 S00.2 Telegram probe — throwaway, owner-run.

Never imported by `src/` (AGENTS §5). Its only job is to answer LV-01…LV-19
(TELEGRAM_FEASIBILITY §11) against real accounts and record exactly what
Telegram sends and returns.

**The owner runs this, never a coding agent** (AGENTS §9): it needs a real
BOT_TOKEN, and the raw dumps it writes under `var/probe/` contain real chat
content. Fixtures for `tests/fixtures/telegram/` are produced from those dumps
by `sanitize.py`, which the owner also runs; the agent sees only its output.

Rules this script obeys, the same as the application:
  - an explicit `allowed_updates` list (the setting is sticky server-side);
  - `deleteWebhook(drop_pending_updates=False)` — never True, it would
    permanently discard up to 24 h of business messages;
  - full payloads are written only for allow-listed chat ids; every other chat
    is reduced to its update type, `update_id` and a hash of `chat.id`.

Usage (inside the dev container, with BOT_TOKEN set in .env):

    just probe me
    just probe listen
    just probe send <connection_id> <chat_id> "text"
    ...

or directly:

    docker compose -f compose.dev.yml run --rm app \
        python tools/probe/probe.py <command> [args]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError

DUMP_DIR = Path("var/probe")

#: Every update type the product will ever ask for. Explicit, because the
#: server remembers the last list passed to getUpdates/setWebhook.
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]


def allowed_chat_ids() -> set[int]:
    raw = os.environ.get("PROBE_ALLOWED_CHAT_IDS", "")
    return {int(part) for part in raw.replace(" ", "").split(",") if part}


def hash_id(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def extract_chat_ids(payload: Any) -> set[int]:
    """Every `chat.id` / `user_chat_id` anywhere in the update."""
    found: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"chat", "sender_chat"} and isinstance(value, dict):
                chat_id = value.get("id")
                if isinstance(chat_id, int):
                    found.add(chat_id)
            elif key == "user_chat_id" and isinstance(value, int):
                found.add(value)
            else:
                found |= extract_chat_ids(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= extract_chat_ids(item)
    return found


def redact_for_unlisted(payload: dict[str, Any], update_type: str) -> dict[str, Any]:
    """A chat we were not told to observe: keep metadata, drop everything else."""
    return {
        "update_id": payload.get("update_id"),
        "update_type": update_type,
        "chat_id_hashes": sorted(hash_id(cid) for cid in extract_chat_ids(payload)),
        "observed_at": datetime.now(UTC).isoformat(),
        "note": "chat not allow-listed; payload withheld",
    }


def update_type_of(payload: dict[str, Any]) -> str:
    for key in payload:
        if key != "update_id":
            return key
    return "unknown"


def write_record(record: dict[str, Any]) -> None:
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    path = DUMP_DIR / f"updates-{datetime.now(UTC):%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def log_result(label: str, value: Any) -> None:
    """Record an API call's outcome — including the exact error (LV-08)."""
    record = {
        "kind": "api_result",
        "label": label,
        "observed_at": datetime.now(UTC).isoformat(),
        "result": value,
    }
    write_record(record)
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))


def make_bot() -> Bot:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("BOT_TOKEN is not set. Put it in .env (never paste it in chat).", file=sys.stderr)
        raise SystemExit(2)
    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))


async def with_bot(coro_name: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    bot = make_bot()
    try:
        result = await fn(bot, *args, **kwargs)
        log_result(coro_name, result.model_dump() if hasattr(result, "model_dump") else result)
    except TelegramAPIError as error:
        # The exact string and class are the deliverable for LV-04 and LV-08.
        log_result(
            coro_name,
            {
                "error_class": type(error).__name__,
                "message": str(error),
                "retry_after": getattr(error, "retry_after", None),
            },
        )
    finally:
        await bot.session.close()


# ----------------------------------------------------------------- commands
async def cmd_me(_args: argparse.Namespace) -> None:
    async def run(bot: Bot) -> Any:
        me = await bot.get_me()
        if not me.can_connect_to_business:
            print(
                "WARNING: can_connect_to_business is false — enable Secretary Mode "
                "in @BotFather before running the LV steps.",
                file=sys.stderr,
            )
        return me

    await with_bot("getMe", run)


async def cmd_listen(args: argparse.Namespace) -> None:
    bot = make_bot()
    listed = allowed_chat_ids()
    print(f"allow-listed chat ids: {sorted(listed) or 'NONE — all payloads withheld'}")
    print(f"allowed_updates: {ALLOWED_UPDATES}")
    # Never drop_pending_updates=True: it discards up to 24 h of business messages.
    await bot.delete_webhook(drop_pending_updates=False)
    offset: int | None = None
    try:
        while True:
            updates = await bot.get_updates(
                offset=offset, timeout=args.timeout, allowed_updates=ALLOWED_UPDATES
            )
            for update in updates:
                offset = update.update_id + 1
                payload = update.model_dump(exclude_none=True, mode="json")
                kind = update_type_of(payload)
                chats = extract_chat_ids(payload)
                if chats and listed and not (chats & listed):
                    record = redact_for_unlisted(payload, kind)
                else:
                    record = {
                        "kind": "update",
                        "update_type": kind,
                        "observed_at": datetime.now(UTC).isoformat(),
                        "payload": payload,
                    }
                write_record(record)
                print(f"[{record.get('update_type')}] update_id={payload.get('update_id')}")
    except KeyboardInterrupt, asyncio.CancelledError:
        print("\nstopped; offset NOT advanced past the last processed update")
    finally:
        await bot.session.close()


async def cmd_connection(args: argparse.Namespace) -> None:
    await with_bot(
        "getBusinessConnection",
        lambda bot: bot.get_business_connection(business_connection_id=args.connection_id),
    )


async def cmd_send(args: argparse.Namespace) -> None:
    await with_bot(
        "sendMessage",
        lambda bot: bot.send_message(
            chat_id=args.chat_id,
            text=args.text,
            business_connection_id=args.connection_id,
        ),
    )


async def cmd_dm(args: argparse.Namespace) -> None:
    """LV-09: can the bot DM the owner before /start? No business connection."""
    await with_bot(
        "sendMessage(no connection)",
        lambda bot: bot.send_message(chat_id=args.chat_id, text=args.text),
    )


async def cmd_edit(args: argparse.Namespace) -> None:
    await with_bot(
        "editMessageText",
        lambda bot: bot.edit_message_text(
            chat_id=args.chat_id,
            message_id=args.message_id,
            text=args.text,
            business_connection_id=args.connection_id,
        ),
    )


async def cmd_typing(args: argparse.Namespace) -> None:
    await with_bot(
        "sendChatAction",
        lambda bot: bot.send_chat_action(
            chat_id=args.chat_id,
            action="typing",
            business_connection_id=args.connection_id,
        ),
    )


async def cmd_delete(args: argparse.Namespace) -> None:
    await with_bot(
        "deleteBusinessMessages",
        lambda bot: bot.delete_business_messages(
            business_connection_id=args.connection_id,
            message_ids=args.message_ids,
        ),
    )


async def cmd_getfile(args: argparse.Namespace) -> None:
    """LV-11: file availability now, after 24 h and after 7 days."""

    async def run(bot: Bot) -> Any:
        file = await bot.get_file(args.file_id)
        if args.download and file.file_path:
            target = DUMP_DIR / "files" / Path(file.file_path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            await bot.download_file(file.file_path, destination=target)
            return {"file": file.model_dump(), "downloaded_to": str(target)}
        return file

    await with_bot("getFile", run)


async def cmd_sendphoto(args: argparse.Namespace) -> None:
    """LV-11: re-send a previously received photo by file_id."""
    await with_bot(
        "sendPhoto(by file_id)",
        lambda bot: bot.send_photo(
            chat_id=args.chat_id,
            photo=args.file_id,
            business_connection_id=args.connection_id,
        ),
    )


async def cmd_burst(args: argparse.Namespace) -> None:
    """LV-16: five sends in quick succession; record any 429 and retry_after."""
    bot = make_bot()
    try:
        for index in range(args.count):
            try:
                message = await bot.send_message(
                    chat_id=args.chat_id,
                    text=f"{args.text} #{index + 1}",
                    business_connection_id=args.connection_id,
                )
                log_result(f"burst[{index + 1}]", message.model_dump())
            except TelegramAPIError as error:
                log_result(
                    f"burst[{index + 1}]",
                    {
                        "error_class": type(error).__name__,
                        "message": str(error),
                        "retry_after": getattr(error, "retry_after", None),
                    },
                )
    finally:
        await bot.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("me", help="getMe; warns if Secretary Mode is off")

    listen = sub.add_parser("listen", help="long-poll and dump updates to var/probe/")
    listen.add_argument("--timeout", type=int, default=30)

    conn = sub.add_parser("connection", help="getBusinessConnection (LV-01, LV-05)")
    conn.add_argument("connection_id")

    send = sub.add_parser("send", help="sendMessage through a business connection")
    send.add_argument("connection_id")
    send.add_argument("chat_id", type=int)
    send.add_argument("text")

    dm = sub.add_parser("dm", help="sendMessage with NO business connection (LV-09)")
    dm.add_argument("chat_id", type=int)
    dm.add_argument("text")

    edit = sub.add_parser("edit", help="editMessageText (LV-08, LV-15)")
    edit.add_argument("connection_id")
    edit.add_argument("chat_id", type=int)
    edit.add_argument("message_id", type=int)
    edit.add_argument("text")

    typing = sub.add_parser("typing", help="sendChatAction typing (LV-08)")
    typing.add_argument("connection_id")
    typing.add_argument("chat_id", type=int)

    delete = sub.add_parser("delete", help="deleteBusinessMessages (LV-07)")
    delete.add_argument("connection_id")
    delete.add_argument("message_ids", type=int, nargs="+")

    getfile = sub.add_parser("getfile", help="getFile, optionally downloading (LV-11)")
    getfile.add_argument("file_id")
    getfile.add_argument("--download", action="store_true")

    photo = sub.add_parser("sendphoto", help="re-send a photo by file_id (LV-11)")
    photo.add_argument("connection_id")
    photo.add_argument("chat_id", type=int)
    photo.add_argument("file_id")

    burst = sub.add_parser("burst", help="N quick sends; records 429 (LV-16)")
    burst.add_argument("connection_id")
    burst.add_argument("chat_id", type=int)
    burst.add_argument("text")
    burst.add_argument("--count", type=int, default=5)

    return parser


COMMANDS = {
    "me": cmd_me,
    "listen": cmd_listen,
    "connection": cmd_connection,
    "send": cmd_send,
    "dm": cmd_dm,
    "edit": cmd_edit,
    "typing": cmd_typing,
    "delete": cmd_delete,
    "getfile": cmd_getfile,
    "sendphoto": cmd_sendphoto,
    "burst": cmd_burst,
}


def main() -> int:
    args = build_parser().parse_args()
    asyncio.run(COMMANDS[args.command](args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
