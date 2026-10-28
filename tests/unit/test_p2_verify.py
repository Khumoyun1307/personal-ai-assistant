"""The audit view never serializes arbitrary values from allow-listed keys."""

from tools.acceptance.p2_verify import details


def test_audit_details_accept_only_structural_values() -> None:
    result = details(
        {
            "from": {"private": "canary"},
            "to": "succeeded",
            "reason": "canary",
            "version": "canary",
            "retry_after": ["canary"],
            "identity_pending": "canary",
            "attempt_id": "canary",
            "text": "canary",
        }
    )
    assert result == "to=succeeded"
