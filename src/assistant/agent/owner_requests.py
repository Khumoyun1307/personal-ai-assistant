"""Flag-on C1 ingress: owner intent and exact sources before model I/O.

Only explicit messaging syntax opens a task. Unknown prose stays ordinary
chat; contact/assistant text is never parsed for authority. C1/C2 define no
model-facing request-opening tool. The parser preserves original offsets,
including Unicode, rather than accepting model-reconstructed dictation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import ControlTurn, PendingInput
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest, OwnerRequestSource
from assistant.actions.requests import (
    AddSources,
    CandidatePair,
    Continuation,
    LinkAction,
    Operation,
    OwnerContinuation,
    OwnerRequestView,
    RecordCandidates,
    RequestSource,
    SelectRecipient,
    SetQuestion,
    SetState,
    SourceSpan,
    load_request,
    lock_requests,
    open_request,
    resolve_continuation,
    update_request,
)
from assistant.agent.persistence import load_owner_continuation
from assistant.conversations import read
from assistant.conversations.models import Conversation

# These recognize imperative framing, never an arbitrary occurrence of
# "write" in a question, a quote, a search, or a negated instruction.
_EN = re.compile(
    r"^(?:(?:can|could)\s+you\s+)?(?:please\s+)?"
    r"(?:write(?:\s+a\s+(?:new\s+)?message)?\s+to|"
    r"send\s+(?:a\s+)?message\s+to|tell|message|new\s+message\s+to|"
    r"(?:prepare\s+a\s+)?reply\s+to)\s+(?P<recipient>[^:\n.!?]+)",
    re.IGNORECASE,
)
_RU = re.compile(
    r"^(?:пожалуйста,?\s+)?(?:напиши|написать|отправь\s+сообщение|"
    r"новое\s+сообщение|ответь|подготовь\s+ответ)\s+(?P<recipient>[^:\n.!?]+)",
    re.IGNORECASE,
)
_RU_EXACT = re.compile(r"^(?P<recipient>[^:\n.!?]+?)\s+дословно\s*(?=:)", re.IGNORECASE)
_UZ = re.compile(
    r"^(?P<recipient>[\w‘’ʻʼ' -]+?)(?:ga|га)\s+"
    r"(?P<framing>(?:(?:aynan|айнан|yangi\s+xabar|янги\s+хабар)\s+)?"
    r"(?:yoz|ёз|javob\s+(?:yoz|tayyorla)|жавоб\s+(?:ёз|тайёрла)))\b",
    re.IGNORECASE,
)
_UZ_BRIEF = re.compile(
    r"^(?P<recipient>[\w‘’ʻʼ' -]+?)(?:ga|га)\s+"
    r"(?P<brief>[^:\n]+?)\s+(?:yoz|ёз)[.!]?$",
    re.IGNORECASE,
)
_GENERATE = re.compile(
    r"\b(?:ask|explain|suggest|compose|compare|decline|about|спроси|объясни|"
    r"предложи|откажи|о|про|so[‘’ʻʼ']ra|сўра|haqida|ҳақида|taklif|таклиф)\b",
    re.IGNORECASE,
)
_EXACT = re.compile(r"\b(?:exactly|verbatim|дословно|aynan|айнан)\b", re.IGNORECASE)
_REPLY = re.compile(r"\b(?:reply|ответь|ответ|javob|жавоб)\b", re.IGNORECASE)
_UNRELATED = re.compile(
    r"^\s*(?:search\b|find\b|what\b|who\b|how\b|why\b|set\s+my\b|"
    r"найди\b|поищи\b|что\b|кто\b|как\b|почему\b|qidir\b|қидир\b)",
    re.IGNORECASE,
)
_NEW_TASK = re.compile(
    r"^\s*(?:search\b|find\b|set\s+my\b|найди\b|поищи\b|qidir\b|қидир\b)",
    re.IGNORECASE,
)
# Uzbek "Kamolaning savoliga javob tayyorla" (reply to Kamola's question):
# the dative attaches to the possessed noun, so the person is the genitive.
_UZ_POSSESSOR = re.compile(
    r"^(?P<name>[\w‘’ʻʼ'-]+?)(?:ning|нинг)\s+[\w‘’ʻʼ'-]+(?:i|и)$", re.IGNORECASE
)
# Russian commands address the recipient in the dative ("Напиши Камоле").
# Nominative forms of a name token are only alternative lookup queries for
# C5's real-match verdict; they never bind a weak guess or an ambiguity.
_RU_NOMINATIVE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ии", ("ия",)),
    ("е", ("а", "я")),
    ("у", ("", "а")),
    ("ю", ("й", "ь", "я")),
)
_CYRILLIC_WORD = re.compile(r"^[а-яё]{3,}$", re.IGNORECASE)
_MAX_RECIPIENT_FORMS = 8


@dataclass(frozen=True, slots=True)
class MessageIntent:
    operation: Operation
    recipient: str
    has_brief: bool
    literal_span: tuple[int, int] | None = None
    #: Alternative lookup queries (Russian nominative forms), never bindings.
    recipient_forms: tuple[str, ...] = ()


def _nominative_forms(recipient: str) -> tuple[str, ...]:
    options: list[list[str]] = []
    for token in recipient.split():
        forms = [token]
        if _CYRILLIC_WORD.match(token):
            lowered = token.casefold()
            for ending, replacements in _RU_NOMINATIVE:
                if lowered.endswith(ending):
                    stem = token[: -len(ending)]
                    forms.extend(stem + replacement for replacement in replacements)
                    break
        options.append(forms)
    combined = [""]
    for forms in options:
        combined = [f"{prefix} {form}".strip() for prefix in combined for form in forms]
        combined = combined[: _MAX_RECIPIENT_FORMS + 1]
    return tuple(form for form in combined if form != recipient)[:_MAX_RECIPIENT_FORMS]


def classify_message(text: str) -> MessageIntent | None:
    """Recognize explicit messaging commands; all offsets address original text."""
    offset = len(text) - len(text.lstrip())
    value = text[offset:]
    matched = (
        _UZ.match(value)
        or _UZ_BRIEF.match(value)
        or _EN.match(value)
        or _RU.match(value)
        or _RU_EXACT.match(value)
    )
    if matched is None:
        return None
    colon = value.find(":")
    header = value[:colon] if colon >= 0 else value.rstrip(".! ")
    recipient = matched["recipient"].strip()
    recipient = _EXACT.sub("", recipient).strip()
    # English/Russian put the content brief after the recipient. Preserve
    # the full command separately; only the identity query is split here.
    recipient = re.split(r"\s+(?:about|о|про)\s+", recipient, maxsplit=1, flags=re.IGNORECASE)[0]
    if matched.re is _UZ and (possessor := _UZ_POSSESSOR.match(recipient)) is not None:
        recipient = possessor["name"]
    if not recipient:
        return None
    russian = matched.re is _RU or matched.re is _RU_EXACT
    reply = _REPLY.search(header) is not None
    body_start = offset + colon + 1
    if colon >= 0 and body_start < len(text) and text[body_start] == " ":
        body_start += 1  # One framing separator, not body whitespace normalization.
    body = text[body_start:] if colon >= 0 else ""
    literal = (
        bool(body)
        and not reply
        and (_EXACT.search(header) is not None or _GENERATE.search(body) is None)
    )
    body_end = len(text)
    if literal and body and body[0] in {'"', "“", "«"}:
        closing = {'"': '"', "“": "”", "«": "»"}[body[0]]
        if body.endswith(closing):
            body_start += 1
            body_end -= 1
    span = (body_start, body_end) if literal and body_start < body_end else None
    return MessageIntent(
        operation="reply" if reply else "dictate" if literal else "compose",
        recipient=recipient,
        has_brief=(
            bool(body.strip()) or matched.re is _UZ_BRIEF or _GENERATE.search(header) is not None
        ),
        literal_span=span,
        recipient_forms=_nominative_forms(recipient) if russian else (),
    )


def _resolve_recipient(
    directory: list[read.DirectoryEntry], intent: MessageIntent
) -> tuple[read.ContactMatch | None, tuple[read.ContactMatch, ...]]:
    """A unique real match to bind, else the request-local choices to keep.

    Alternative forms are consulted only when the parsed name has no strong
    match. Any disagreement between them (two people) stays a choice.
    """
    first = read.resolve_contacts(directory, intent.recipient)
    if first.verdict == "unique_strong":
        return first.strong[0], ()
    if first.verdict == "ambiguous_strong":
        return None, first.strong
    strong: dict[int, read.ContactMatch] = {}
    for form in intent.recipient_forms:
        for match in read.resolve_contacts(directory, form).strong:
            strong.setdefault(match.entry.contact_id, match)
    if len(strong) == 1:
        return next(iter(strong.values())), ()
    if strong:
        return None, tuple(strong.values())[:10]
    return None, first.suggestions


def _sources(
    turn: ControlTurn,
    *,
    intent: MessageIntent | None = None,
    literal: bool = False,
    brief: bool = False,
) -> tuple[RequestSource, ...]:
    text = turn.text
    if not text:
        return ()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    def source(purpose: str, start: int, end: int) -> RequestSource:
        return RequestSource.model_validate(
            {
                "span": SourceSpan(
                    turn_id=turn.id, field="text", start=start, end=end, field_sha256=digest
                ),
                "purpose": purpose,
            }
        )

    result = [source("instruction" if brief else "constraint", 0, len(text))]
    span = intent.literal_span if intent is not None else (0, len(text)) if literal else None
    if span is not None:
        result.append(source("literal_body", *span))
    return tuple(result)


async def settle_request(
    session: AsyncSession, *, account_id: int, request_id: int, now: datetime
) -> OwnerRequestView | None:
    """Promote only application-validated ready inputs; never create an effect."""
    view = await load_request(session, account_id=account_id, request_id=request_id)
    if (
        view is None
        or view.state != "collecting"
        or view.conversation_id is None
        or view.expires_at <= now
    ):
        return view
    purposes = {source.purpose for source in view.sources}
    ready = (
        (view.operation == "dictate" and "literal_body" in purposes)
        or (view.operation == "compose" and "instruction" in purposes)
        or (view.operation == "reply" and view.target_message_id is not None)
        or (view.operation == "revise" and view.action_id is not None and "instruction" in purposes)
    )
    if not ready:
        if view.operation == "reply" and view.missing_slot != "target":
            question = PendingInput(
                account_id=account_id,
                kind="owner_request",
                ref_id=view.request_id,
                created_at=now,
                expires_at=view.expires_at,
            )
            session.add(question)
            await session.flush()
            asked = await update_request(
                session,
                account_id=account_id,
                request_id=view.request_id,
                expected_version=view.version,
                update=SetQuestion(slot="target", input_id=question.id),
                now=now,
            )
            if asked.status == "applied":
                return asked.view
        return view
    result = await update_request(
        session,
        account_id=account_id,
        request_id=view.request_id,
        expected_version=view.version,
        update=SetState(state="ready", reason=None),
        now=now,
    )
    return result.view if result.status == "applied" else view


def selected_request(view: OwnerRequestView, basis: Continuation | None = None) -> Continuation:
    return Continuation(
        "selected",
        request_id=view.request_id,
        version=view.version,
        action_id=view.action_id,
        action_version=view.action_version,
        basis=basis.basis if basis is not None else "focused_request",
    )


async def prepare_owner_request(
    session: AsyncSession,
    *,
    account_id: int,
    turn: ControlTurn,
    now: datetime,
    managed_allowed: bool,
) -> OwnerContinuation:
    """Persist this turn once, then load H2's consent/provenance-aware context."""
    if (
        not turn.text
        or turn.from_tainted_run
        or (turn.contains_managed and not turn.owner_text_verified)
        or (turn.third_party and not (turn.owner_text_verified and turn.quoted_text is not None))
    ):
        return OwnerContinuation(Continuation("none"))
    if not await lock_requests(session, account_id=account_id):
        return OwnerContinuation(Continuation("unavailable", code="not_available"))
    # A restarted job must select its own task, even if another turn has since
    # changed focus. Existing spans also identify a continued turn after restart.
    persisted = list(
        await session.scalars(
            select(OwnerRequest).where(
                OwnerRequest.account_id == account_id,
                (OwnerRequest.trigger_turn_id == turn.id)
                | OwnerRequest.id.in_(
                    select(OwnerRequestSource.request_id).where(
                        OwnerRequestSource.account_id == account_id,
                        OwnerRequestSource.turn_id == turn.id,
                    )
                ),
            )
        )
    )
    refs = (turn.refs or {}).get("owner_request_ids", [])
    linked_ids = refs if isinstance(refs, list) else []
    persisted = (
        [row for row in persisted if row.trigger_turn_id == turn.id]
        or [row for row in persisted if row.id in linked_ids]
        or persisted
    )
    selection = await resolve_continuation(
        session,
        account_id=account_id,
        turn_id=turn.id,
        reply_to_message_id=turn.reply_to_telegram_message_id,
        now=now,
    )
    if selection.basis == "explicit_reply" and selection.status != "selected":
        return OwnerContinuation(selection)
    if len(persisted) > 1:
        return OwnerContinuation(
            Continuation("ambiguous", request_ids=tuple(row.id for row in persisted))
        )
    existing = persisted[0].id if persisted else None
    intent = classify_message(turn.text)
    if existing is not None:
        view = await load_request(session, account_id=account_id, request_id=existing)
        assert view is not None
        selection = selected_request(view, selection)
    elif (
        selection.status == "selected"
        and selection.basis == "explicit_reply"
        and selection.action_id is not None
        and selection.request_id is None
        and managed_allowed
    ):
        # A pre-C1 card has no request to continue. Adopt only the selected
        # live version; never create a new target/body from some newer card.
        action = await session.scalar(
            select(Action).where(
                Action.account_id == account_id,
                Action.id == selection.action_id,
                Action.version == selection.action_version,
                Action.state.in_(("awaiting_approval", "blocked")),
            )
        )
        if action is not None:
            contact_id = await session.scalar(
                select(Conversation.contact_id).where(
                    Conversation.account_id == account_id,
                    Conversation.id == action.conversation_id,
                )
            )
            if contact_id is not None:
                view = await open_request(
                    session,
                    account_id=account_id,
                    turn_id=turn.id,
                    operation="revise",
                    sources=_sources(turn, brief=True),
                    now=now,
                )
                for update in (
                    SelectRecipient(
                        contact_id=contact_id,
                        conversation_id=action.conversation_id,
                        basis="explicit_reply",
                    ),
                    LinkAction(action_id=action.id, version=action.version),
                ):
                    result = await update_request(
                        session,
                        account_id=account_id,
                        request_id=view.request_id,
                        expected_version=view.version,
                        update=update,
                        now=now,
                    )
                    if result.status != "applied":
                        return OwnerContinuation(Continuation("unavailable", code="stale"))
                    view = result.view
                selection = selected_request(view, selection)
    elif selection.basis != "explicit_reply" and intent is not None:
        view = await open_request(
            session,
            account_id=account_id,
            turn_id=turn.id,
            operation=intent.operation,
            sources=_sources(
                turn, intent=intent, brief=intent.has_brief and intent.literal_span is None
            ),
            now=now,
        )
        if managed_allowed:
            unique, matches = _resolve_recipient(
                await read.contact_directory(session, account_id), intent
            )
            if unique is not None:
                if unique.entry.conversation_id is not None:
                    bound = await update_request(
                        session,
                        account_id=account_id,
                        request_id=view.request_id,
                        expected_version=view.version,
                        update=SelectRecipient(
                            contact_id=unique.entry.contact_id,
                            conversation_id=unique.entry.conversation_id,
                            basis="unique_strong",
                        ),
                        now=now,
                    )
                    if bound.status == "applied":
                        view = bound.view
            elif matches:
                # Without a unique real match, keep the same request-local
                # choices resolve_contact would record, so a later full-name
                # answer can select among them (H5). A weak guess is never
                # bound here; only the owner's answer selects a recipient.
                recorded = await update_request(
                    session,
                    account_id=account_id,
                    request_id=view.request_id,
                    expected_version=view.version,
                    update=RecordCandidates(
                        turn_id=turn.id,
                        candidates=tuple(
                            CandidatePair(
                                contact_id=match.entry.contact_id,
                                conversation_id=match.entry.conversation_id,
                            )
                            for match in matches
                        ),
                    ),
                    now=now,
                )
                if recorded.status == "applied":
                    view = recorded.view
        selection = selected_request(view)
    elif selection.status == "selected" and selection.request_id is not None:
        before = await load_request(session, account_id=account_id, request_id=selection.request_id)
        slot = before.missing_slot if before is not None else None
        # Decide a clear topic switch before H2 attaches request refs to this
        # turn: replying to an unrelated search must not resurrect the task.
        if (
            before is not None
            and selection.basis != "explicit_reply"
            and (
                _NEW_TASK.match(turn.text)
                or (
                    selection.basis == "focused_request"
                    and before.action_id is None
                    and _UNRELATED.match(turn.text)
                )
            )
        ):
            await update_request(
                session,
                account_id=account_id,
                request_id=before.request_id,
                expected_version=before.version,
                update=SetState(state="suspended", reason="topic_switch"),
                now=now,
            )
            return OwnerContinuation(Continuation("none"))
        loaded = await load_owner_continuation(
            session,
            account_id=account_id,
            turn=turn,
            now=now,
            managed_allowed=managed_allowed,
            selection=selection,
        )
        view = loaded.request
        if view is None:
            return loaded
        answering = view.state == "collecting" and view.missing_slot is not None
        if selection.basis == "explicit_reply" or answering:
            added = await update_request(
                session,
                account_id=account_id,
                request_id=view.request_id,
                expected_version=view.version,
                update=AddSources(
                    sources=_sources(
                        turn,
                        literal=view.operation == "dictate" and slot == "content",
                        brief=slot == "content" or selection.basis == "explicit_reply",
                    )
                ),
                now=now,
            )
            if added.status == "applied":
                selection = selected_request(added.view, selection)
            else:
                return OwnerContinuation(Continuation("unavailable", code="source_unavailable"))
    if selection.status == "selected" and selection.request_id is not None:
        settled = await settle_request(
            session, account_id=account_id, request_id=selection.request_id, now=now
        )
        if settled is not None:
            selection = selected_request(settled, selection)
    return await load_owner_continuation(
        session,
        account_id=account_id,
        turn=turn,
        now=now,
        managed_allowed=managed_allowed,
        selection=selection,
    )


async def refresh_owner_request(
    session: AsyncSession,
    *,
    account_id: int,
    continuation: OwnerContinuation,
    now: datetime,
) -> OwnerContinuation:
    """Refresh structural CAS facts after tools, without adding/replaying text."""
    request_id = continuation.selection.request_id
    if request_id is None or not await lock_requests(session, account_id=account_id):
        return continuation
    view = await settle_request(session, account_id=account_id, request_id=request_id, now=now)
    if view is None:
        return OwnerContinuation(Continuation("unavailable", code="not_available"))
    return replace(
        continuation, selection=selected_request(view, continuation.selection), request=view
    )
