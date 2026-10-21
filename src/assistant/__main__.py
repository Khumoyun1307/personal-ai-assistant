"""Command-line entrypoint: `python -m assistant <command>`.

Commands: `healthcheck`, `migrate`, `inbox requeue|status`, `run` (the
long-running process, S01.5) and `post-restore` (after a backup restore, S02.5).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_IMPLEMENTED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assistant", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("healthcheck", help="validate configuration and reach the database")
    sub.add_parser("migrate", help="apply database migrations")
    run = sub.add_parser("run", help="run the assistant (poller, inbox, workers, system tasks)")
    run.add_argument(
        "--roles",
        default="poller,inbox,worker,system",
        help="comma-separated subset of poller,inbox,worker,system (default: all)",
    )

    inbox = sub.add_parser("inbox", help="durable inbox maintenance")
    inbox_sub = inbox.add_subparsers(dest="inbox_command", required=True)
    requeue = inbox_sub.add_parser("requeue", help="reset dead-lettered updates to pending")
    requeue.add_argument(
        "--id", type=int, default=None, help="one row id; default is every failed row"
    )
    inbox_sub.add_parser("status", help="show inbox counts by status")

    restore = sub.add_parser(
        "post-restore", help="make a restored database safe (ARCHITECTURE §15.6)"
    )
    restore.add_argument(
        "--backup-taken-at",
        required=True,
        help="when the restored dump was taken (ISO 8601, UTC), e.g. 2026-10-21T02:00:00Z",
    )
    return parser


async def _check_database(database_url: str) -> None:
    """Open a connection and run `SELECT 1`. Raises on failure."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # hide_parameters keeps statement parameters out of exception text (§14.4).
    engine = create_async_engine(database_url, hide_parameters=True, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def healthcheck() -> int:
    """Validate settings, then reach the database. Reports names, never values."""
    from assistant.config import Settings

    try:
        settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as error:
        print("Configuration invalid:", file=sys.stderr)
        for item in error.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in item["loc"]) or "<settings>"
            print(f"  {location.upper()}: {item['msg']}", file=sys.stderr)
        return EXIT_FAILED

    from assistant.observability.logging import UnsafeLoggingError, configure_logging

    try:
        configure_logging(settings)
    except UnsafeLoggingError as error:
        print(f"Logging configuration unsafe for prod: {error}", file=sys.stderr)
        return EXIT_FAILED

    from sqlalchemy.exc import SQLAlchemyError

    try:
        asyncio.run(_check_database(settings.database_url.get_secret_value()))
    except (SQLAlchemyError, OSError) as error:
        # Reported by exception type: the message would quote the URL (§14.4).
        print(
            f"Database unreachable via DATABASE_URL ({type(error).__qualname__})",
            file=sys.stderr,
        )
        return EXIT_FAILED

    print(f"OK: configuration valid, database reachable (APP_ENV={settings.app_env})")
    return EXIT_OK


def migrate() -> int:
    """Apply Alembic migrations up to head."""
    from alembic import command
    from alembic.config import Config

    from assistant.config import Settings

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as error:
        print("Configuration invalid:", file=sys.stderr)
        for item in error.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in item["loc"]) or "<settings>"
            print(f"  {location.upper()}: {item['msg']}", file=sys.stderr)
        return EXIT_FAILED

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    # Passed via the environment so the URL never lands in alembic.ini.
    os.environ["ALEMBIC_DATABASE_URL"] = settings.database_url.get_secret_value()
    command.upgrade(config, "head")
    print("OK: migrations applied")
    return EXIT_OK


def _inbox(args: argparse.Namespace) -> int:
    """`inbox requeue` / `inbox status` (ARCHITECTURE §6.2).

    The dead-letter runbook is: fix the bug, deploy, requeue.
    """
    from assistant.config import Settings
    from assistant.db.engine import build_engine, build_session_factory
    from assistant.inbox.processor import inbox_backlog, requeue_failed

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as error:
        print("Configuration invalid:", file=sys.stderr)
        for item in error.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in item["loc"]) or "<settings>"
            print(f"  {location.upper()}: {item['msg']}", file=sys.stderr)
        return EXIT_FAILED

    async def run() -> int:
        engine = build_engine(settings.database_url.get_secret_value())
        factory = build_session_factory(engine)
        try:
            if args.inbox_command == "status":
                for status, count in sorted((await inbox_backlog(factory)).items()):
                    print(f"{status:>10}: {count}")
                return EXIT_OK
            reset = await requeue_failed(factory, row_id=args.id)
            print(f"OK: {reset} update(s) reset to pending")
            return EXIT_OK
        finally:
            await engine.dispose()

    return asyncio.run(run())


def run(args: argparse.Namespace) -> int:
    """`run` — the long-running process (ARCHITECTURE §4.1, §4.3).

    Exits non-zero when any role fails or turns unhealthy, so Compose restarts
    the container; exits 0 after a clean SIGTERM shutdown.
    """
    from assistant.bootstrap import InstanceAlreadyRunningError, running_process
    from assistant.config import ConfigError, Settings
    from assistant.observability.logging import UnsafeLoggingError, configure_logging
    from assistant.privacy.post_restore import RestorePendingError

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as error:
        print("Configuration invalid:", file=sys.stderr)
        for item in error.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in item["loc"]) or "<settings>"
            print(f"  {location.upper()}: {item['msg']}", file=sys.stderr)
        return EXIT_FAILED
    try:
        configure_logging(settings)
    except UnsafeLoggingError as error:
        print(f"Logging configuration unsafe for prod: {error}", file=sys.stderr)
        return EXIT_FAILED

    roles = frozenset(part.strip() for part in args.roles.split(",") if part.strip())

    async def main() -> int:
        async with running_process(settings, roles) as supervisor:
            return await supervisor.run()

    try:
        return asyncio.run(main())
    except (ConfigError, InstanceAlreadyRunningError, RestorePendingError) as error:
        # Each message names a setting or a condition, never a value.
        print(f"Cannot start: {error}", file=sys.stderr)
        return EXIT_FAILED


def post_restore_command(args: argparse.Namespace) -> int:
    """`post-restore --backup-taken-at <ts>` (ARCHITECTURE §15.6; runbooks/restore.md)."""
    from datetime import UTC, datetime

    from assistant.config import Settings
    from assistant.core.clock import SystemClock
    from assistant.db.engine import build_engine, build_session_factory
    from assistant.privacy.post_restore import post_restore

    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as error:
        print("Configuration invalid:", file=sys.stderr)
        for item in error.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in item["loc"]) or "<settings>"
            print(f"  {location.upper()}: {item['msg']}", file=sys.stderr)
        return EXIT_FAILED
    try:
        taken_at = datetime.fromisoformat(args.backup_taken_at.replace("Z", "+00:00"))
    except ValueError:
        print("--backup-taken-at must be an ISO 8601 timestamp", file=sys.stderr)
        return EXIT_FAILED
    if taken_at.tzinfo is None:
        taken_at = taken_at.replace(tzinfo=UTC)

    async def run_it() -> int:
        engine = build_engine(settings.database_url.get_secret_value())
        try:
            report = await post_restore(
                build_session_factory(engine), SystemClock(), backup_taken_at=taken_at
            )
        finally:
            await engine.dispose()
        print(
            f"OK: post-restore done — {report.accounts_paused} account(s) paused, "
            f"{report.jobs_deleted} queued job(s) dropped, {report.gaps_recorded} coverage "
            f"gap(s) recorded, retention swept {report.sweep.total} item(s). "
            "restore_pending cleared; the owner is notified when the app starts."
        )
        return EXIT_OK

    return asyncio.run(run_it())


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "healthcheck":
        return healthcheck()
    if args.command == "migrate":
        return migrate()
    if args.command == "inbox":
        return _inbox(args)
    if args.command == "run":
        return run(args)
    if args.command == "post-restore":
        return post_restore_command(args)
    print(
        f"'{args.command}' is not implemented yet; see docs/IMPLEMENTATION_PLAN.md",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
