"""Command-line entrypoint: `python -m assistant <command>`.

S00.1 implements `healthcheck`. `run` and `migrate` arrive with the milestones
that build them (S01.x); they are declared here so the task runner's recipes
fail with an explanation rather than an argparse error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_IMPLEMENTED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assistant", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("healthcheck", help="validate configuration and reach the database")
    sub.add_parser("migrate", help="apply database migrations (from S01.1)")
    sub.add_parser("run", help="run the assistant (from S01.2)")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "healthcheck":
        return healthcheck()
    print(
        f"'{args.command}' is not implemented yet; see docs/IMPLEMENTATION_PLAN.md",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
