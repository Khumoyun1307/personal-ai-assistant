"""Scenario fixtures: the integration environment (real Postgres, real inbox,
FakeLLM, FakeClock, fake Telegram sender) reused for end-to-end journeys."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.integration.conftest import env, migrated_url
from tests.scenarios.m05_drive import M05Scenario, m05_environment


@pytest.fixture
async def m05(migrated_url: str) -> AsyncIterator[M05Scenario]:
    async with m05_environment(migrated_url) as scenario:
        yield scenario


__all__ = ["env", "migrated_url"]
