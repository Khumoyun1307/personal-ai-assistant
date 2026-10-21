"""Turn raw probe dumps into committable test fixtures. Owner- or agent-run (AGENTS §9).

AGENTS §9: coding agents never read `var/probe/`. The owner or the agent runs
this (AGENTS §9 standing exception); the agent sees only its output, which is
committed to `tests/fixtures/telegram/`.

The rule is **default-deny for strings**: a string value is kept only when its
key is on the structural allow-list below (chat types, statuses, MIME types —
enums the tests actually assert on). Everything else — message text, captions,
names, usernames, phone numbers, file ids — is replaced with synthetic content.
Identifiers are remapped consistently, so a fixture still makes sense: the same
real chat id always becomes the same synthetic chat id, within and across files.

Replacement text has the same length **in UTF-16 code units** as the original,
because Telegram message entity offsets are counted that way; a fixture whose
text changed length would carry entity offsets that no longer line up.

    docker compose -f compose.dev.yml run --rm app python tools/probe/sanitize.py \
        --input var/probe/updates-20261018.jsonl \
        --output tests/fixtures/telegram/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

#: String values kept verbatim: structural enums the tests assert on.
STRUCTURAL_KEYS = frozenset(
    {
        "type",
        "status",
        "mime_type",
        "language_code",
        "currency",
        "update_type",
        "kind",
        "note",
        "error_class",
        "label",
    }
)

#: Keys holding identifiers: remapped consistently, never kept.
ID_KEYS = frozenset(
    {
        "id",
        "message_id",
        "update_id",
        "chat_id",
        "user_chat_id",
        "sender_chat_id",
        "reply_to_message_id",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        "business_connection_id",
        "file_id",
        "file_unique_id",
        "media_group_id",
        "custom_emoji_id",
        "chat_instance",
        "callback_data",
    }
)

#: Integer ids that name a user or chat (a private chat's id is its user's id).
PEER_ID_KEYS = frozenset({"id", "chat_id", "user_chat_id", "sender_chat_id"})

#: Keys whose *integer* values are timestamps, kept so window tests stay real.
TIMESTAMP_KEYS = frozenset({"date", "edit_date", "forward_date", "expiration_date"})

FILLER = "abcdefghijklmnopqrstuvwxyz"


class Sanitizer:
    """Deterministic, stable across runs and across files in one run."""

    def __init__(self) -> None:
        self._ids: dict[tuple[str, Any], Any] = {}
        self._counters: dict[str, int] = {}

    def _next(self, bucket: str) -> int:
        self._counters[bucket] = self._counters.get(bucket, 0) + 1
        return self._counters[bucket]

    @staticmethod
    def _namespace(key: str, value: Any) -> str:
        """Keys that name the same kind of thing share one mapping.

        A user id appears as `user.id`, `from.id`, `chat.id`, `user_chat_id`
        and `chat_id`; a connection id as `business_connection.id` and
        `business_connection_id`. Mapped per key, one real id became several
        synthetic ones and fixtures could not link a message to its
        connection or its sender to the owner.
        """
        if isinstance(value, int) and key in PEER_ID_KEYS:
            return "peer"
        if isinstance(value, str) and key in {"id", "business_connection_id"}:
            return "business-connection-id"
        return key

    def map_id(self, key: str, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        namespace = self._namespace(key, value)
        cached = self._ids.get((namespace, value))
        if cached is not None:
            return cached
        index = self._next(namespace)
        if isinstance(value, int):
            # Keep the sign: negative chat ids mean groups/channels.
            base = {"update_id": 100_000, "message_id": 1_000}.get(key, 700_000_000)
            mapped: Any = -(base + index) if value < 0 else base + index
        else:
            mapped = f"synthetic-{namespace.replace('_', '-')}-{index:04d}"
        self._ids[(namespace, value)] = mapped
        return mapped

    @staticmethod
    def _utf16_len(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def fake_text(self, key: str, value: str) -> str:
        """Synthetic text of the same UTF-16 length, so entity offsets hold."""
        length = self._utf16_len(value)
        if length == 0:
            return ""
        seed = f"{key}:{self._next(f'text:{key}')}"
        start = sum(ord(char) for char in seed) % len(FILLER)
        out: list[str] = []
        while len(out) < length:
            out.append(FILLER[(start + len(out)) % len(FILLER)])
        return "".join(out[:length])

    def walk(self, node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {child_key: self.walk(value, child_key) for child_key, value in node.items()}
        if isinstance(node, list):
            return [self.walk(item, key) for item in node]
        if isinstance(node, bool) or node is None:
            return node
        if key in ID_KEYS:
            return self.map_id(key, node)
        if isinstance(node, int | float):
            return node  # timestamps, offsets, lengths, file sizes
        if isinstance(node, str):
            if key in STRUCTURAL_KEYS:
                return node
            return self.fake_text(key, node)
        return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="a var/probe/*.jsonl dump")
    parser.add_argument("--output", required=True, type=Path, help="fixture directory")
    parser.add_argument(
        "--per-file",
        action="store_true",
        help="write one fixture per update instead of a single array",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"no such dump: {args.input}", file=sys.stderr)
        return 1

    sanitizer = Sanitizer()
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(args.input.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"skipping unparsable line {line_number}", file=sys.stderr)
            continue
        records.append(sanitizer.walk(record))

    args.output.mkdir(parents=True, exist_ok=True)
    if args.per_file:
        for index, record in enumerate(records, 1):
            kind = str(record.get("update_type") or record.get("kind") or "record")
            path = args.output / f"{kind}-{index:03d}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")
    else:
        path = args.output / f"{args.input.stem}.sanitized.json"
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", "utf-8")

    print(f"sanitized {len(records)} record(s) -> {args.output}")
    print("Review the output before committing: no real text, names or file ids.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
