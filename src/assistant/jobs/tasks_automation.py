"""Automation job registration point, inactive until its owning units land."""

from dataclasses import dataclass

from procrastinate import App


@dataclass(frozen=True, slots=True)
class AutomationJobHandlers:
    """Automation handlers, populated by their owning units."""


def register_automation_tasks(app: App, handlers: AutomationJobHandlers | None) -> None:
    """No automation jobs exist at F0; omitted groups register nothing."""
