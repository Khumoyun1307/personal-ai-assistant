"""The single proactive draft trigger switch (P2-O6)."""

from collections.abc import Set
from typing import Literal


def assist_trigger(flags: Set[str]) -> Literal["off", "ingestion", "triage"]:
    if "assist.drafts" not in flags:
        return "off"
    if {"triage.run", "triage.assist_trigger"} <= flags:
        return "triage"
    return "ingestion"
