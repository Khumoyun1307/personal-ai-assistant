"""M04 job registration point; bodies are added by their owning units."""

from dataclasses import dataclass

from procrastinate import App


@dataclass(frozen=True, slots=True)
class ActionsJobHandlers:
    """M04 handlers, populated as the corresponding units land."""


def register_actions_tasks(app: App, handlers: ActionsJobHandlers | None) -> None:
    """No M04 jobs exist at F0; omitted groups always register nothing."""
