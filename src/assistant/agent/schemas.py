"""Model-facing structured-output schemas (ARCHITECTURE §7.5), and the
deterministic validation that runs after the model (§8.4 layer 4).

`ConversationSummaryOutput` is the `summarize` run's output (S03.2), for a
rolling summary and a period summary alike. It is versioned with its prompt
(`summarize.vN.md`): a change here bumps the prompt version (AGENTS §8), and
`output_model_for` picks the schema a prompt version was written for —
`ConversationSummaryOutputV4` for `summarize.v1`-`v4`, the current one from
`summarize.v5` (S03.7: a commitment's `status`; texts carry only content).

The schema is plain on purpose — every field required, nullable where
optional, no length or format keywords — so OpenAI strict structured
outputs accept it (M03 D52). Limits are enforced afterwards by `sanitize`,
which also drops every item that does not cite a message the model was
actually shown: an unsupported item is treated as invented and never stored
(FR-SUM-08, no fabricated facts). Who asked, promised or agreed is then
checked against the cited messages' authors (`agent/attribution.py`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from assistant.agent.prompt_registry import Prompt

CommitmentStatus = Literal["promised", "agreed", "requested"]


# ------------------------------------------------------------------ v5 (S03.7)
class SummaryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        description=(
            "Only the question, decision or fact itself, in the owner's language and "
            "script; never who asked or said it (the application adds that)."
        )
    )
    message_ids: list[int] = Field(
        description="Ids (from the brackets) of the messages that support it."
    )


class Commitment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: Literal["owner", "contact"] = Field(description="Who is to do it.")
    status: CommitmentStatus = Field(
        description=(
            "promised: `by` said they will do it; agreed: one side asked or offered "
            "and the other confirmed; requested: the other side asked `by` to do it "
            "and `by` has not agreed."
        )
    )
    text: str = Field(
        description=(
            "Only what is to be done, in the owner's language and script; never who "
            "promised, asked or agreed, nor to whom."
        )
    )
    due: str | None = Field(description="When, as stated in the messages, or null.")
    message_ids: list[int] = Field(description="Supporting message ids.")


class ConversationSummaryOutput(BaseModel):
    """One conversation's summary (`summarize.v5` on). Never covers more than
    one chat (§6 inv. 4)."""

    model_config = ConfigDict(extra="forbid")

    # First, so the model commits to the owner's language and script before it
    # writes a word of the summary (S03.6: Cyrillic chats were summarized in
    # Cyrillic for a uz-Latn owner while this field came last).
    language: str = Field(
        description=(
            "The owner's preferred language tag, e.g. 'uz-Latn': every text below "
            "is written in that language and script."
        )
    )
    overview: str = Field(description="What the chat is about and what happened, briefly.")
    open_questions_to_owner: list[SummaryPoint] = Field(
        description="Questions the contact asked the owner that are still unanswered."
    )
    commitments: list[Commitment] = Field(
        description=(
            "Things either side is to do that are not yet done: promised, agreed, or "
            "requested and not yet agreed."
        )
    )
    decisions_requested: list[SummaryPoint] = Field(
        description="Decisions the contact is waiting for from the owner."
    )
    key_facts: list[SummaryPoint] = Field(
        description="Dates, places, amounts and other facts stated in the messages."
    )
    needs_reply: bool = Field(
        description="True if the contact's latest messages call for a reply from the owner."
    )
    needs_reply_reason: str | None = Field(
        description="Why a reply is or is not needed, briefly; null if obvious."
    )


# ----------------------------------------------------- v1-v4 (S03.2-S03.6)
class SummaryPointV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="One short statement in the owner's language and script.")
    message_ids: list[int] = Field(
        description="Ids (from the brackets) of the messages that support it."
    )


class CommitmentV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: Literal["owner", "contact"] = Field(description="Who promised.")
    text: str = Field(description="What was promised, to whom, in the owner's language and script.")
    due: str | None = Field(description="When, as stated in the messages, or null.")
    message_ids: list[int] = Field(description="Supporting message ids.")


class ConversationSummaryOutputV4(BaseModel):
    """The schema of `summarize.v1`-`v4`, field for field: a rollback to v4
    sends exactly what v4 was evaluated with."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(
        description=(
            "The owner's preferred language tag, e.g. 'uz-Latn': every text below "
            "is written in that language and script."
        )
    )
    overview: str = Field(description="What the chat is about and what happened, briefly.")
    open_questions_to_owner: list[SummaryPointV4] = Field(
        description="Questions the contact asked the owner that are still unanswered."
    )
    commitments: list[CommitmentV4] = Field(
        description="Promises made by either side that are not yet done."
    )
    decisions_requested: list[SummaryPointV4] = Field(
        description="Decisions the contact is waiting for from the owner."
    )
    key_facts: list[SummaryPointV4] = Field(
        description="Dates, places, amounts and other facts stated in the messages."
    )
    needs_reply: bool = Field(
        description="True if the contact's latest messages call for a reply from the owner."
    )
    needs_reply_reason: str | None = Field(
        description="Why a reply is or is not needed, briefly; null if obvious."
    )


#: Either schema's output, as `sanitize` takes it.
SummaryOutput = ConversationSummaryOutput | ConversationSummaryOutputV4
#: The first prompt version written for the current schema.
_FIRST_V5 = 5


def output_model_for(
    prompt: Prompt,
) -> type[ConversationSummaryOutput] | type[ConversationSummaryOutputV4]:
    """The output schema a `summarize` prompt version was written for."""
    number = int(prompt.version.removeprefix("v"))
    return ConversationSummaryOutput if number >= _FIRST_V5 else ConversationSummaryOutputV4


#: Deterministic limits applied after validation.
MAX_OVERVIEW = 1500
MAX_ITEM_TEXT = 400
MAX_DUE = 100
MAX_REASON = 300
MAX_LANGUAGE = 20
MAX_ITEMS = 15
MAX_IDS_PER_ITEM = 10
_POINT_LISTS = ("open_questions_to_owner", "decisions_requested", "key_facts")


class SanitizeResult(NamedTuple):
    #: The stored form of the summary.
    summary: dict[str, Any]
    #: Items dropped because they cite no message the model was shown.
    uncited: int
    #: Items cut by the list cap.
    capped: int


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"


def _cited(ids: Iterable[int], allowed: frozenset[int]) -> list[int]:
    return sorted({i for i in ids if i in allowed})[:MAX_IDS_PER_ITEM]


def sanitize(output: SummaryOutput, allowed_ids: frozenset[int]) -> SanitizeResult:
    """The stored form of a summary, how many items were dropped as
    uncited, and how many were cut by the list cap.

    An item survives only if it cites at least one message id from
    `allowed_ids` (the messages shown in this call, plus — for a rolling
    summary — those cited by the summary it updates); ids outside the set
    are removed. Text is trimmed to fixed limits; lists are capped at
    `MAX_ITEMS`, and the stored summary says how many items the cap left out
    (`items_omitted`), so a reader never takes a capped list as complete.
    A v5 commitment keeps its `status`; a v4 one is stored without (legacy).
    """
    dropped = 0
    capped = 0
    result: dict[str, Any] = {
        "overview": _clip(output.overview, MAX_OVERVIEW) or "",
        "needs_reply": output.needs_reply,
        "needs_reply_reason": _clip(output.needs_reply_reason, MAX_REASON),
        "language": _clip(output.language, MAX_LANGUAGE) or "",
    }
    for key in _POINT_LISTS:
        kept: list[dict[str, Any]] = []
        for point in getattr(output, key):
            ids = _cited(point.message_ids, allowed_ids)
            text = _clip(point.text, MAX_ITEM_TEXT)
            if not ids or not text:
                dropped += 1
                continue
            kept.append({"text": text, "message_ids": ids})
        capped += max(0, len(kept) - MAX_ITEMS)
        result[key] = kept[:MAX_ITEMS]
    commitments: list[dict[str, Any]] = []
    for item in output.commitments:
        ids = _cited(item.message_ids, allowed_ids)
        text = _clip(item.text, MAX_ITEM_TEXT)
        if not ids or not text:
            dropped += 1
            continue
        stored: dict[str, Any] = {"by": item.by}
        if isinstance(item, Commitment):
            stored["status"] = item.status
        stored.update({"text": text, "due": _clip(item.due, MAX_DUE), "message_ids": ids})
        commitments.append(stored)
    capped += max(0, len(commitments) - MAX_ITEMS)
    result["commitments"] = commitments[:MAX_ITEMS]
    result["items_omitted"] = capped
    return SanitizeResult(result, dropped, capped)


def cited_ids(summary: dict[str, Any] | Any) -> frozenset[int]:
    """Every message id a stored summary cites (it may carry them forward)."""
    found: set[int] = set()
    if not isinstance(summary, dict):
        return frozenset()
    for key in (*_POINT_LISTS, "commitments"):
        items = summary.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            ids = item.get("message_ids") if isinstance(item, dict) else None
            if isinstance(ids, list):
                found.update(i for i in ids if isinstance(i, int) and not isinstance(i, bool))
    return frozenset(found)
