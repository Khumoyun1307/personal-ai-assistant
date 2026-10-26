"""Stand-ins for the summary refresher (S03.2), for job tests that are not
about summaries."""

from __future__ import annotations

from dataclasses import dataclass, field

from assistant.agent.summarize import RefreshOutcome, SummaryStatus


@dataclass
class RecordingRefresher:
    """Records which conversations a job asked to refresh; calls no model."""

    refreshed: list[int] = field(default_factory=list)

    async def refresh(self, conversation_id: int) -> RefreshOutcome:
        self.refreshed.append(conversation_id)
        return RefreshOutcome(SummaryStatus.UP_TO_DATE)
