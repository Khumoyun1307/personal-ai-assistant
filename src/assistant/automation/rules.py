"""Versioned persisted automation rules remain independent of prompt schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int


class MonitorScope(BaseModel):
    """The chats a monitor watches: named contacts and chats, or every chat
    the assistant observes (Observe or Assist mode)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contact_ids: tuple[int, ...] = ()
    conversation_ids: tuple[int, ...] = ()
    all_observed: bool = False


class MonitorCondition(BaseModel):
    """What counts as a match. Only the fields of its kind are set: the
    contact (`contact_replied`), the terms as the owner gave them and their
    `memory.search.normalize` forms (`keywords`), the owner's description
    (`semantic`; optional for `decision_requested`/`urgent`), the media
    kinds (`media_received`). Owner-authored text: `automations.spec` is a
    content column (ARCHITECTURE §10.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "contact_replied", "keywords", "media_received", "semantic", "decision_requested", "urgent"
    ]
    contact_id: int | None = None
    terms: tuple[str, ...] = ()
    normalized_terms: tuple[str, ...] = ()
    description: str | None = None
    media_kinds: tuple[Literal["photo", "document", "voice", "video", "any"], ...] = ()


class MonitorRule(RuleBase):
    """A confirmed monitor (S05.4 §3.1), stored in `automations.spec` with
    `kind = 'monitor'`. Persisted rules never change with prompts or tool
    schemas (ARCHITECTURE §7.5): a new shape is a new `schema_version`."""

    schema_version: Literal[1] = 1
    scope: MonitorScope
    condition: MonitorCondition
    action: Literal["alert", "alert_with_summary"]
    lifecycle: Literal["one_shot", "standing"]
    bypass_quiet_hours: bool = False
