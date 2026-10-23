"""Per-route model configuration and the processor behind each model
(ARCHITECTURE §7.4, §14.3; M03 D48).

A route is a run type. `LLM_ROUTE_<RUN_TYPE>_MODEL` overrides
`LLM_DEFAULT_MODEL` for that route (e.g. summaries moved up to
`gpt-6-sol` after an evaluation). Which **processor** (the company the data
goes to) serves a model is decided here, in one table, so the consent wrapper
can refuse a request whose route points at a processor the owner never
agreed to — a route switched to another provider's model cannot silently
send data there (SEC-02, §6 invariant 9). An unknown model has no processor
and is refused (fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

#: Model-id prefix → processor name as consent records it. The single place
#: this mapping lives; a new provider is a new row *and* a new consent.
MODEL_PROCESSORS: tuple[tuple[str, str], ...] = (
    ("gpt-", "OpenAI"),
    ("claude-", "Anthropic"),
    ("gemini-", "Google"),
)


def processor_for_model(model: str) -> str | None:
    """The processor that serves `model`, or None when the model is unknown."""
    for prefix, processor in MODEL_PROCESSORS:
        if model.startswith(prefix):
            return processor
    return None


@dataclass(frozen=True, slots=True)
class ModelRoutes:
    """`model_for(run_type)`: the route's override, else the default."""

    default: str
    overrides: Mapping[str, str] = field(default_factory=dict)

    def model_for(self, run_type: str) -> str:
        return self.overrides.get(run_type) or self.default

    def processor_for(self, run_type: str) -> str | None:
        return processor_for_model(self.model_for(run_type))
