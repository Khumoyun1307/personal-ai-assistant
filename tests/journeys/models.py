"""Closed, data-only event and oracle vocabulary. No eval or executable strings."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Locale = Literal["uz-Latn", "uz-Cyrl", "ru", "en"]
LOCALES: tuple[Locale, ...] = ("uz-Latn", "uz-Cyrl", "ru", "en")


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Call(Value):
    name: str
    arguments: dict[str, Any]


class Owner(Value):
    kind: Literal["owner"] = "owner"
    text: str
    answer: str
    calls: tuple[Call, ...] = ()
    reply_to: str | None = None
    quote: str | None = None


class Contact(Value):
    kind: Literal["contact"] = "contact"
    alias: str
    text: str
    chat: str = "k"
    owner_manual: bool = False


class EditMessage(Value):
    kind: Literal["edit"] = "edit"
    alias: str
    text: str


class DeleteMessage(Value):
    kind: Literal["delete"] = "delete"
    alias: str


class Tap(Value):
    kind: Literal["tap"] = "tap"
    card: str
    version: int
    op: Literal["approve", "reject", "edit", "redraft"]


class Restart(Value):
    kind: Literal["restart"] = "restart"


class AdvanceClock(Value):
    kind: Literal["advance"] = "advance"
    seconds: int


class Revoke(Value):
    kind: Literal["revoke"] = "revoke"
    scope: Literal["ai_control", "ai_managed"]


class Fail(Value):
    kind: Literal["fail"] = "fail"
    stage: Literal["model", "presentation", "send"]
    outcome: Literal["definite", "uncertain"]


JourneyStep = Annotated[
    Owner | Contact | EditMessage | DeleteMessage | Tap | Restart | AdvanceClock | Revoke | Fail,
    Field(discriminator="kind"),
]


class CardCheck(Value):
    kind: Literal["card"] = "card"
    alias: str
    chat: str = "k"
    text: str
    version: int = 1
    state: str = "awaiting_approval"
    origin: Literal["owner_dictated", "agent_proposal"]
    reason: str | None = None
    presented: bool = True
    authorship: (
        Literal[
            "owner_dictated", "owner_literal_edit", "assistant_written", "agent_proposal_selected"
        ]
        | None
    ) = None


class RequestCheck(Value):
    kind: Literal["request"] = "request"
    state: str
    slot: Literal["recipient", "content", "target", "constraints"] | None = None
    chat: str | None = None


class CountCheck(Value):
    kind: Literal["count"] = "count"
    artifact: Literal["actions", "business_attempts", "nested_writers"]
    count: int


class ReplyCheck(Value):
    kind: Literal["reply"] = "reply"
    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()


class FactualReplyCheck(Value):
    kind: Literal["factual_reply"] = "factual_reply"
    card: str
    version: int
    status: Literal["awaiting_approval", "sending", "sent", "not_sent", "uncertain"]
    wording: str
    text_sha256: str | None = None


ArtifactCheck = Annotated[
    CardCheck | RequestCheck | CountCheck | ReplyCheck | FactualReplyCheck,
    Field(discriminator="kind"),
]


class DirectoryEntry(Value):
    kind: Literal["directory"] = "directory"
    alias: str
    name: str


class Settings(Value):
    kind: Literal["settings"] = "settings"
    locale: Locale


SyntheticSetup = Annotated[DirectoryEntry | Settings, Field(discriminator="kind")]


class JourneyCase(Value):
    case_id: str
    locale: Locale
    setup: tuple[SyntheticSetup, ...] = ()
    steps: tuple[JourneyStep, ...]
    deterministic_checks: tuple[ArtifactCheck, ...]
    quality_checks: tuple[
        Literal["intent", "language_script", "necessary_question", "no_cross_chat_leak"], ...
    ] = ()
