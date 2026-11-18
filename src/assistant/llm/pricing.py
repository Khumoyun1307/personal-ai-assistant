"""Model prices: the single, sourced price table (I16, C7 "Priced admission").

Every entry names where its numbers come from and when they were checked, so
a stale or invented price is visible in review. A model that is not in this
table has **no price**: admission refuses it (fail closed, D-S7) instead of
treating it as free. A conservative bound for an unpriced model may be added
only as an `OWNER_APPROVED_BOUND` entry that cites the owner's recorded
approval; there is none today.

Cached input tokens are charged at the full input price: the cached-token
discount is not verified, so the budget errs on the safe side.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

_MILLION = Decimal(1_000_000)


class PriceKind(StrEnum):
    VERIFIED = "verified"
    """Copied from the provider's official price page (`source`, `checked_on`)."""

    OWNER_APPROVED_BOUND = "owner_approved_bound"
    """A conservative upper bound the owner approved for a model without a
    verified price; `source` cites the recorded approval."""


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per 1M tokens for one configured model id."""

    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    kind: PriceKind
    #: Where the numbers come from (document/section or official page).
    source: str
    #: ISO date the numbers were last checked against `source`.
    checked_on: str
    #: Static identifier stored with every admission (`llm_admissions.price_version`).
    version: str

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """USD for this many tokens, cached input at the full input price."""
        return (
            Decimal(input_tokens) * self.input_per_mtok
            + Decimal(output_tokens) * self.output_per_mtok
        ) / _MILLION


#: The price table. Only models already priced in the repository are listed;
#: nothing is invented (I16 limits). `gpt-6.1-sol` (D-S7 comparison
#: candidate) and the ARCHITECTURE §3 alternatives `gpt-6-sol`/`gpt-6-astra`
#: are deliberately absent: they fail admission until a lead/owner adds a
#: verified price.
PRICES: tuple[ModelPrice, ...] = (
    ModelPrice(
        model="gpt-6-luna",
        input_per_mtok=Decimal("0.10"),
        output_per_mtok=Decimal("0.50"),
        kind=PriceKind.VERIFIED,
        source=(
            "ARCHITECTURE §3/§7.4 (pre-I16 metering table); official model page "
            "https://developers.openai.com/api/docs/models/gpt-6-luna as cited in "
            "stabilization reviews/r0-research.md"
        ),
        checked_on="2026-11-09",
        version="gpt-6-luna@2026-11-09",
    ),
)


def price_for(model: str) -> ModelPrice | None:
    """The price of `model`, or None when it has none (never "free").

    The API reports the resolved model, which may carry a dated snapshot
    suffix (`gpt-6-luna-2026-09-01`), so a listed name also matches as a
    prefix followed by `-` and a digit; `gpt-6-lunatic` or `gpt-6-luna-pro`
    do not match.
    """
    for price in PRICES:
        if model == price.model:
            return price
        suffix = model.removeprefix(f"{price.model}-")
        if suffix != model and suffix[:1].isdigit():
            return price
    return None
