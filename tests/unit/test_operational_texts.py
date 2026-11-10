"""Four complete operational catalogues with identical interpolation contracts."""

from __future__ import annotations

import ast
from pathlib import Path
from string import Formatter

import pytest

from assistant.accounts.connections import Change, Tier
from assistant.core.control_texts import CATALOGUE, deletion_text, text
from assistant.inbox.connection_alerts import compose, consent_buttons
from assistant.inbox.notification_types import NotificationKind
from assistant.inbox.notify import ALERT_TEXT, AlertKind
from assistant.inbox.policy_notice import POLICY_CHANGES
from assistant.privacy import deletion
from assistant.privacy.requests import OUTCOME_FAILED, OUTCOME_UNCLEAR, failure_text
from assistant.telegram.control.handlers import HELP_TEXT, MANAGED_CONSENT_TEXT
from assistant.telegram.control.onboarding_texts import (
    CONSENT_BLOCKED,
    CONSENT_PROMPT,
    consent_prompt,
)
from assistant.telegram.render import render_control_message

LOCALES = ("uz-Latn", "uz-Cyrl", "ru", "en")


@pytest.mark.parametrize("locale", LOCALES)
def test_mute_kinds_and_restore_refusal_share_the_operational_catalogue(locale: str) -> None:
    for kind in NotificationKind:
        assert text(f"mute_kind_{kind.value}", locale)
    assert CONSENT_BLOCKED[locale] == text("consent_blocked", locale)
    assert text("not_useful", locale) != text("feedback_muted", locale)
    assert "30" in text("feedback_muted", locale)
    assert "/settings" in text("feedback_muted", locale)
    assert "30" in text("mutes_body", locale)
    assert "/digest" in text("mutes_body", locale)
    assert "/start" in text("restore_consent_pending", locale, scopes="ai_control, ai_managed")
    assert "/privacy managed" in text(
        "restore_consent_pending", locale, scopes="ai_control, ai_managed"
    )


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name is not None}


@pytest.mark.parametrize("key", sorted(CATALOGUE))
def test_every_operational_key_has_all_four_locales_and_matching_fields(key: str) -> None:
    entries = CATALOGUE[key]
    assert set(entries) == set(LOCALES)
    for template in entries.values():
        assert template.strip()
        assert fields(template) == fields(entries["en"])


def test_no_literal_operational_lookup_is_missing_from_catalogue() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "assistant"
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "operational_text"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                assert node.args[0].value in CATALOGUE, source


def test_english_fallback_for_unknown_language_and_missing_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert text("paused", "unknown") == text("paused", "en")
    monkeypatch.delitem(CATALOGUE["paused"], "ru")
    assert text("paused", "ru") == text("paused", "en")


def test_english_consent_and_help_meaning_stays_unchanged() -> None:
    assert text("help", "en") == HELP_TEXT
    assert text("managed_prompt", "en", processors="SyntheticProcessor") == (
        MANAGED_CONSENT_TEXT.format(processors="SyntheticProcessor")
    )
    for kind, template in ALERT_TEXT.items():
        assert CATALOGUE[f"alert_{kind.value}"]["en"] == template
    for version, template in POLICY_CHANGES.items():
        assert CATALOGUE[f"policy_{version}"]["en"] == template


def test_english_off_button_preserves_capitalization_without_changing_status() -> None:
    assert text("off_button", "en") == "Off"
    assert text("off", "en") == "off"


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("key", sorted(CATALOGUE))
def test_every_template_renders_as_control_html(key: str, locale: str) -> None:
    values = dict.fromkeys(fields(CATALOGUE[key]["en"]), "SYNTHETIC &lt;value&gt;")
    rendered = render_control_message(text(key, locale, **values))
    assert rendered
    assert all(len(chunk) <= 4096 for chunk in rendered)
    assert "{processors}" not in "".join(rendered)


@pytest.mark.parametrize("locale", LOCALES)
def test_long_values_are_chunked_without_losing_the_end(locale: str) -> None:
    long_value = "SYNTHETIC " * 1000 + "END-SYNTHETIC"
    chunks = render_control_message(text("managed_prompt", locale, processors=long_value))
    assert len(chunks) > 1
    assert "END-SYNTHETIC" in "".join(chunks)


@pytest.mark.parametrize(
    ("locale", "saved", "not_read", "training", "revoke"),
    [
        ("en", "messages saved", "doesn't read", "isn't used to train", "any time"),
        (
            "uz-Latn",
            "saqlangan xabarlarni",
            "ularni oʻqimaydi",
            "oʻqitish uchun ishlatilmaydi",
            "istalgan payt",
        ),
        (
            "uz-Cyrl",
            "сақланган хабарларни",
            "уларни ўқимайди",
            "ўқитиш учун ишлатилмайди",
            "исталган пайт",
        ),
        ("ru", "сохранённые сообщения", "их не читает", "обучения их моделей", "любой момент"),
    ],
)
def test_consent_retains_scope_processors_storage_training_and_opt_out(
    locale: str, saved: str, not_read: str, training: str, revoke: str
) -> None:
    processors = "SyntheticProcessorA, SyntheticProcessorB"
    managed = text("managed_prompt", locale, processors=processors)
    assert saved in managed
    assert not_read in managed
    assert training in managed
    assert revoke in managed
    assert "90" in managed
    assert managed.count(processors) == 2
    assert "/privacy" in managed
    assert processors in consent_prompt(locale, ["SyntheticProcessorA", "SyntheticProcessorB"])
    assert set(CONSENT_PROMPT) == set(LOCALES)
    assert "/privacy revoke" in text("managed_granted", locale, processors=processors)
    assert "/start" in text("revoked", locale)


@pytest.mark.parametrize("locale", LOCALES)
def test_connection_controls_and_failure_explanations_render(locale: str) -> None:
    for change in (Change.CONNECTED, Change.RECONNECTED, Change.RIGHTS_CHANGED, Change.DISABLED):
        for tier in Tier:
            assert render_control_message(compose(change, tier, ["can_edit_name"], locale))
    buttons = consent_buttons("synthetic-token", locale)
    assert [button.label for button in buttons] == [
        text("turn_on", locale),
        text("not_now", locale),
    ]
    for scope in (
        deletion.SCOPE_CONVERSATION,
        deletion.SCOPE_CONTACT,
        deletion.SCOPE_EVERYTHING,
        deletion.SCOPE_MANAGED,
    ):
        for offered in (True, False):
            failed = failure_text(scope, OUTCOME_FAILED, card_offered=offered, locale=locale)
            unclear = failure_text(scope, OUTCOME_UNCLEAR, card_offered=offered, locale=locale)
            assert failed != unclear
            assert render_control_message(failed)
            assert render_control_message(unclear)
    for key in ("managed_delete_body", "delete_all_body"):
        prompt = deletion_text(key, locale)
        assert text("backup_note", locale) in prompt
        assert text("arriving_note", locale) in prompt
    receipt = deletion.receipt_text({"messages": 17, "tasks": 23}, locale)
    assert "17" in receipt
    assert "23" in receipt
    assert text("backup_note", locale) in receipt
    assert {f"alert_{kind.value}" for kind in AlertKind} <= CATALOGUE.keys()
