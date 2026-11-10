"""Run only restore-test commands against a structurally selected scratch database."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.engine import make_url
from tools.acceptance.p2_verify import aware


def scratch_url(value: str) -> str:
    url = make_url(value)
    # A libpq dbname query parameter can override the path database. Refuse
    # alternate selectors instead of claiming the path alone proves isolation.
    if (
        url.drivername != "postgresql+psycopg"
        or not url.database
        or {"dbname", "database"}.intersection(url.query)
    ):
        raise ValueError("unsupported database URL shape")
    return url.set(database="restore_test").render_as_string(hide_password=False)


async def purge_scratch(target: str) -> int:
    """Exercise the conservative fallback only in the isolated scratch DB."""
    from assistant.core.clock import SystemClock
    from assistant.db.engine import build_engine, build_session_factory
    from assistant.db.models import KEY_RESTORE_PENDING, SystemState
    from assistant.privacy.journal import complete_purge

    # Independently guard this mutating helper, even if called outside main.
    if scratch_url(target) != target:
        raise ValueError("scratch database required")
    engine = build_engine(target)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            marker = await session.get(SystemState, KEY_RESTORE_PENDING)
            if marker is None:
                raise ValueError("scratch restore gate required")
            restore_id = UUID(marker.value["restore_id"])
        await complete_purge(factory, SystemClock(), restore_id=restore_id)
    finally:
        await engine.dispose()
    print("scratch_privacy_purge_complete; fresh_consent_required")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("post-restore", "actions"):
        commands.add_parser(name).add_argument("timestamp", type=aware)
    commands.add_parser("release")
    commands.add_parser("restore-check")
    purge = commands.add_parser("privacy-purge")
    purge.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        # Parse/validate before replacing anything or starting a child. The URL
        # stays private in this container; no secret crosses the operator shell.
        target = scratch_url(os.environ["DATABASE_URL"])
        match args.command:
            case "privacy-purge":
                if args.execute:
                    # Internal child entry: reject an unmodified production
                    # URL rather than silently redirecting a mutating call.
                    return asyncio.run(purge_scratch(os.environ["DATABASE_URL"]))
                command = ["-m", "tools.acceptance.scratch_restore", "privacy-purge", "--execute"]
            case "post-restore":
                command = [
                    "-m",
                    "assistant",
                    "post-restore",
                    "--backup-taken-at",
                    args.timestamp.isoformat(),
                ]
            case "actions":
                command = [
                    "tools/acceptance/p2_verify.py",
                    "actions",
                    "--since",
                    args.timestamp.isoformat(),
                ]
            case "release":
                command = ["tools/acceptance/p2_verify.py", "release"]
            case "restore-check":
                command = ["-m", "tools.acceptance.restore_verify"]
            case _:
                raise ValueError("unsupported command")
        os.environ["DATABASE_URL"] = target
        os.execv(sys.executable, [sys.executable, *command])  # noqa: S606 — fixed command allowlist
    except Exception as error:
        print(f"scratch_restore_failed type={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
