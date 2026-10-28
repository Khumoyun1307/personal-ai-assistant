"""Scenario fixtures: the integration environment (real Postgres, real inbox,
FakeLLM, FakeClock, fake Telegram sender) reused for end-to-end journeys."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.integration.conftest import env, migrated_url
from tests.scenarios.p2_drive import Scenario, m04_environment


@pytest.fixture
async def p2(migrated_url: str) -> AsyncIterator[Scenario]:
    async with m04_environment(migrated_url) as scenario:
        yield scenario


__all__ = ["env", "migrated_url"]
