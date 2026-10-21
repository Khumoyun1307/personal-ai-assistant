"""Deep-link parameters on `/start` (TF §12 LV-10).

"Manage Bot" in a managed chat opens the bot with `/start bizChat<id>`, where
`<id>` is the contact's chat id. The parser is deliberately strict — ASCII
digits only, no sign, within the signed 64-bit range — because the value
comes from the client and feeds a database lookup. `str.isdigit` is not used:
it accepts full-width and other Unicode digits.
"""

from __future__ import annotations

import re

_BIZ_CHAT = re.compile(r"\Abizchat([1-9][0-9]{0,18})\Z", re.IGNORECASE)
_MAX_ID = 2**63 - 1


def parse_biz_chat(argument: str) -> int | None:
    """The business chat id in a `bizChat<id>` parameter, or None."""
    match = _BIZ_CHAT.match(argument.strip())
    if match is None:
        return None
    value = int(match.group(1))
    return value if value <= _MAX_ID else None


def is_deep_link(argument: str) -> bool:
    return argument.strip().lower().startswith("bizchat")
