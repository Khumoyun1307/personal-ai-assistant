"""Switch the laptop's settings from Alviro to the dev bot (D40, M02 step D).

Run by `just dev-bot-switch` inside the dev container. Never imported by
`src/` (AGENTS §5). It reads and writes git-ignored settings files and prints
setting NAMES only, never a value (AGENTS §9):

  1. The main account's id (the laptop's current `OWNER_TELEGRAM_IDS`) is
     copied into `var/prod.env` first, so `just prod-env-push` can read it
     from there and nobody has to type it.
  2. The current `.env` is kept once as `var/dev-switch/env.before-dev-bot`.
  3. `.env` gets the dev bot's token, the second account as owner, the probe
     allow-list for the dev chats, and loses the production heartbeat URL if
     it had it (a laptop ping would hide a server outage).

Idempotent: a second run on switched settings changes nothing.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

TOKEN_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]{30,}$")
ID_RE = re.compile(r"^[0-9]+$")
IDS_RE = re.compile(r"^[0-9]+(,[0-9]+)*$")
PLACEHOLDER_MARKS = ("REPLACE", "CHANGE_ME", "<")


class SwitchError(Exception):
    """A precondition failed; nothing was written."""


def read_env(path: Path) -> dict[str, str]:
    """Settings of a dotenv file (last assignment wins, quotes removed)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    # utf-8-sig: PowerShell's Out-File writes a BOM, which would glue itself
    # to the first key.
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Set keys in place (first assignment kept, repeats dropped), append the rest."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    out: list[str] = []
    done: set[str] = set()
    for raw in lines:
        stripped = raw.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not stripped.startswith("#") and key in updates:
            if key not in done:
                out.append(f"{key}={updates[key]}")
                done.add(key)
            continue
        out.append(raw)
    out.extend(f"{key}={value}" for key, value in updates.items() if key not in done)
    tmp = path.with_name(path.name + ".switch-tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(out) + "\n")
        tmp.replace(path)
    finally:
        # A failed replace must not leave a second copy of the settings behind.
        tmp.unlink(missing_ok=True)


def _usable(value: str, pattern: re.Pattern[str]) -> bool:
    return (
        bool(value)
        and not any(m in value for m in PLACEHOLDER_MARKS)
        and bool(pattern.match(value))
    )


def plan_switch(
    env: dict[str, str], prod: dict[str, str], dev: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """What to write to `.env` and `var/prod.env`, plus notes (names only)."""
    dev_token = dev.get("DEV_BOT_TOKEN", "")
    dev_owner = dev.get("DEV_OWNER_TELEGRAM_ID", "")
    if not _usable(dev_token, TOKEN_RE):
        raise SwitchError("DEV_BOT_TOKEN in var/dev-bot.env is missing or not a BotFather token")
    if not _usable(dev_owner, ID_RE):
        raise SwitchError("DEV_OWNER_TELEGRAM_ID in var/dev-bot.env is missing or not a user id")
    prod_token = prod.get("BOT_TOKEN", "")
    if not _usable(prod_token, TOKEN_RE):
        raise SwitchError("BOT_TOKEN in var/prod.env is missing or not a BotFather token")
    if dev_token.split(":", 1)[0] == prod_token.split(":", 1)[0]:
        raise SwitchError("DEV_BOT_TOKEN belongs to the production bot; use the dev bot's token")

    prod_owner = prod.get("OWNER_TELEGRAM_IDS", "")
    env_owner = env.get("OWNER_TELEGRAM_IDS", "")
    switched = env.get("BOT_TOKEN") == dev_token and env_owner == dev_owner
    if prod_owner:
        main_ids = prod_owner
        if env_owner and not switched and env_owner != prod_owner:
            raise SwitchError(
                "OWNER_TELEGRAM_IDS differs between .env and var/prod.env; fix one by hand"
            )
    elif switched:
        raise SwitchError(
            "already on the dev bot but var/prod.env has no OWNER_TELEGRAM_IDS; "
            "copy it from the backup in var/dev-switch by hand"
        )
    else:
        main_ids = env_owner
    if not _usable(main_ids, IDS_RE):
        raise SwitchError("OWNER_TELEGRAM_IDS (the main account) is missing or malformed")
    if dev_owner in main_ids.split(","):
        raise SwitchError("the dev owner (second account) is among the production owner ids")

    prod_updates = {} if prod_owner else {"OWNER_TELEGRAM_IDS": main_ids}
    wanted = {
        "BOT_TOKEN": dev_token,
        "OWNER_TELEGRAM_IDS": dev_owner,
        # The control chat with the second account, and its business chats
        # with the main account (a private chat's id is the peer's user id).
        "PROBE_ALLOWED_CHAT_IDS": f"{dev_owner},{main_ids}",
    }
    heartbeat = env.get("HEARTBEAT_URL", "")
    if heartbeat and heartbeat == prod.get("HEARTBEAT_URL", ""):
        wanted["HEARTBEAT_URL"] = ""
    env_updates = {k: v for k, v in wanted.items() if env.get(k) != v}

    notes: list[str] = []
    openai_key = env.get("OPENAI_API_KEY", "")
    if openai_key and openai_key == prod.get("OPENAI_API_KEY", ""):
        notes.append("OPENAI_API_KEY in .env is the production key; a separate dev key is advised")
    return env_updates, prod_updates, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="switch_dev_bot", description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--prod", type=Path, default=Path("var/prod.env"))
    parser.add_argument("--dev", type=Path, default=Path("var/dev-bot.env"))
    parser.add_argument("--backup", type=Path, default=Path("var/dev-switch/env.before-dev-bot"))
    args = parser.parse_args(argv)

    for path in (args.env, args.prod, args.dev):
        if not path.is_file():
            print(f"switch: {path} is missing; nothing changed", file=sys.stderr)
            return 1
    try:
        env_updates, prod_updates, notes = plan_switch(
            read_env(args.env), read_env(args.prod), read_env(args.dev)
        )
    except SwitchError as exc:
        print(f"switch: {exc}; nothing changed", file=sys.stderr)
        return 1

    if prod_updates:
        write_env(args.prod, prod_updates)
        print(f"switch: var/prod.env += {', '.join(prod_updates)}")
    if env_updates:
        if not args.backup.exists():
            args.backup.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(args.env.read_bytes())
            print(f"switch: previous settings kept in {args.backup}")
        write_env(args.env, env_updates)
        print(f"switch: .env updated: {', '.join(env_updates)}")
    else:
        print("switch: .env is already on the dev bot")
    for note in notes:
        print(f"switch: note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
