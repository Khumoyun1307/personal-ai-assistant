"""M05 inbox job registration point, inactive until its owning units land."""

from dataclasses import dataclass

from procrastinate import App


@dataclass(frozen=True, slots=True)
class InboxJobHandlers:
    """M05 inbox handlers, populated by their owning units."""


def register_inbox_tasks(app: App, handlers: InboxJobHandlers | None) -> None:
    """No M05 inbox jobs exist at F0; omitted groups register nothing."""
