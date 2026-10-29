"""Monitors: the spec the owner run writes, and the `evaluate_monitor` output
(ARCHITECTURE §7.5 `MonitorSpec`; S05.4 §3.1, §3.5; P2-T14, P2-T27).

**`CreateMonitorArgs` is the monitor spec.** The owner run — the trusted
interpreter — turns "notify me when Daniel replies" into these strict
arguments; the confirmation card shows them and their plain-language
interpretation before anything is active (`agent/tools/monitors.py`).
Constraints are Pydantic validators, never `format`/`pattern` schema
keywords, so the strict tool schema stays within plain strict mode (as the
S03.1 tools do); each broken rule is an `ArgumentRuleError` whose fixed text
the model is shown. What needs the database (ids of this account, chats not
ignored) is checked by the tool handler.

M05 supports the actions `alert` and `alert_with_summary` only (P2-T27);
`create_task`, `draft_reply` and `digest` arrive with M07.

**`MonitorVerdicts` is the `evaluate_monitor` output:** one verdict per
semantic monitor in scope, citing the matching messages. Plain on purpose
(every field required, no keywords; `language` first, like every structured
output here). Deterministic code validates it (`agent/monitor_eval.py`). A
change here bumps the `evaluate_monitor` prompt version (AGENTS §8).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from assistant.agent.tools.common import (
    ArgumentRuleError,
    OptionalId,
    check_id,
    local_to_utc,
    parse_local_moment,
)
from assistant.memory.search import query_terms

ConditionKind = Literal[
    "contact_replied", "keywords", "media_received", "semantic", "decision_requested", "urgent"
]
MediaKind = Literal["photo", "document", "voice", "video", "any"]

#: Conditions matched at ingestion by deterministic code, with no model call.
DETERMINISTIC_KINDS = frozenset({"contact_replied", "keywords", "media_received"})
#: Conditions judged by one tool-less `evaluate_monitor` call per burst.
SEMANTIC_KINDS = frozenset({"semantic", "decision_requested", "urgent"})

#: TL decision (spec): a one-shot monitor ends after 30 days unless the owner
#: says otherwise; a standing one runs until stopped.
ONE_SHOT_DEFAULT_TTL = timedelta(days=30)
MAX_TERMS = 10
MAX_TERM_CHARS = 40
MAX_DESCRIPTION_CHARS = 300
#: Named chats or contacts in one monitor's scope.
MAX_SCOPE_IDS = 20


def _ids(values: list[int]) -> list[int]:
    if len(values) > MAX_SCOPE_IDS:
        raise ArgumentRuleError(f"name at most {MAX_SCOPE_IDS} chats or contacts")
    for value in values:
        check_id(value)
    return values


ScopeIds = Annotated[list[int], AfterValidator(_ids)]


class MonitorScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_ids: ScopeIds = Field(
        description="Contacts to watch (ids from resolve_contact); [] if not used."
    )
    conversation_ids: ScopeIds = Field(description="Chats to watch by id; [] if not used.")
    all_observed: bool = Field(
        description=(
            "True to watch every chat the assistant observes (Observe or Assist mode); "
            "then leave contact_ids and conversation_ids empty."
        )
    )

    @model_validator(mode="after")
    def _all_or_named(self) -> Self:
        if self.all_observed and (self.contact_ids or self.conversation_ids):
            raise ArgumentRuleError(
                "all_observed already covers every observed chat: leave contact_ids and "
                "conversation_ids empty"
            )
        return self


class MonitorConditionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ConditionKind = Field(
        description=(
            "contact_replied: the contact writes (no AI needed); keywords: a message "
            "contains a literal word or phrase; media_received: a photo, document, voice "
            "or video arrives; semantic: a message means what `description` says (AI "
            "check); decision_requested: a contact asks the owner to decide; urgent: a "
            "message is urgent."
        )
    )
    contact_id: OptionalId = Field(
        description="contact_replied: the contact (from resolve_contact); otherwise null."
    )
    terms: list[str] = Field(
        description=(
            f"keywords: 1-{MAX_TERMS} words or short phrases, each 1-{MAX_TERM_CHARS} "
            "characters, as the owner gave them; otherwise []."
        )
    )
    description: str | None = Field(
        description=(
            f"semantic: what to look for, in the owner's words (1-{MAX_DESCRIPTION_CHARS} "
            "characters). decision_requested/urgent: optional topic to narrow it, or null."
        )
    )
    media_kinds: list[MediaKind] = Field(
        description="media_received: which kinds ('any' = any media); otherwise []."
    )

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if len(self.terms) > MAX_TERMS:
            raise ArgumentRuleError(f"give at most {MAX_TERMS} terms")
        for term in self.terms:
            if not 1 <= len(term.strip()) <= MAX_TERM_CHARS:
                raise ArgumentRuleError(f"each term must be 1-{MAX_TERM_CHARS} characters")
            if not query_terms(term):
                raise ArgumentRuleError("each term needs at least one letter or digit")
        if self.description is not None and not (
            1 <= len(self.description.strip()) <= MAX_DESCRIPTION_CHARS
        ):
            raise ArgumentRuleError(f"description must be 1-{MAX_DESCRIPTION_CHARS} characters")
        if self.kind == "contact_replied" and self.contact_id is None:
            raise ArgumentRuleError("contact_replied needs contact_id (use resolve_contact)")
        if self.kind == "keywords" and not self.terms:
            raise ArgumentRuleError("keywords needs at least one term")
        if self.kind == "semantic" and self.description is None:
            raise ArgumentRuleError("semantic needs a description of what to look for")
        if self.kind == "media_received" and not self.media_kinds:
            raise ArgumentRuleError("media_received needs at least one media kind")
        return self


class CreateMonitorArgs(BaseModel):
    """What to watch, for what, and what to do on a match (the monitor spec)."""

    model_config = ConfigDict(extra="forbid")

    scope: MonitorScopeArgs = Field(description="Which chats to watch.")
    condition: MonitorConditionArgs = Field(description="What counts as a match.")
    action: Literal["alert", "alert_with_summary"] = Field(
        description=(
            "alert: tell the owner who wrote and when; alert_with_summary: also the chat's "
            "latest triage reasons when they cover the message."
        )
    )
    lifecycle: Literal["one_shot", "standing"] = Field(
        description="one_shot: fire once, then end; standing: keep firing until stopped."
    )
    expires_at: str | None = Field(
        description=(
            "When the monitor ends, owner-local 'YYYY-MM-DD HH:MM'; null for the default "
            "(one_shot: 30 days; standing: never)."
        )
    )
    bypass_quiet_hours: bool = Field(
        description="True only when the owner asked to be reached even during quiet hours."
    )

    @model_validator(mode="after")
    def _rules(self) -> Self:
        parse_local_moment(self.expires_at)
        if self.condition.kind == "contact_replied":
            return self  # the scope is that contact (`effective_scope`)
        scope = self.scope
        if not (scope.all_observed or scope.contact_ids or scope.conversation_ids):
            raise ArgumentRuleError(
                "name at least one contact or chat, or set all_observed to true"
            )
        return self


class EffectiveScope(BaseModel):
    """The chats a monitor watches, after the kind's own rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contact_ids: tuple[int, ...]
    conversation_ids: tuple[int, ...]
    all_observed: bool


def effective_scope(args: CreateMonitorArgs) -> EffectiveScope:
    """`contact_replied` watches that contact's chat only (S05.4 §3.1); every
    other kind watches what the call named, without duplicates."""
    if args.condition.kind == "contact_replied" and args.condition.contact_id is not None:
        return EffectiveScope(
            contact_ids=(args.condition.contact_id,), conversation_ids=(), all_observed=False
        )
    return EffectiveScope(
        contact_ids=tuple(dict.fromkeys(args.scope.contact_ids)),
        conversation_ids=tuple(dict.fromkeys(args.scope.conversation_ids)),
        all_observed=args.scope.all_observed,
    )


def monitor_expiry(args: CreateMonitorArgs, *, timezone: str, now: datetime) -> datetime | None:
    """When the monitor ends, in UTC: the owner's local `expires_at`, which
    must be in the future, or the lifecycle's default."""
    local = parse_local_moment(args.expires_at)
    if local is None:
        return now + ONE_SHOT_DEFAULT_TTL if args.lifecycle == "one_shot" else None
    expires = local_to_utc(local, timezone)
    if expires <= now:
        raise ArgumentRuleError("expires_at must be in the future (owner-local time)")
    return expires


# ------------------------------------------------------------------ the run's output
class MonitorVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monitor_id: int = Field(description="The monitor's id, from [monitor:<id>].")
    matched: bool = Field(
        description="True only when a NEW message clearly meets the monitor's condition."
    )
    message_ids: list[int] = Field(
        description=(
            "The ids (from [msg:<id>]) of the NEW messages that meet it; [] when not matched."
        )
    )


class MonitorVerdicts(BaseModel):
    """One verdict per listed monitor, about ONE conversation's new messages."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(description="The owner's preferred language tag, e.g. 'uz-Latn'.")
    verdicts: list[MonitorVerdict] = Field(description="One verdict per listed monitor.")


__all__ = [
    "DETERMINISTIC_KINDS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_TERMS",
    "MAX_TERM_CHARS",
    "ONE_SHOT_DEFAULT_TTL",
    "SEMANTIC_KINDS",
    "CreateMonitorArgs",
    "EffectiveScope",
    "MonitorConditionArgs",
    "MonitorScopeArgs",
    "MonitorVerdict",
    "MonitorVerdicts",
    "effective_scope",
    "monitor_expiry",
]
