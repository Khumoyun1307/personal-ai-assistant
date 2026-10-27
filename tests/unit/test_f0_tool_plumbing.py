"""Tool dependencies and the answered turn are optional F0 plumbing."""

from dataclasses import replace

from tests.unit.test_tool_registry import _stub_context


def test_tool_services_leave_unavailable_phase_services_unset() -> None:
    services = _stub_context().services
    assert services.drafter is None
    assert services.approval_cards is None
    assert services.actions_config is None
    assert services.notifications_enabled is False


def test_tool_context_carries_the_answered_turn_id() -> None:
    context = replace(_stub_context(), turn_id=31)
    assert context.turn_id == 31


def test_bootstrap_passes_phase_services_to_the_runtime_and_confirmation_applier(
    clean_env: None,
) -> None:
    from assistant.bootstrap import build_confirmation_applier, build_owner_runtime
    from assistant.config import Settings
    from tests.fakes.channel import FakeChannel
    from tests.fakes.llm import FakeLLM

    services = _stub_context().services
    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://synthetic:synthetic@localhost/test"
    )
    drafter, presenter, config = object(), object(), object()
    options = {
        "drafter": drafter,
        "approval_cards": presenter,
        "actions_config": config,
        "notifications_enabled": True,
    }
    runtime = build_owner_runtime(
        settings, services.session_factory, FakeChannel(), services.clock, llm=FakeLLM(), **options
    )
    applier = build_confirmation_applier(
        settings, services.session_factory, FakeChannel(), services.clock, **options
    )
    assert runtime._drafter is drafter
    assert runtime._approval_cards is presenter
    assert runtime._actions_config is config
    assert runtime._notifications_enabled is True
    assert applier._services.drafter is drafter
    assert applier._services.approval_cards is presenter
    assert applier._services.actions_config is config
    assert applier._services.notifications_enabled is True
