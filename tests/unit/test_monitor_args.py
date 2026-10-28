"""S05.4 §3.1: `create_monitor`'s strict arguments are the monitor spec.

Constraints are Pydantic validators (no `format`/`pattern` keywords), each
broken rule a fixed `ArgumentRuleError` text the model can act on; expiry is
parsed in the owner's timezone and must be in the future, and a one-shot
monitor defaults to 30 days (TL decision, S05.4 §3.1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from assistant.agent.monitor_schemas import (
    ONE_SHOT_DEFAULT_TTL,
    CreateMonitorArgs,
    MonitorVerdicts,
    effective_scope,
    monitor_expiry,
)
from assistant.agent.tools.common import ArgumentRuleError
from assistant.agent.tools.registry import strict_schema

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

#: Keywords strict mode does not need, kept out of tool schemas (D52).
_UNUSED = {"format", "minLength", "maxLength", "minItems", "maxItems", "pattern", "default"}


def args(**overrides: Any) -> dict[str, Any]:
    condition: dict[str, Any] = {
        "kind": "keywords",
        "contact_id": None,
        "terms": ["P2KEY"],
        "description": None,
        "media_kinds": [],
    }
    condition.update(overrides.pop("condition", {}))
    scope: dict[str, Any] = {"contact_ids": [], "conversation_ids": [], "all_observed": True}
    scope.update(overrides.pop("scope", {}))
    values: dict[str, Any] = {
        "scope": scope,
        "condition": condition,
        "action": "alert",
        "lifecycle": "standing",
        "expires_at": None,
        "bypass_quiet_hours": False,
    }
    values.update(overrides)
    return values


def rules(error: ValidationError) -> list[str]:
    found = []
    for issue in error.errors(include_input=False, include_url=False):
        rule = issue.get("ctx", {}).get("error")
        if isinstance(rule, ArgumentRuleError):
            found.append(str(rule))
    return found


def keywords(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in keywords(value)}
    if isinstance(node, list):
        return {k for value in node for k in keywords(value)}
    return set()


def test_create_monitor_schema_is_strict() -> None:
    schema = strict_schema(CreateMonitorArgs)
    for node in (schema, *schema.get("$defs", {}).values()):
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])
    assert not keywords(schema) & _UNUSED
    verdicts = strict_schema(MonitorVerdicts)
    assert next(iter(verdicts["properties"])) == "language"
    assert not keywords(verdicts) & _UNUSED
    with pytest.raises(ValidationError):
        CreateMonitorArgs.model_validate({**args(), "surprise": 1})


def test_contact_replied_needs_a_contact() -> None:
    with pytest.raises(ValidationError) as missing:
        CreateMonitorArgs.model_validate(
            args(condition={"kind": "contact_replied", "terms": []}, scope={"all_observed": False})
        )
    assert any("contact_id" in rule for rule in rules(missing.value))
    parsed = CreateMonitorArgs.model_validate(
        args(
            condition={"kind": "contact_replied", "contact_id": 41, "terms": []},
            scope={"all_observed": False},
            lifecycle="one_shot",
        )
    )
    # The scope is that contact, whatever else the call named.
    scope = effective_scope(parsed)
    assert (scope.contact_ids, scope.conversation_ids, scope.all_observed) == ((41,), (), False)
    with pytest.raises(ValidationError):
        CreateMonitorArgs.model_validate(
            args(condition={"kind": "contact_replied", "contact_id": 0, "terms": []})
        )


def test_keywords_need_terms_within_bounds() -> None:
    for terms in ([], [""], ["   "], ["x" * 41], [f"t{i}" for i in range(11)], ["?!"]):
        with pytest.raises(ValidationError) as error:
            CreateMonitorArgs.model_validate(args(condition={"terms": terms}))
        assert rules(error.value), terms
    ok = CreateMonitorArgs.model_validate(
        args(condition={"terms": ["x" * 40, *(f"t{i}" for i in range(9))]})
    )
    assert len(ok.condition.terms) == 10
    # Some scope is required: none named and not every observed chat.
    with pytest.raises(ValidationError) as unscoped:
        CreateMonitorArgs.model_validate(args(scope={"all_observed": False}))
    assert rules(unscoped.value)
    # Every observed chat already covers named chats: never both.
    with pytest.raises(ValidationError):
        CreateMonitorArgs.model_validate(args(scope={"conversation_ids": [5]}))


def test_semantic_needs_a_description() -> None:
    with pytest.raises(ValidationError) as missing:
        CreateMonitorArgs.model_validate(args(condition={"kind": "semantic", "terms": []}))
    assert any("description" in rule for rule in rules(missing.value))
    for too_long in ("", " ", "d" * 301):
        with pytest.raises(ValidationError):
            CreateMonitorArgs.model_validate(
                args(condition={"kind": "semantic", "terms": [], "description": too_long})
            )
    assert CreateMonitorArgs.model_validate(
        args(condition={"kind": "semantic", "terms": [], "description": "d" * 300})
    )
    # decision_requested and urgent may narrow the topic, or not.
    for kind in ("decision_requested", "urgent"):
        assert CreateMonitorArgs.model_validate(args(condition={"kind": kind, "terms": []}))
    with pytest.raises(ValidationError):
        CreateMonitorArgs.model_validate(
            args(condition={"kind": "media_received", "terms": [], "media_kinds": []})
        )
    assert CreateMonitorArgs.model_validate(
        args(condition={"kind": "media_received", "terms": [], "media_kinds": ["photo"]})
    )


def test_expires_at_parses_in_the_owner_timezone_and_must_be_future() -> None:
    parsed = CreateMonitorArgs.model_validate(args(expires_at="2026-10-28 09:30"))
    # 09:30 in Tashkent (UTC+5) is 04:30 UTC.
    assert monitor_expiry(parsed, timezone="Asia/Tashkent", now=NOW) == datetime(
        2026, 10, 28, 4, 30, tzinfo=UTC
    )
    assert monitor_expiry(parsed, timezone="UTC", now=NOW) == datetime(
        2026, 10, 28, 9, 30, tzinfo=UTC
    )
    past = CreateMonitorArgs.model_validate(args(expires_at="2026-10-27 16:00"))
    # 16:00 in Tashkent is 11:00 UTC: already gone at noon UTC.
    with pytest.raises(ArgumentRuleError):
        monitor_expiry(past, timezone="Asia/Tashkent", now=NOW)
    for malformed in ("tomorrow", "2026-10-28T09:30", "28.10.2026 09:30"):
        with pytest.raises(ValidationError):
            CreateMonitorArgs.model_validate(args(expires_at=malformed))


def test_one_shot_defaults_to_thirty_days() -> None:
    one_shot = CreateMonitorArgs.model_validate(args(lifecycle="one_shot"))
    assert timedelta(days=30) == ONE_SHOT_DEFAULT_TTL
    assert monitor_expiry(one_shot, timezone="Asia/Tashkent", now=NOW) == NOW + timedelta(days=30)
    standing = CreateMonitorArgs.model_validate(args(lifecycle="standing"))
    assert monitor_expiry(standing, timezone="Asia/Tashkent", now=NOW) is None
