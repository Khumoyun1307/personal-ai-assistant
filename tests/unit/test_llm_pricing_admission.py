"""I16 pricing coverage, conservative bounds and failure classification."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from assistant import bootstrap
from assistant.config import Settings
from assistant.llm.admission import CallOutcome, input_bound
from assistant.llm.admitted import AdmittedLLMClient, classify_failure
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    Provenance,
    ProviderItems,
    TextItem,
    ToolResultItem,
    ToolSpec,
)
from assistant.llm.pricing import PRICES, PriceKind, price_for
from tests.fakes.clock import FakeClock


def request(**overrides: Any) -> LLMRequest:
    values: dict[str, Any] = {
        "account_id": 1,
        "purpose": "owner_request",
        "model": "gpt-6-luna",
        "instructions": "synthetic",
        "items": (TextItem("user", "synthetic", Provenance.OWNER_CONTROL),),
        "max_output_tokens": 100,
        "timeout_seconds": 10,
    }
    values.update(overrides)
    return LLMRequest(**values)


def test_every_price_names_its_source_date_and_version() -> None:
    for price in PRICES:
        assert price.source
        assert price.checked_on
        assert price.version
        assert price.kind is PriceKind.VERIFIED
        assert price.input_per_mtok > 0
        assert price.output_per_mtok > 0


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "unknown"])
def test_models_without_a_recorded_price_have_none(model: str) -> None:
    assert price_for(model) is None


@pytest.mark.parametrize(
    ("model", "priced"),
    [
        ("gpt-6-luna", True),
        ("gpt-6-luna-2026-09-01", True),
        ("gpt-6-luna-pro", False),
        ("gpt-6-lunatic", False),
    ],
)
def test_only_dated_snapshots_share_a_price(model: str, priced: bool) -> None:
    assert (price_for(model) is not None) is priced


def test_luna_cost_is_exact_decimal() -> None:
    price = price_for("gpt-6-luna")
    assert price is not None
    assert price.cost(1_000_000, 1_000_000) == Decimal("0.60")


def test_input_bound_covers_every_serialized_byte() -> None:
    text = "Ертага soat 10 da 🙂"  # multi-byte UTF-8: bytes > characters
    base = input_bound(request(items=()))
    with_text = input_bound(request(items=(TextItem("user", text, Provenance.MANAGED),)))
    assert with_text - base >= len(text.encode("utf-8"))

    tool = ToolSpec("get_status", "synthetic", {"type": "object", "properties": {}})
    assert input_bound(request(tools=(tool,))) > input_bound(request())

    items = (
        ToolResultItem("c1", "synthetic result", Provenance.TOOL_RESULT),
        ProviderItems("openai", ({"type": "reasoning", "encrypted_content": "QUJD" * 50},)),
    )
    assert input_bound(request(items=items)) > 200


def test_structured_output_schema_counts_toward_the_bound() -> None:
    class Out(BaseModel):
        summary: str
        needs_reply: bool

    assert input_bound(request(), Out) > input_bound(request())


@pytest.mark.parametrize(
    ("error", "outcome", "code"),
    [
        (LLMError(LLMErrorKind.RATE_LIMITED), CallOutcome.DEFINITE_FAILURE, "rate_limited"),
        (LLMError(LLMErrorKind.AUTH), CallOutcome.DEFINITE_FAILURE, "auth"),
        (LLMError(LLMErrorKind.BAD_REQUEST), CallOutcome.DEFINITE_FAILURE, "bad_request"),
        (
            LLMError(LLMErrorKind.UNAVAILABLE, exc_type="InternalServerError"),
            CallOutcome.DEFINITE_FAILURE,
            "provider_error",
        ),
        (
            LLMError(LLMErrorKind.UNAVAILABLE, exc_type="APITimeoutError"),
            CallOutcome.BILLING_UNCERTAIN,
            "timeout",
        ),
        (
            LLMError(LLMErrorKind.UNAVAILABLE, exc_type="APIConnectionError"),
            CallOutcome.BILLING_UNCERTAIN,
            "connection",
        ),
        (LLMError(LLMErrorKind.UNAVAILABLE), CallOutcome.BILLING_UNCERTAIN, "connection"),
        (
            LLMError(LLMErrorKind.INVALID_OUTPUT),
            CallOutcome.BILLING_UNCERTAIN,
            "invalid_output",
        ),
    ],
)
def test_failures_are_definite_only_when_the_provider_answered(
    error: LLMError, outcome: CallOutcome, code: str
) -> None:
    assert classify_failure(error) == (outcome, code)


def test_production_wiring_is_consent_then_admission_without_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OPENAI_API_KEY", "LLM_MONTHLY_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/db",
        openai_api_key="sk-test-synthetic",
    )

    client = bootstrap.build_llm_client(settings, None, FakeClock())  # type: ignore[arg-type]

    assert bootstrap.OPENAI_MAX_RETRIES == 0
    assert isinstance(client, ConsentGatedLLMClient)
    assert isinstance(client._inner, AdmittedLLMClient)
