"""Reuse the migrated PostgreSQL fixture; never start a poller or real client."""

import json
from collections.abc import AsyncIterator
from itertools import count

import pytest

from assistant.actions.executor import ActionExecutor, ExecuteOutcome
from tests.integration import business_env
from tests.integration.conftest import migrated_url
from tests.journeys import harness
from tests.journeys.harness import JOURNEY_FLAGS, Journey
from tests.scenarios.m05_drive import FLAGS as M05_FLAGS
from tests.scenarios.m05_drive import M05Scenario, m05_environment
from tests.scenarios.p2_drive import Scenario, m04_environment

__all__ = ["migrated_url"]


@pytest.fixture(autouse=True)
def isolated_journey_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload/tool IDs must not depend on earlier journeys (including xfails).

    The environment already constructs fresh transport, clock, lookup, model,
    runtime and job adapters and truncates synthetic domain/jobs rows per test.
    These two payload-builder counters are the remaining mutable module state.
    Restore the original objects afterwards for tests outside this directory.
    """
    monkeypatch.setattr(business_env, "_ids", iter(range(1_000_000, 1_999_999)))
    monkeypatch.setattr(harness, "_CALL_IDS", count(1))
    execute = ActionExecutor.execute

    async def observed_execute(executor: ActionExecutor, action_id: int) -> ExecuteOutcome:
        # Task-local identity follows the real executor all the way to dispatch,
        # including concurrent executions. It does not change its behavior.
        token = harness.EXECUTING_ACTION.set(action_id)
        try:
            return await execute(executor, action_id)
        finally:
            harness.EXECUTING_ACTION.reset(token)

    monkeypatch.setattr(ActionExecutor, "execute", observed_execute)


@pytest.fixture
async def journey(migrated_url: str, request: pytest.FixtureRequest) -> AsyncIterator[Journey]:
    async with m04_environment(migrated_url, flags=JOURNEY_FLAGS) as scenario:
        driver = Journey(scenario, case_id=request.node.nodeid)
        scenario.env.sender.on_business_call = driver.observe_business_call
        await driver.directory("k", "Kamola Ziyo")
        yield driver
        request.node.user_properties.append(("journey", json.dumps(await driver.manifest())))
        try:
            await driver.assert_approved_attempts()
        except AssertionError as error:
            pytest.fail(str(error))


@pytest.fixture
async def background(
    migrated_url: str, request: pytest.FixtureRequest
) -> AsyncIterator[tuple[M05Scenario, Journey]]:
    async with m05_environment(migrated_url, flags=M05_FLAGS | JOURNEY_FLAGS) as scenario:
        driver = Scenario(
            scenario.env,
            scenario.fake,
            scenario.app,
            scenario.handlers,
            scenario.wiring,
            scenario.account_id,
            scenario.conversation_id,
        )
        journey = Journey(driver, case_id=request.node.nodeid)
        scenario.env.sender.on_business_call = journey.observe_business_call
        await journey.directory("k", "Kamola Ziyo")
        yield scenario, journey
        request.node.user_properties.append(("journey", json.dumps(await journey.manifest())))
        try:
            await journey.assert_approved_attempts()
        except AssertionError as error:
            pytest.fail(str(error))
