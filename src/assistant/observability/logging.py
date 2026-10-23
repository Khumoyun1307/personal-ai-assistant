"""structlog configuration (ARCHITECTURE §14.4, §17).

JSON in production, human-readable in development. Redaction processors run
before any renderer, so no formatter can put content back in. Records from
stdlib `logging` (every third-party library) go through the same chain via
`structlog.stdlib.ProcessorFormatter`, so no library traceback or
`str(exc)` reaches the output unredacted.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import MutableMapping
from typing import Any, Final

import structlog

from assistant.config import Settings
from assistant.observability.redaction import (
    REDACTED,
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
    "openai",
    "httpcore",
    "httpx",
    # The openai SDK's transport (3.x) is httpx2, which logs under its own name.
    "httpx2",
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
    if os.environ.get("OPENAI_LOG", "").lower() == "debug":
        problems.append("OPENAI_LOG=debug")
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
    _route_stdlib_logging(renderer, level)

    assert_production_logging_is_safe(settings)


#: Library records below this level stay unrendered, as they were before
#: stdlib logging was routed here (Python's last-resort handler showed only
#: WARNING and above); library INFO is noise and may carry ids we do not need.
STDLIB_MIN_LEVEL: Final = logging.WARNING


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever `sys.stdout` is at emit time — where structlog's
    `PrintLogger` writes — so both streams interleave and test capture works."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        return


def redact_foreign_message(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """The message of a library (stdlib) record is not ours: it is often an
    f-string with `str(exc)` or a payload in it (asyncio's "Task exception
    was never retrieved" quotes the exception; aiogram quotes the error,
    which can quote the Bot API URL). A pre-formatted message cannot be told
    apart from a constant one, so only what is safe by construction is kept:
    a %-style template, without its arguments. Anything else becomes
    `[redacted]`; the logger name, level and typed exception fields
    (`redact_exception`) remain, which is what an operator needs to look.
    """
    record = event_dict.get("_record")
    args = event_dict.pop("positional_args", None)
    event_dict.pop("stack_info", None)
    if not isinstance(record, logging.LogRecord):
        return event_dict
    event_dict["logger"] = record.name
    event_dict["event"] = str(record.msg) if args else REDACTED
    return event_dict


def _route_stdlib_logging(renderer: structlog.typing.Processor, level: int) -> None:
    """Send stdlib records (procrastinate, asyncio, aiogram, SQLAlchemy …)
    through the same redaction as our own logs (§14.4), instead of Python's
    last-resort handler, which prints `str(exc)` and full tracebacks."""
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_foreign_message,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_exception,
            drop_sensitive_keys,
            scrub_sensitive_values,
            renderer,
        ],
        pass_foreign_args=True,
    )
    handler = _StdoutHandler()
    handler.setFormatter(formatter)
    handler.setLevel(max(level, STDLIB_MIN_LEVEL))
    handler.set_name(_HANDLER_NAME)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > handler.level:
        root.setLevel(handler.level)
    # `warnings.warn` → the `py.warnings` logger → the same handler.
    logging.captureWarnings(True)


_HANDLER_NAME: Final = "assistant-redacted"


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for `name`."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
