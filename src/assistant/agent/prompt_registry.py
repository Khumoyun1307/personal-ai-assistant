"""Versioned prompts (ARCHITECTURE §7.6, AGENTS §8).

Prompts are files named `name.vN.md` in `agent/prompts/`. `ACTIVE` says which
version each run type uses, and that version is stored on every `agent_runs`
row. A version that has been used in a run is never edited: add the next
version and switch it here.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

#: Run type → active prompt version.
ACTIVE: dict[str, str] = {
    # v2 (S03.1): chat tools, citing sources, coverage honesty, query
    # variants, awaiting_confirmation. v1 stays: retained runs reference it.
    "owner_request": "v2",
}


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def label(self) -> str:
        """What `agent_runs.prompt_version` records, e.g. `owner_request.v1`."""
        return f"{self.name}.{self.version}"


@cache
def load(name: str, version: str) -> Prompt:
    path = PROMPT_DIR / f"{name}.{version}.md"
    return Prompt(name=name, version=version, text=path.read_text(encoding="utf-8").strip())


def active(name: str) -> Prompt:
    return load(name, ACTIVE[name])
