"""structlog configuration (ARCHITECTURE §14.4, §17).

JSON in production, human-readable in development. Redaction processors run
before any renderer, so no formatter can put content back in.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import structlog

from assistant.config import Settings
from assistant.observability.redaction import (
    drop_sensitive_keys,
    redact_exception,
    scrub_sensitive_values,
)

#: Libraries that log request URLs, SQL parameters or raw payloads at DEBUG.
#: A Bot API URL embeds the token, so DEBUG here would defeat redaction.
CHATTY_LOGGERS: Final[tuple[str, ...]] = (
    "aiohttp",
    "aiohttp.client",
    "aiogram",
    "aiogram.event",
    "anthropic",
    "httpcore",
    "httpx",
    "sqlalchemy",
    "sqlalchemy.engine",
)


class UnsafeLoggingError(RuntimeError):
    """Production logging is configured in a way that could leak content."""


def assert_production_logging_is_safe(settings: Settings) -> None:
    """Refuse to start in prod when a third-party logger would dump payloads."""
    if not settings.is_production:
        return

    problems: list[str] = []
    if os.environ.get("ANTHROPIC_LOG", "").lower() == "debug":
        problems.append("ANTHROPIC_LOG=debug")
    for name in CHATTY_LOGGERS:
        logger = logging.getLogger(name)
        if logger.isEnabledFor(logging.DEBUG):
            problems.append(f"logger '{name}' at DEBUG")
    if settings.log_level == "DEBUG":
        problems.append("LOG_LEVEL=DEBUG")
    if problems:
        raise UnsafeLoggingError("; ".join(problems))


def configure_logging(settings: Settings) -> None:
    """Install the processor chain. Idempotent; safe to call again in tests."""
    level = logging.getLevelNamesMapping()[settings.log_level]

    # Quiet the libraries that would otherwise log URLs and SQL parameters.
    for name in CHATTY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.INFO))

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Redaction runs last before rendering, so nothing added above escapes.
            redact_exception,
            drop_sensitive_keys,
            scrub_sensitive_values,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    assert_production_logging_is_safe(settings)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for `name`."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
