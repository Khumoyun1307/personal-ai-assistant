"""Deterministic artifact graders, independent of scripted model narration."""

import hashlib
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CardArtifact:
    account_id: int
    conversation_id: int
    text: str | None
    version: int
    state: str
    origin: str
    reason: str | None


def assert_card(actual: CardArtifact, expected: CardArtifact) -> None:
    assert actual.account_id == expected.account_id
    assert actual.conversation_id == expected.conversation_id
    assert actual.text == expected.text
    assert actual.version == expected.version
    assert actual.state == expected.state
    assert actual.origin == expected.origin
    assert actual.reason == expected.reason


Claim = Literal["awaiting_approval", "sending", "sent", "not_sent", "uncertain"]
_CLAIMS: dict[str, Claim] = {
    "awaiting_approval": "awaiting_approval",
    "approved": "sending",
    "executing": "sending",
    "succeeded": "sent",
    "delivery_uncertain": "uncertain",
    "rejected": "not_sent",
    "outdated": "not_sent",
    "cancelled": "not_sent",
    "blocked": "not_sent",
    "failed": "not_sent",
    "expired": "not_sent",
}


def assert_claim(
    actual: CardArtifact,
    *,
    claimed_version: int,
    claimed_status: Claim,
    claimed_text_hash: str | None = None,
) -> None:
    assert actual.version == claimed_version
    assert _CLAIMS.get(actual.state) == claimed_status
    if claimed_text_hash is not None:
        assert actual.text is not None, "purged text cannot support a content claim"
        assert hashlib.sha256(actual.text.encode()).hexdigest() == claimed_text_hash
