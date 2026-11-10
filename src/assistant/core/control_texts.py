"""Fixed operational wording, localized before interpolating escaped values."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import cast

from assistant.core.locales import supported_locale

CATALOGUE = cast(
    dict[str, dict[str, str]],
    json.loads(files("assistant.core").joinpath("control_texts.json").read_text(encoding="utf-8")),
)


def text(key: str, tag: str | None, **values: object) -> str:
    """Missing translations use English; unknown keys are programming errors."""
    entries = CATALOGUE[key]
    return entries.get(supported_locale(tag), entries["en"]).format(**values)


def buttons(rows: list[tuple[str, str]], tag: str | None) -> list[tuple[str, str]]:
    """Preserve opaque callback operations while translating label keys."""
    return [(text(key, tag), op) for key, op in rows]


def deletion_text(key: str, tag: str | None, **values: object) -> str:
    return text(
        key,
        tag,
        undo=text("cannot_undo", tag),
        arriving=text("arriving_note", tag),
        backup=text("backup_note", tag),
        **values,
    )
