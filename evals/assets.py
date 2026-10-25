"""Versioned prompt and rubric files used by the evaluators.

Like the product's prompts (`agent/prompts/name.vN.md`, AGENTS §8), a file that
has been used in a recorded run is never edited: add `name.v(N+1).md` and
switch the constant that names it. Reports record the `name.vN` they used.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class Asset:
    #: `name.vN`, as recorded in reports.
    id: str
    text: str


def load_asset(kind: Literal["prompts", "rubrics"], asset_id: str) -> Asset:
    path = ROOT / kind / f"{asset_id}.md"
    return Asset(asset_id, path.read_text(encoding="utf-8").strip())
