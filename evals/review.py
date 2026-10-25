"""The owner's review ledger (IMPLEMENTATION_PLAN §10.1).

Gate results count only what the owner has confirmed. The datasets are frozen
and carry no review state, so the owner's decisions live in a small JSON file
(default `evals/reviews/<dataset>.json`, or `--reviews PATH`):

    {
      "confirmed_items": ["c01", "c02", "r01"],
      "findings": {"c05#1a2b3c4d": "confirmed", "c07#99aabbcc": "dismissed"},
      "coverage": {"c05.q2": "covered", "c05.c1": "missed"}
    }

- `confirmed_items`: conversation ids (E1) and query ids (retrieval) whose
  text and labels the owner has reviewed. A label id such as `c05.q1` counts
  as confirmed when its conversation `c05` is.
- `findings`: verdicts on the judge's fabrication flags. A flag with no
  verdict is *pending owner confirmation* and keeps the gate open.
- `coverage`: the owner's verdict on whether a summary surfaced an open loop
  that has no `values` to check by string matching (42 of the 103 open loops
  in E1 v1). Without a verdict such an item leaves the recall gate open.
- `fabrications`: conversations where the owner found a fabrication the judge
  did not flag. The judge can only pre-screen, and a conversation it did not
  flag counts as clean unless the owner lists it here.

With no file the ledger is empty: nothing is confirmed, so no gate can pass.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.errors import LedgerError

Verdict = Literal["confirmed", "dismissed"]
CoverageVerdict = Literal["covered", "missed"]
FindingStatus = Literal["pending_owner", "confirmed", "dismissed"]


class _LedgerFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed_items: list[str] = Field(default_factory=list)
    findings: dict[str, Verdict] = Field(default_factory=dict)
    coverage: dict[str, CoverageVerdict] = Field(default_factory=dict)
    fabrications: list[str] = Field(default_factory=list)


def finding_id(item_id: str, statement: str) -> str:
    """A stable id for a judge finding: the conversation plus a digest of the
    flagged statement, so the owner's verdict survives a re-run that flags the
    same statement again."""
    digest = hashlib.sha256(statement.strip().casefold().encode("utf-8")).hexdigest()
    return f"{item_id}#{digest[:8]}"


@dataclass(frozen=True, slots=True)
class ReviewLedger:
    confirmed_items: frozenset[str]
    findings: Mapping[str, Verdict]
    coverage: Mapping[str, CoverageVerdict]
    #: Where it was loaded from, for the report; None for an empty ledger.
    source: str | None = None
    #: Conversations where the owner found a fabrication the judge did not flag.
    owner_fabrications: frozenset[str] = frozenset()

    @classmethod
    def empty(cls) -> ReviewLedger:
        return cls(frozenset(), {}, {})

    @classmethod
    def load(cls, path: Path) -> ReviewLedger:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise LedgerError(f"the review ledger {path.name} cannot be read") from None
        try:
            parsed = _LedgerFile.model_validate_json(text)
        except ValidationError as error:
            # Field names only: an error can echo values.
            where = ", ".join(sorted({".".join(map(str, e["loc"])) for e in error.errors()}))
            raise LedgerError(
                f"the review ledger {path.name} is malformed ({where or 'not a JSON object'}); "
                "see evals/review.py for the format"
            ) from None
        return cls(
            frozenset(parsed.confirmed_items),
            dict(parsed.findings),
            dict(parsed.coverage),
            path.name,
            frozenset(parsed.fabrications),
        )

    def is_confirmed(self, item_id: str) -> bool:
        """True for a confirmed id or any label inside a confirmed conversation."""
        return item_id in self.confirmed_items or item_id.split(".", 1)[0] in self.confirmed_items

    def finding_status(self, finding: str) -> FindingStatus:
        return self.findings.get(finding, "pending_owner")

    def coverage_verdict(self, item_id: str) -> bool | None:
        """The owner's verdict on an uncheckable open loop: True surfaced,
        False missed, None no verdict yet."""
        verdict = self.coverage.get(item_id)
        return None if verdict is None else verdict == "covered"
