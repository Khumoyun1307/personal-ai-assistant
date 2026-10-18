"""The consent choke point and metering around every LLM call.

§6 invariant 9: no content reaches the provider without the consent that gates
it. The consent wrapper raises *before* the inner client is called; FakeLLM
recording zero requests is the proof.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from assistant.accounts.service import ConsentMissingError, ConsentState
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient, estimate_cost
from assistant.llm.port import CallRecord, LLMRequest, Provenance, TextItem, Usage
from tests.fakes.llm import FakeLLM, text_response


def request(*provenances: Provenance) -> LLMRequest:
    items = tuple(
        TextItem("user", "synthetic", p) for p in provenances or (Provenance.OWNER_CONTROL,)
    )
    return LLMRequest(
        account_id=7,
        purpose="owner_request",
        model="gpt-6-luna",
        instructions="x",
        items=items,
        max_output_tokens=100,
        timeout_seconds=10,
        run_id=3,
    )


class Consents:
    def __init__(self, **granted: tuple[str, ...]) -> None:
        self._granted = granted
        self.reads: list[tuple[int, str]] = []

    async def __call__(self, account_id: int, kind: str) -> ConsentState:
        self.reads.append((account_id, kind))
        processors = self._granted.get(kind)
        if processors is None:
            return ConsentState(kind=kind, granted=False, policy_version=None, processors=())
        return ConsentState(kind=kind, granted=True, policy_version="v0", processors=processors)


async def test_no_control_consent_means_no_call() -> None:
    inner = FakeLLM(text_response("never"))
    client = ConsentGatedLLMClient(inner, Consents())

    with pytest.raises(ConsentMissingError):
        await client.complete(request())

    assert inner.requests == []


async def test_managed_content_needs_managed_consent() -> None:
    inner = FakeLLM(text_response("never"))
    client = ConsentGatedLLMClient(inner, Consents(ai_control=("OpenAI",)))

    with pytest.raises(ConsentMissingError, match="ai_managed"):
        await client.complete(request(Provenance.OWNER_CONTROL, Provenance.MANAGED))

    assert inner.requests == []


async def test_consent_must_name_this_processor() -> None:
    inner = FakeLLM(text_response("never"))
    client = ConsentGatedLLMClient(inner, Consents(ai_control=("SomeOtherProvider",)))

    with pytest.raises(ConsentMissingError, match="OpenAI"):
        await client.complete(request())

    assert inner.requests == []


async def test_consent_is_read_on_every_call_not_cached() -> None:
    consents = Consents(ai_control=("OpenAI",))
    client = ConsentGatedLLMClient(FakeLLM(text_response("a"), text_response("b")), consents)

    await client.complete(request())
    await client.complete(request())

    assert consents.reads == [(7, "ai_control"), (7, "ai_control")]


async def test_granted_consent_lets_the_call_through() -> None:
    inner = FakeLLM(text_response("ok"))
    client = ConsentGatedLLMClient(inner, Consents(ai_control=("OpenAI",), ai_managed=("OpenAI",)))

    result = await client.complete(request(Provenance.MANAGED))

    assert result.text == "ok"
    assert len(inner.requests) == 1


async def test_metering_records_every_successful_call() -> None:
    records: list[CallRecord] = []

    async def record(entry: CallRecord) -> None:
        records.append(entry)

    client = MeteredLLMClient(FakeLLM(text_response("ok", tokens=90)), record)

    await client.complete(request())

    assert len(records) == 1
    assert records[0].account_id == 7
    assert records[0].run_id == 3
    assert records[0].purpose == "owner_request"
    assert records[0].usage.input_tokens == 90
    assert records[0].request_id == "req_fake"


def test_cost_estimate_uses_the_configured_price_and_snapshot_names() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)

    assert estimate_cost("gpt-6-luna", usage) == Decimal("0.60")
    assert estimate_cost("gpt-6-luna-2026-09-01", usage) == Decimal("0.60")
    assert estimate_cost("gpt-6-lunatic", usage) is None
    assert estimate_cost("unknown-model", usage) is None
