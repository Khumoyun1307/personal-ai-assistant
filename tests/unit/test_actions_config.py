"""Actions receive the resolved draft route without consulting configuration."""

from datetime import timedelta

from assistant.actions.config import ActionsConfig


def test_actions_config_carries_the_resolved_draft_route_model() -> None:
    config = ActionsConfig(
        approval_max_age=timedelta(hours=12),
        draft_debounce=timedelta(seconds=20),
        mark_uncertain_after=timedelta(seconds=120),
        connection_stale_alert_after=timedelta(minutes=30),
        sending_enabled=False,
        process_run_id="synthetic-process-run",
        assist_draft_trigger="off",
        draft_route_model="synthetic-resolved-route-model",
    )
    assert config.draft_route_model == "synthetic-resolved-route-model"
