"""M05 inbox jobs (F0-interfaces §6.1): registered only for the handler
groups bootstrap wires; an omitted group registers nothing."""

from dataclasses import dataclass

from procrastinate import App, RetryStrategy
from sqlalchemy.exc import DBAPIError, OperationalError

from assistant.core.jobs import NOTIFY_TASK
from assistant.inbox.notifications import NotificationDelivery
from assistant.jobs.app import QUEUE_OUTBOUND

#: Database errors, as `OWNER_REQUEST_RETRY`: `DBAPIError` also covers
#: integrity and data errors, not only transient ones, which is harmless — a
#: re-run reloads the row and decides again, at most three times. Never a
#: resend: the row is claimed (`uncertain`) before the send, so a re-run after
#: it finds nothing queued. Telegram errors are not exceptions here.
NOTIFY_DELIVER_RETRY = RetryStrategy(
    max_attempts=3, exponential_wait=2, retry_exceptions={OperationalError, DBAPIError}
)


@dataclass(frozen=True, slots=True)
class InboxJobHandlers:
    """M05 inbox handlers, populated by their owning units."""

    #: S05.2: `notify.deliver`.
    notifications: NotificationDelivery | None = None


def register_inbox_tasks(app: App, handlers: InboxJobHandlers | None) -> None:
    if handlers is None:
        return
    delivery = handlers.notifications
    if delivery is not None:

        @app.task(name=NOTIFY_TASK, queue=QUEUE_OUTBOUND, retry=NOTIFY_DELIVER_RETRY)
        async def notify_deliver(notification_id: int) -> None:
            await delivery.deliver(notification_id)
