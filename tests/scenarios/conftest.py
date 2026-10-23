"""Scenario fixtures: the integration environment (real Postgres, real inbox,
FakeLLM, FakeClock, fake Telegram sender) reused for end-to-end journeys."""

from __future__ import annotations

from tests.integration.conftest import env, migrated_url

__all__ = ["env", "migrated_url"]
