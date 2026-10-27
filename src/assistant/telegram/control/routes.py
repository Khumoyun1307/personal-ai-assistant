"""Phase-owned control handlers, composed only in bootstrap.

The dispatcher consults these after its existing built-in handlers; empty
registries preserve the current control-chat behaviour.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, PendingInput
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.telegram.control.handlers import CardPress, HandlerResult, IncomingMessage
from assistant.telegram.gateway import OutboundGateway

CallbackHandler = Callable[
    [AsyncSession, OutboundGateway, Clock, JobScheduler | None, Account, CardPress],
    Awaitable[HandlerResult],
]
PendingInputHandler = Callable[
    [
        AsyncSession,
        OutboundGateway,
        Clock,
        JobScheduler | None,
        Account,
        IncomingMessage,
        PendingInput,
    ],
    Awaitable[HandlerResult],
]
CommandHandler = Callable[
    [AsyncSession, OutboundGateway, Clock, JobScheduler | None, Account, IncomingMessage, str],
    Awaitable[HandlerResult],
]


@dataclass(frozen=True, slots=True)
class ControlRoutes:
    callbacks: Mapping[str, CallbackHandler] = field(default_factory=dict)
    pending_inputs: Mapping[str, PendingInputHandler] = field(default_factory=dict)
    commands: Mapping[str, CommandHandler] = field(default_factory=dict)
