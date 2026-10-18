"""Command-line entrypoint: `python -m assistant <command>`.

S00.1 implements `healthcheck`. `run` and `migrate` arrive with the milestones
that build them (S01.x); they are declared here so the task runner's recipes
fail with an explanation rather than an argparse error.
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
    sub.add_parser("run", help="run the assistant (from S01.5)")

    inbox = sub.add_parser("inbox", help="durable inbox maintenance")
    inbox_sub = inbox.add_subparsers(dest="inbox_command", required=True)
    requeue = inbox_sub.add_parser("requeue", help="reset dead-lettered updates to pending")
    requeue.add_argument(
        "--id", type=int, default=None, help="one row id; default is every failed row"
    )
    inbox_sub.add_parser("status", help="show inbox counts by status")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "healthcheck":
        return healthcheck()
    if args.command == "migrate":
        return migrate()
    if args.command == "inbox":
        return _inbox(args)
    print(
        f"'{args.command}' is not implemented yet; see docs/IMPLEMENTATION_PLAN.md",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
