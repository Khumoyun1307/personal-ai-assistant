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


def assert_equal(actual: object, expected: object, *, field: str) -> None:
    assert actual == expected, f"{field}: expected {expected!r}, actual {actual!r}"


def assert_card(actual: CardArtifact, expected: CardArtifact) -> None:
    assert_equal(actual.account_id, expected.account_id, field="card account_id")
    assert_equal(actual.conversation_id, expected.conversation_id, field="card conversation_id")
    assert_equal(actual.text, expected.text, field="card text")
    assert_equal(actual.version, expected.version, field="card version")
    assert_equal(actual.state, expected.state, field="card state")
    assert_equal(actual.origin, expected.origin, field="card origin")
    assert_equal(actual.reason, expected.reason, field="card reason")


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
    assert_equal(actual.version, claimed_version, field="claimed card version")
    assert_equal(
        _CLAIMS.get(actual.state),
        claimed_status,
        field=f"claimed status for state {actual.state!r}",
    )
    if claimed_text_hash is not None:
        assert actual.text is not None, (
            f"card text for content claim: expected retained text, actual {actual.text!r}"
        )
        assert_equal(
            hashlib.sha256(actual.text.encode()).hexdigest(),
            claimed_text_hash,
            field="claimed card text SHA-256",
        )
