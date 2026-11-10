"""Frozen localized matrix inputs; tests never read coordination docs at runtime."""

import json
from pathlib import Path

from tests.journeys.models import Locale

MATRIX: list[dict[str, str]] = json.loads(Path(__file__).with_suffix(".json").read_text("utf-8"))


def owner_input(case_id: str, locale: Locale) -> tuple[str, ...]:
    row = next(row for row in MATRIX if row["id"] == case_id)
    # J09 explicitly shares J08's localized owner input.
    if row[locale] == "Same J08":
        return owner_input("J08", locale)
    return tuple(text.strip() for text in row[locale].split(" → "))
