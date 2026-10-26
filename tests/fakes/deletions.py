"""Stands in for the owner's button deletions (`privacy.delete_data`, S03.5)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RecordingDeleter:
    runs: list[tuple[int, str, int, int, int | None]] = field(default_factory=list)

    async def run(
        self,
        account_id: int,
        scope: str,
        subject_id: int,
        card_id: int,
        turn_cutoff: int | None = None,
    ) -> str:
        self.runs.append((account_id, scope, subject_id, card_id, turn_cutoff))
        return "deleted"
