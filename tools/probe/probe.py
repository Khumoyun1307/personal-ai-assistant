"""M00 S00.2 Telegram probe — throwaway, owner- or agent-run (AGENTS §9).

Never imported by `src/` (AGENTS §5). Its only job is to answer LV-01…LV-19
(TELEGRAM_FEASIBILITY §11) against real accounts and record exactly what
Telegram sends and returns.

It needs a real BOT_TOKEN, and the raw dumps it writes under `var/probe/`
contain real chat content. The owner runs it, or a coding agent under the
standing exception in AGENTS §9 (dev/probe bot only, never `--verbose`, never
opening `var/probe/`). The console and `summary` are content-free: ids, flags,
timestamps, rights, content kinds and Telegram error strings only. Fixtures for
`tests/fixtures/telegram/` come from the dumps via `sanitize.py`.

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
from collections import Counter
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


#: Set by --verbose. Off by default so the console stays content-free and the
#: probe can be driven by a coding agent without real chat content reaching it
#: (AGENTS §9). Full payloads always go to the git-ignored dump either way.
VERBOSE = False

#: Result fields safe to show on the console: ids, codes and API error strings.
#: Telegram's error strings are diagnostics, not user content, and they are the
#: whole point of LV-04, LV-08 and LV-16.
CONSOLE_SAFE_FIELDS = (
    "message_id",
    "date",
    "edit_date",
    "error_class",
    "message",
    "retry_after",
    "file_path",
    "file_size",
    "file_unique_id",
    "downloaded_to",
    # getBusinessConnection: flags only, no names (connection health checks).
    "is_enabled",
    "rights",
)


#: Keys that say *what kind* of message it is. Only the key name is shown.
CONTENT_KINDS = (
    "text",
    "photo",
    "voice",
    "video_note",
    "video",
    "document",
    "audio",
    "sticker",
    "animation",
    "contact",
    "location",
    "poll",
)

#: Message fields we print as-is: identifiers, flags and timestamps only.
STRUCTURAL_MESSAGE_FIELDS = ("message_id", "date", "edit_date", "is_from_offline")


def _first_token_if_command(text: Any) -> str | None:
    """`/start bizChat123` is a bot command, not prose; anything else is hidden."""
    if isinstance(text, str) and text.startswith("/"):
        return text.split()[0] + (" <param>" if " " in text.strip() else "")
    return None


def _deep_link_param(text: Any) -> str | None:
    """LV-10 needs the shape of the `/start` payload (prefix and id), nothing else."""
    if isinstance(text, str) and text.startswith("/start ") and len(text.split()) == 2:
        return text.split()[1]
    return None


def describe_message(message: dict[str, Any]) -> dict[str, Any]:
    """Content-free view of a message: who, where, which flags — never what."""
    out: dict[str, Any] = {key: message[key] for key in STRUCTURAL_MESSAGE_FIELDS if key in message}
    chat = message.get("chat") or {}
    # The dump is aiogram's model_dump (field names, not aliases): `from_user`.
    sender = message.get("from_user") or message.get("from") or {}
    out["chat_id"] = chat.get("id")
    out["chat_type"] = chat.get("type")
    out["from_id"] = sender.get("id")
    out["from_is_bot"] = sender.get("is_bot")
    if "sender_business_bot" in message:
        out["sender_business_bot_id"] = (message["sender_business_bot"] or {}).get("id")
    if message.get("business_connection_id"):
        out["connection_id"] = message["business_connection_id"]
    out["content"] = [key for key in CONTENT_KINDS if key in message] or ["other"]
    if "reply_to_message" in message:
        out["reply_to_message_id"] = (message["reply_to_message"] or {}).get("message_id")
    command = _first_token_if_command(message.get("text"))
    if command:
        out["command"] = command
        param = _deep_link_param(message.get("text"))
        if param:
            out["start_param"] = param
    return out


def describe_update(record: dict[str, Any]) -> dict[str, Any]:
    """One content-free line per dumped update — the LV evidence we may read.

    Ids, flags, timestamps, rights and content *kinds*. Never text, captions,
    names, usernames, phone numbers or file ids (AGENTS §9).
    """
    kind = str(record.get("update_type"))
    out: dict[str, Any] = {"type": kind, "observed_at": record.get("observed_at")}
    if record.get("note"):
        out["withheld"] = True
        return out
    payload = record.get("payload") or {}
    out["update_id"] = payload.get("update_id")
    body = payload.get(kind) or {}
    if kind in {"business_message", "edited_business_message", "message", "edited_message"}:
        out.update(describe_message(body))
    elif kind == "business_connection":
        rights = body.get("rights")
        out.update(
            {
                "connection_id": body.get("id"),
                "user_id": (body.get("user") or {}).get("id"),
                "user_chat_id": body.get("user_chat_id"),
                "date": body.get("date"),
                "is_enabled": body.get("is_enabled"),
                "rights": "absent"
                if rights is None
                else sorted(key for key, value in rights.items() if value is True),
            }
        )
    elif kind == "deleted_business_messages":
        out.update(
            {
                "connection_id": body.get("business_connection_id"),
                "chat_id": (body.get("chat") or {}).get("id"),
                "message_ids": body.get("message_ids"),
            }
        )
    elif kind == "callback_query":
        out["from_id"] = (body.get("from_user") or body.get("from") or {}).get("id")
    return out


def format_description(described: dict[str, Any]) -> str:
    head = f"[{described.pop('type')}]"
    described.pop("observed_at", None)
    return head + " " + " ".join(f"{key}={value}" for key, value in described.items())


def console_summary(value: Any) -> str:
    """A content-free one-liner: never text, captions, names or usernames."""
    if not isinstance(value, dict):
        return "ok"
    shown = {key: value[key] for key in CONSOLE_SAFE_FIELDS if value.get(key) is not None}
    if not shown:
        return "ok (payload written to var/probe/)"
    return ", ".join(f"{key}={val}" for key, val in shown.items())


def log_result(label: str, value: Any) -> None:
    """Record an API call's outcome — including the exact error (LV-08)."""
    record = {
        "kind": "api_result",
        "label": label,
        "observed_at": datetime.now(UTC).isoformat(),
        "result": value,
    }
    write_record(record)
    if VERBOSE:
        print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{label}: {console_summary(value)}")


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


def should_capture_full(chats: set[int], listed: set[int]) -> bool:
    """Fail closed: a full payload is written only when we are sure we may.

    That means all three of: an allow-list is configured, we could identify at
    least one chat id in the update, and one of them is allow-listed. An unset
    allow-list, or an update whose chat we cannot identify, yields metadata
    only — never the other way round (AGENTS §6.7).
    """
    return bool(listed) and bool(chats) and bool(chats & listed)


async def cmd_listen(args: argparse.Namespace) -> None:
    bot = make_bot()
    listed = allowed_chat_ids()
    if listed:
        print(f"allow-listed chat ids: {sorted(listed)}")
    else:
        print(
            "PROBE_ALLOWED_CHAT_IDS is not set: every payload will be withheld and\n"
            "only update type, update_id and a hash of chat.id recorded. Set it in\n"
            ".env to capture the chats you are testing.",
            file=sys.stderr,
        )
    print(f"allowed_updates: {ALLOWED_UPDATES}", flush=True)
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
                if should_capture_full(chats, listed):
                    record = {
                        "kind": "update",
                        "update_type": kind,
                        "observed_at": datetime.now(UTC).isoformat(),
                        "payload": payload,
                    }
                else:
                    record = redact_for_unlisted(payload, kind)
                write_record(record)
                # Structural and content-free, so the console can be read by
                # whoever drives the probe (including an agent) while the
                # owner acts in the Telegram client.
                print(format_description(describe_update(record)), flush=True)
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

    file_id = args.file_id
    if not file_id and not args.latest:
        print("give a file_id or --latest <kind>", file=sys.stderr)
        raise SystemExit(2)
    if args.latest:
        # Look the id up in the dump so it never has to cross the console.
        file_id = latest_file_id(args.latest)
        if file_id is None:
            print(f"no {args.latest} found in var/probe/ dumps", file=sys.stderr)
            raise SystemExit(1)

    async def run(bot: Bot) -> Any:
        file = await bot.get_file(file_id)
        if args.download and file.file_path:
            target = DUMP_DIR / "files" / Path(file.file_path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            await bot.download_file(file.file_path, destination=target)
            return {"file": file.model_dump(), "downloaded_to": str(target)}
        return file

    await with_bot(f"getFile({args.latest or 'by id'})", run)


def latest_file_id(content_kind: str) -> str | None:
    """The file_id of the most recent `content_kind` message in any dump."""
    found: str | None = None
    for path in sorted(DUMP_DIR.glob("updates-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") or {}
            body = payload.get(str(record.get("update_type"))) or {}
            media = body.get(content_kind) if isinstance(body, dict) else None
            if isinstance(media, list) and media:  # photo: sizes, largest last
                media = media[-1]
            if isinstance(media, dict) and isinstance(media.get("file_id"), str):
                found = media["file_id"]
    return found


async def cmd_sendphoto(args: argparse.Namespace) -> None:
    """LV-11: re-send a previously received photo by file_id."""
    file_id = args.file_id or latest_file_id("photo")
    if file_id is None:
        print("no photo found in var/probe/ dumps", file=sys.stderr)
        raise SystemExit(1)
    # "none" targets the control chat, which has no business connection.
    connection_id = None if args.connection_id == "none" else args.connection_id
    await with_bot(
        "sendPhoto(by file_id)",
        lambda bot: bot.send_photo(
            chat_id=args.chat_id,
            photo=file_id,
            business_connection_id=connection_id,
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


def cmd_summary(args: argparse.Namespace) -> None:
    """What is in a dump, without showing any of it.

    Prints update-type counts and the connection ids seen — identifiers and
    codes only, never text, names or usernames — so the owner (or an agent)
    can see what was captured without opening the dump.
    """
    path = Path(args.path)
    if not path.exists():
        print(f"no such dump: {path}", file=sys.stderr)
        raise SystemExit(1)

    kinds: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    connections: set[str] = set()
    withheld = 0
    timeline: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = str(record.get("observed_at", ""))[:19]
        if record.get("kind") == "api_result":
            labels[str(record.get("label"))] += 1
            result = record.get("result")
            if isinstance(result, dict) and result.get("error_class"):
                errors[f"{result['error_class']}: {result.get('message')}"] += 1
            timeline.append(f"{stamp}  call {record.get('label')}: {console_summary(result)}")
            continue
        timeline.append(f"{stamp}  {format_description(describe_update(record))}")
        kinds[str(record.get("update_type"))] += 1
        if record.get("note"):
            withheld += 1
        for found in _connection_ids(record.get("payload")):
            connections.add(found)

    print(f"dump: {path}")
    print(f"\nupdates by type ({sum(kinds.values())} total):")
    for kind, count in sorted(kinds.items()):
        print(f"  {count:4}  {kind}")
    if withheld:
        print(f"  ({withheld} had payloads withheld: chat not allow-listed)")
    print(f"\nconnection ids seen: {sorted(connections) or 'none yet'}")
    if labels:
        print("\nAPI calls:")
        for label, count in sorted(labels.items()):
            print(f"  {count:4}  {label}")
    if errors:
        print("\nerrors recorded (these are the LV deliverable):")
        for error, count in sorted(errors.items()):
            print(f"  {count:4}  {error}")
    if args.timeline:
        print("\ntimeline (structural, content-free):")
        for entry in timeline[-args.timeline :]:
            print(f"  {entry}")


def _connection_ids(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "business_connection_id" and isinstance(value, str):
                found.add(value)
            elif key == "business_connection" and isinstance(value, dict):
                if isinstance(value.get("id"), str):
                    found.add(value["id"])
            else:
                found |= _connection_ids(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _connection_ids(item)
    return found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probe", description=__doc__)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print full payloads to the console (contains real chat content)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary", help="what a dump contains, content-free")
    summary.add_argument("path", help="a var/probe/*.jsonl dump")
    summary.add_argument(
        "--timeline",
        type=int,
        default=0,
        metavar="N",
        help="also print the last N events as content-free structural lines",
    )

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
    getfile.add_argument("file_id", nargs="?")
    getfile.add_argument(
        "--latest",
        choices=["photo", "voice", "video_note", "video", "document", "audio"],
        help="use the newest file of this kind from the dump (keeps file ids off the console)",
    )
    getfile.add_argument("--download", action="store_true")

    photo = sub.add_parser("sendphoto", help="re-send a photo by file_id (LV-11)")
    photo.add_argument("connection_id")
    photo.add_argument("chat_id", type=int)
    photo.add_argument("file_id", nargs="?", help="default: newest photo in the dump")

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
    global VERBOSE
    args = build_parser().parse_args()
    VERBOSE = bool(getattr(args, "verbose", False))
    if args.command == "summary":  # local file read; no event loop needed
        cmd_summary(args)
        return 0
    asyncio.run(COMMANDS[args.command](args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
