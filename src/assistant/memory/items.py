"""Curated memory: owner instructions, preferences, facts and contact notes
(ARCHITECTURE §7.3, §10.3, §11.1 layer 3; FR-MEM-01/03/06/07; S03.3).

Domain code only — no model calls. Every query is account-scoped, and every
contact or conversation an item names must be this account's (§6 invariant
12): otherwise `NotInAccountError`, the same as an id that does not exist.

- **save** stores the owner's words as given (trimmed; never paraphrased by
  code). Idempotent per `(run_id, tool_call_id)`: a repeated tool call
  returns the item it saved the first time.
- **forget** archives: the item is never used again at once, and the
  retention sweep deletes it once the one-tap Undo window has passed
  (`FORGET_UNDO_WINDOW`, M03 D69).
- **contact_facing** is the §7.3 isolation rule, the only way a
  contact-facing run (summarize now; draft/triage from M04/M05) loads memory:
  that contact's notes, that conversation's notes, and — only when asked —
  owner preferences/style and owner instructions marked `applies_to_replies`.
  Owner-scope **facts** never reach a contact-facing run (FR-MEM-06).

Every change writes one content-free audit row (ids and enums only), which
is what Undo reverses (`memory/undo.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import (
    Boolean,
    ColumnElement,
    delete,
    func,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.conversations.models import Contact, Conversation
from assistant.core.errors import NotInAccountError
from assistant.memory.models import (
    SCOPE_CONTACT,
    SCOPE_CONVERSATION,
    SCOPE_OWNER,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    MemoryItem,
)
from assistant.memory.preference_proof import OwnerCapture, read_capture

EVENT_MEMORY_SAVED = "memory_saved"
EVENT_MEMORY_FORGOTTEN = "memory_forgotten"
#: A forgotten item stays restorable this long (the Undo card's lifetime),
#: then the hourly sweep deletes it (§10.4).
FORGET_UNDO_WINDOW = timedelta(hours=24)
#: Longest note, in characters (the confirmation card must show it whole).
MAX_CONTENT = 400


@dataclass(frozen=True, slots=True)
class MemoryView:
    memory_id: int
    scope: str
    kind: str
    content: str
    contact_id: int | None
    conversation_id: int | None
    applies_to_replies: bool
    #: Saved from a run that had other people's text in its context: its
    #: words may be someone else's (M03 D70, revised by the safety review).
    tainted: bool
    created_at: datetime
    source: str
    #: May hold content from the managed chats: needs `ai_managed` to reach
    #: a model, and goes with managed-data deletion (invariant 9).
    managed: bool = False
    #: The chats and contacts its run drew on (`provenance.refs`).
    ref_conversation_ids: tuple[int, ...] = ()
    ref_contact_ids: tuple[int, ...] = ()
    owner_source_exact: bool = False
    status: str = STATUS_ACTIVE
    #: New unproved H13 preferences cannot inherit legacy draft applicability.
    owner_source_required: bool = False

    @property
    def untrusted(self) -> bool:
        return self.tainted or self.managed or (self.kind != "fact" and not self.owner_source_exact)

    @property
    def conversation_refs(self) -> frozenset[int]:
        """Every chat this item is about or was drawn from (for `refs` of a
        result that shows it, and for deletion; verification item 1)."""
        own = {self.conversation_id} if self.conversation_id is not None else set()
        return frozenset(own | set(self.ref_conversation_ids))

    @property
    def contact_refs(self) -> frozenset[int]:
        own = {self.contact_id} if self.contact_id is not None else set()
        return frozenset(own | set(self.ref_contact_ids))


def provenance(
    *, tainted: bool, managed: bool, refs: Mapping[str, list[int]] | None = None
) -> dict[str, Any]:
    """The flags (and, for an item from other people's text, the ids of the
    chats and contacts its run drew on) stored with an item. No content."""
    value: dict[str, Any] = {"tainted": tainted, "managed": managed}
    if refs and (refs.get("conversation_ids") or refs.get("contact_ids")):
        value["refs"] = {
            "conversation_ids": sorted(set(refs.get("conversation_ids", []))),
            "contact_ids": sorted(set(refs.get("contact_ids", []))),
        }
    return value


#: SQL conditions on the provenance flags. A tainted item counts as managed
#: even without the key: rows saved before `managed` existed carried only
#: `{"tainted": true}` (verification item 4).
IS_TAINTED = MemoryItem.provenance.contains({"tainted": True})
IS_MANAGED = MemoryItem.provenance.contains({"managed": True}) | IS_TAINTED


def _ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(i for i in value if isinstance(i, int) and not isinstance(i, bool))


def _view(row: MemoryItem) -> MemoryView:
    flags = row.provenance or {}
    raw_refs = flags.get("refs")
    refs: dict[str, Any] = raw_refs if isinstance(raw_refs, dict) else {}
    return MemoryView(
        managed=bool(flags.get("managed")) or bool(flags.get("tainted")),
        ref_conversation_ids=_ids(refs.get("conversation_ids")),
        ref_contact_ids=_ids(refs.get("contact_ids")),
        memory_id=row.id,
        scope=row.scope,
        kind=row.kind,
        content=row.content,
        contact_id=row.contact_id,
        conversation_id=row.conversation_id,
        applies_to_replies=row.applies_to_replies,
        tainted=bool((row.provenance or {}).get("tainted")),
        created_at=row.created_at,
        source=row.source,
        owner_source_exact=(
            read_capture(flags.get("owner_capture"), row.content) is not None
            and flags.get("tainted") is False
            and flags.get("managed") is False
            and not raw_refs
        ),
        status=row.status,
        owner_source_required=bool(flags.get("owner_source_required")),
    )


async def _require_contact(session: AsyncSession, account_id: int, contact_id: int) -> None:
    found = (
        await session.execute(
            select(Contact.id)
            .where(Contact.account_id == account_id)
            .where(Contact.id == contact_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotInAccountError("contact not found in this account")


async def _require_conversation(
    session: AsyncSession, account_id: int, conversation_id: int
) -> None:
    found = (
        await session.execute(
            select(Conversation.id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotInAccountError("conversation not found in this account")


def _audit(
    session: AsyncSession, *, account_id: int, actor: str, event: str, at: datetime, **details: Any
) -> AuditLogEntry:
    entry = AuditLogEntry(
        account_id=account_id,
        at=at,
        actor=actor,
        event=event,
        subject_type="memory_item",
        subject_id=details.get("memory_id"),
        details=details,
    )
    session.add(entry)
    return entry


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    item: MemoryView
    #: The audit row (the Undo reference); None for a repeated call.
    audit_id: int | None
    replaced_id: int | None = None
    #: The same (run, tool call) saved this before: nothing new was written.
    repeated: bool = False


async def get(session: AsyncSession, account_id: int, memory_id: int) -> MemoryView | None:
    row = (
        await session.execute(
            select(MemoryItem)
            .where(MemoryItem.account_id == account_id)
            .where(MemoryItem.id == memory_id)
        )
    ).scalar_one_or_none()
    return None if row is None else _view(row)


async def save(
    session: AsyncSession,
    *,
    account_id: int,
    content: str,
    kind: str,
    contact_id: int | None = None,
    conversation_id: int | None = None,
    applies_to_replies: bool = False,
    replaces_id: int | None = None,
    run_id: int | None = None,
    tool_call_id: str | None = None,
    tainted: bool = False,
    managed: bool | None = None,
    refs: Mapping[str, list[int]] | None = None,
    owner_capture: OwnerCapture | None = None,
    owner_source_required: bool = False,
    actor: str,
    now: datetime,
) -> SaveOutcome:
    """Store one item (scope from the target: contact, conversation or the
    owner). `replaces_id` archives an active item of this account in the same
    change (a correction). NotInAccountError for a foreign or unknown id.

    `tainted`: saved by a run that had other people's text in its context.
    `managed` defaults to `tainted` — every tainted item is treated as
    possibly holding managed-chat content (the conservative rule, safety
    review V2). `refs`: the chats and contacts that run drew on, so deleting
    one of them deletes the item too (S3)."""
    if contact_id is not None and conversation_id is not None:
        raise ValueError("an item is about a contact or a conversation, not both")
    text = content if owner_capture is not None else content.strip()
    if not text.strip() or len(text) > MAX_CONTENT:
        raise ValueError(f"content must be 1-{MAX_CONTENT} characters")
    keyed = run_id is not None and bool(tool_call_id)
    if keyed:
        existing = (
            await session.execute(
                select(MemoryItem)
                .where(MemoryItem.account_id == account_id)
                .where(MemoryItem.run_id == run_id)
                .where(MemoryItem.tool_call_id == tool_call_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return SaveOutcome(_view(existing), None, repeated=True)
    if contact_id is not None:
        await _require_contact(session, account_id, contact_id)
        scope = SCOPE_CONTACT
    elif conversation_id is not None:
        await _require_conversation(session, account_id, conversation_id)
        scope = SCOPE_CONVERSATION
    else:
        scope = SCOPE_OWNER
    replaced: MemoryItem | None = None
    if replaces_id is not None:
        replaced = (
            await session.execute(
                select(MemoryItem)
                .where(MemoryItem.account_id == account_id)
                .where(MemoryItem.id == replaces_id)
                .where(MemoryItem.status == STATUS_ACTIVE)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if replaced is None:
            raise NotInAccountError("memory item not found in this account")
        old = _view(replaced)
        if owner_capture is None:
            owner_source_required = owner_source_required or old.owner_source_required
        if (old.tainted or old.managed) and owner_capture is None:
            # A correction of a note from other people's text is one too: it
            # keeps the flags and the chats/contacts it came from, so the
            # per-chat deletion still finds it (verification item 1).
            tainted = True
            managed = True
            refs = {
                "conversation_ids": sorted(
                    set((refs or {}).get("conversation_ids", [])) | old.conversation_refs
                ),
                "contact_ids": sorted(set((refs or {}).get("contact_ids", [])) | old.contact_refs),
            }
        # C8 replaces the entire text with an independently validated owner
        # source. The archived old row keeps its origins for Undo/deletion;
        # none of its prose or genuine dependencies is copied into the new row.
    values: dict[str, Any] = {
        "account_id": account_id,
        "scope": scope,
        "contact_id": contact_id,
        "conversation_id": conversation_id,
        "kind": kind,
        "content": text,
        "applies_to_replies": applies_to_replies,
        "provenance": provenance(
            tainted=tainted, managed=tainted if managed is None else managed, refs=refs
        ),
        "run_id": run_id if keyed else None,
        "tool_call_id": tool_call_id if keyed else None,
        "created_at": now,
        "updated_at": now,
    }
    if owner_capture is not None:
        if read_capture(owner_capture, text) is None:
            raise ValueError("invalid owner capture")
        values["provenance"]["owner_capture"] = owner_capture.model_dump(mode="json")
    elif owner_source_required:
        values["provenance"]["owner_source_required"] = True
    statement = insert(MemoryItem).values(**values)
    if keyed:
        statement = statement.on_conflict_do_nothing(
            constraint="uq_memory_items_run_id_tool_call_id"
        )
    new_id = (await session.execute(statement.returning(MemoryItem.id))).scalar_one_or_none()
    if new_id is None:
        # Raced with the same call: report what it saved.
        row = (
            await session.execute(
                select(MemoryItem)
                .where(MemoryItem.account_id == account_id)
                .where(MemoryItem.run_id == run_id)
                .where(MemoryItem.tool_call_id == tool_call_id)
            )
        ).scalar_one()
        return SaveOutcome(_view(row), None, repeated=True)
    if replaced is not None:
        replaced.status = STATUS_ARCHIVED
        replaced.archived_at = now
        replaced.updated_at = now
    entry = _audit(
        session,
        account_id=account_id,
        actor=actor,
        event=EVENT_MEMORY_SAVED,
        at=now,
        memory_id=int(new_id),
        scope=scope,
        kind=kind,
        replaced_id=replaced.id if replaced is not None else None,
        **(
            {"owner_source_capture": True, "run_id": run_id, "tool_call_id": tool_call_id}
            if owner_capture is not None and keyed
            else {}
        ),
    )
    await session.flush()
    row = (await session.execute(select(MemoryItem).where(MemoryItem.id == new_id))).scalar_one()
    return SaveOutcome(
        _view(row), entry.id, replaced_id=replaced.id if replaced is not None else None
    )


@dataclass(frozen=True, slots=True)
class ForgetOutcome:
    item: MemoryView
    #: False: it was already forgotten (nothing changed).
    changed: bool
    audit_id: int | None


async def forget(
    session: AsyncSession, *, account_id: int, memory_id: int, actor: str, now: datetime
) -> ForgetOutcome:
    row = (
        await session.execute(
            select(MemoryItem)
            .where(MemoryItem.account_id == account_id)
            .where(MemoryItem.id == memory_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotInAccountError("memory item not found in this account")
    if row.status == STATUS_ARCHIVED:
        return ForgetOutcome(_view(row), False, None)
    row.status = STATUS_ARCHIVED
    row.archived_at = now
    row.updated_at = now
    entry = _audit(
        session,
        account_id=account_id,
        actor=actor,
        event=EVENT_MEMORY_FORGOTTEN,
        at=now,
        memory_id=row.id,
    )
    await session.flush()
    return ForgetOutcome(_view(row), True, entry.id)


async def set_status(
    session: AsyncSession, *, account_id: int, memory_id: int, active: bool, now: datetime
) -> bool:
    """For Undo: archive or restore one item. True if it changed. An item
    already deleted by the sweep cannot be restored (False)."""
    target, other = (STATUS_ACTIVE, STATUS_ARCHIVED) if active else (STATUS_ARCHIVED, STATUS_ACTIVE)
    result = await session.execute(
        update(MemoryItem)
        .where(MemoryItem.account_id == account_id)
        .where(MemoryItem.id == memory_id)
        .where(MemoryItem.status == other)
        .values(status=target, archived_at=None if active else now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(getattr(result, "rowcount", 0))


async def list_items(
    session: AsyncSession,
    account_id: int,
    *,
    contact_id: int | None = None,
    conversation_id: int | None = None,
    scope: str | None = None,
    limit: int = 20,
    offset: int = 0,
    kind: str | None = None,
    query: str | None = None,
    include_managed: bool = True,
) -> tuple[list[MemoryView], int]:
    """Active items, newest first: (page, total matching). A contact filter
    also returns the notes of that contact's conversations. Without
    `include_managed` (no `ai_managed` consent) items that may hold
    managed-chat content are left out before counting/paging (invariant 9).
    `query` is a literal, case-insensitive substring of the saved content.
    Pages use created_at/id descending, including a deterministic tie-break.
    Offsets describe the current matching set; restart after changing items."""
    statement = (
        select(MemoryItem)
        .where(MemoryItem.account_id == account_id)
        .where(MemoryItem.status == STATUS_ACTIVE)
    )
    if not include_managed:
        statement = statement.where(~IS_MANAGED)
    if contact_id is not None:
        await _require_contact(session, account_id, contact_id)
        chats = select(Conversation.id).where(
            Conversation.account_id == account_id, Conversation.contact_id == contact_id
        )
        statement = statement.where(
            or_(MemoryItem.contact_id == contact_id, MemoryItem.conversation_id.in_(chats))
        )
    if conversation_id is not None:
        await _require_conversation(session, account_id, conversation_id)
        statement = statement.where(MemoryItem.conversation_id == conversation_id)
    if scope is not None:
        statement = statement.where(MemoryItem.scope == scope)
    if kind is not None:
        statement = statement.where(MemoryItem.kind == kind)
    if query is not None:
        statement = statement.where(MemoryItem.content.icontains(query, autoescape=True))
    total = (
        await session.execute(select(func.count()).select_from(statement.subquery()))
    ).scalar_one()
    rows = await session.execute(
        statement.order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [_view(row) for row in rows.scalars().all()], int(total)


async def owner_items(
    session: AsyncSession, account_id: int, *, limit: int, include_managed: bool
) -> list[MemoryView]:
    """Owner-scope active items for the owner's own runs (control chat).
    Without `include_managed`, managed-origin items are left out entirely
    (invariant 9); the context builder never shows a tainted item's text."""
    statement = (
        select(MemoryItem)
        .where(MemoryItem.account_id == account_id)
        .where(MemoryItem.status == STATUS_ACTIVE)
        .where(MemoryItem.scope == SCOPE_OWNER)
    )
    if not include_managed:
        statement = statement.where(~IS_MANAGED)
    rows = await session.execute(
        statement.order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc()).limit(limit)
    )
    return [_view(row) for row in rows.scalars().all()]


@dataclass(frozen=True, slots=True)
class EffectivePreference:
    memory_id: int
    content: str
    scope: Literal["owner", "contact", "conversation"]
    kind: Literal["preference", "style", "instruction"]
    applies_to_replies: bool
    verification: Literal["owner_source_exact", "legacy_unverified"] = "owner_source_exact"


async def effective_owner_preferences(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int | None,
    purpose: Literal["owner_request", "assist"],
    managed_allowed: bool,
) -> tuple[EffectivePreference, ...]:
    """Only independently proved instructions; never globalize scoped notes.

    The caller's provider wrapper still enforces ai_control (and ai_managed
    for Assist). A selected chat must be account-local and consented even
    when the saved preference itself contains only owner-control content.
    """
    allowed: ColumnElement[bool] = MemoryItem.scope == SCOPE_OWNER
    if conversation_id is not None:
        if not managed_allowed:
            if purpose == "assist":
                return ()
            return await effective_owner_preferences(
                session,
                account_id=account_id,
                conversation_id=None,
                purpose=purpose,
                managed_allowed=False,
            )
        contact_id = await session.scalar(
            select(Conversation.contact_id).where(
                Conversation.account_id == account_id,
                Conversation.id == conversation_id,
            )
        )
        if contact_id is None:
            return ()
        allowed = (
            allowed
            | ((MemoryItem.scope == SCOPE_CONTACT) & (MemoryItem.contact_id == contact_id))
            | (
                (MemoryItem.scope == SCOPE_CONVERSATION)
                & (MemoryItem.conversation_id == conversation_id)
            )
        )
    elif purpose == "assist":
        return ()
    statement = select(MemoryItem).where(
        MemoryItem.account_id == account_id,
        MemoryItem.status == STATUS_ACTIVE,
        MemoryItem.kind.in_(("preference", "style", "instruction")),
        allowed,
        ~IS_TAINTED,
        ~IS_MANAGED,
    )
    if purpose == "assist":
        statement = statement.where(
            (MemoryItem.kind != "instruction") | MemoryItem.applies_to_replies.is_(True)
        )
    rows = await session.scalars(statement.order_by(MemoryItem.created_at, MemoryItem.id))
    return tuple(
        EffectivePreference(
            row.id,
            row.content,
            cast(Literal["owner", "contact", "conversation"], row.scope),
            cast(Literal["preference", "style", "instruction"], row.kind),
            row.applies_to_replies,
        )
        for row in rows
        if read_capture(row.provenance.get("owner_capture"), row.content) is not None
        and row.provenance.get("tainted") is False
        and row.provenance.get("managed") is False
        and not row.provenance.get("refs")
    )


#: Owner-scope kinds a contact-facing run may see (§7.3); never `fact`.
CONTACT_FACING_OWNER_KINDS = ("preference", "style")


async def contact_facing(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    include_owner_preferences: bool,
    limit: int = 30,
    preference_capture_enabled: bool = False,
) -> list[MemoryView]:
    """The §7.3 isolation rule: what a contact-facing run about ONE
    conversation may load. That conversation's contact's notes and that
    conversation's notes; with `include_owner_preferences` (reply drafting,
    M04) also owner preferences/style and owner instructions marked
    `applies_to_replies`. Never another contact's or conversation's items,
    never owner-scope facts (FR-MEM-06)."""
    contact_id = (
        await session.execute(
            select(Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if contact_id is None:
        return []
    # A note saved from another chat's text may carry that chat's content:
    # it never reaches this chat's run, even about this contact (invariant
    # 4; verification item 2). Only items from other people's text carry refs.
    drawn_elsewhere = or_(
        func.jsonb_path_exists(
            MemoryItem.provenance,
            literal_column("'$.refs.conversation_ids[*] ? (@ != $c)'::jsonpath"),
            func.jsonb_build_object("c", conversation_id),
            type_=Boolean,
        ),
        func.jsonb_path_exists(
            MemoryItem.provenance,
            literal_column("'$.refs.contact_ids[*] ? (@ != $k)'::jsonpath"),
            func.jsonb_build_object("k", int(contact_id)),
            type_=Boolean,
        ),
    )
    allowed = [
        (MemoryItem.scope == SCOPE_CONTACT)
        & (MemoryItem.contact_id == contact_id)
        & ~drawn_elsewhere,
        (MemoryItem.scope == SCOPE_CONVERSATION)
        & (MemoryItem.conversation_id == conversation_id)
        & ~drawn_elsewhere,
    ]
    if include_owner_preferences:
        # Preserve the legacy eligible notes, including those without C8
        # proof. H13 changes their authority, not their scoped availability.
        allowed.append(
            (MemoryItem.scope == SCOPE_OWNER)
            & (
                MemoryItem.kind.in_(CONTACT_FACING_OWNER_KINDS)
                | ((MemoryItem.kind == "instruction") & MemoryItem.applies_to_replies.is_(True))
            )
            # Owner items from other people's text never steer how replies
            # to anyone are written (safety review S2).
            & ~IS_TAINTED
            & ~IS_MANAGED
            # Persisted H13 guards survive callers using the legacy path.
            & ~MemoryItem.provenance.contains({"owner_source_required": True})
        )
    rows = await session.execute(
        select(MemoryItem)
        .where(MemoryItem.account_id == account_id)
        .where(MemoryItem.status == STATUS_ACTIVE)
        .where(or_(*allowed))
        # The newest `limit`, shown oldest first (review item 4).
        .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
        .limit(limit)
    )
    newest = [_view(row) for row in rows.scalars().all()]
    if include_owner_preferences and preference_capture_enabled:
        newest = [
            view
            for view in newest
            if not (
                view.owner_source_exact
                and view.kind == "instruction"
                and not view.applies_to_replies
            )
        ]
        # Applicable defaults must not disappear behind a page of recent
        # contact notes. This additional path requires exact owner proof;
        # legacy notes remain untrusted background when capture is enabled.
        effective = await effective_owner_preferences(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            purpose="assist",
            managed_allowed=True,
        )
        existing = {item.memory_id for item in newest}
        defaults = await session.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.account_id == account_id,
                MemoryItem.id.in_(
                    [item.memory_id for item in effective if item.memory_id not in existing]
                ),
            )
            .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
        )
        newest.extend(_view(row) for row in defaults)
        newest.sort(key=lambda item: (item.created_at, item.memory_id), reverse=True)
    newest.reverse()
    return newest


async def notes_for_panel(
    session: AsyncSession, account_id: int, conversation_id: int, *, limit: int = 5
) -> tuple[list[MemoryView], int]:
    """The notes shown on a conversation panel (J12): the contact's and the
    chat's own, newest first."""
    contact_id = (
        await session.execute(
            select(Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if contact_id is None:
        return [], 0
    return await list_items(session, account_id, contact_id=int(contact_id), limit=limit)


# ------------------------------------------------------- deletion, retention
def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_about_contacts(session: AsyncSession, account_id: int) -> int:
    """Managed-data deletion: every contact- and conversation-scoped item of
    the account (notes about the people in the managed chats), and every
    item that may hold managed-chat content, in any scope. The owner's own
    items stay (M03 D71, safety review V2)."""
    return _rowcount(
        await session.execute(
            delete(MemoryItem)
            .where(MemoryItem.account_id == account_id)
            .where(MemoryItem.scope.in_((SCOPE_CONTACT, SCOPE_CONVERSATION)) | IS_MANAGED)
        )
    )


def _drew_on(conversation_ids: Sequence[int], contact_ids: Sequence[int]) -> Any:
    """Items about these chats/contacts, or saved by a run that drew on them."""
    condition: Any = MemoryItem.conversation_id.in_(list(conversation_ids))
    if contact_ids:
        condition = condition | MemoryItem.contact_id.in_(list(contact_ids))
    for conversation_id in conversation_ids:
        condition = condition | MemoryItem.provenance.contains(
            {"refs": {"conversation_ids": [conversation_id]}}
        )
    for contact_id in contact_ids:
        condition = condition | MemoryItem.provenance.contains(
            {"refs": {"contact_ids": [contact_id]}}
        )
    return condition


async def delete_for_conversation(
    session: AsyncSession, account_id: int, conversation_id: int, contact_ids: Sequence[int]
) -> int:
    """A purged conversation: its notes, its contact's, and every item saved
    from a run that drew on them (safety review S3). The helper S03.5's
    per-conversation deletion calls."""
    return _rowcount(
        await session.execute(
            delete(MemoryItem)
            .where(MemoryItem.account_id == account_id)
            .where(_drew_on([conversation_id], contact_ids))
        )
    )


async def delete_for_contact(
    session: AsyncSession, account_id: int, contact_id: int, conversation_ids: Sequence[int]
) -> int:
    """A deleted contact (S03.5's per-contact deletion): their notes, their
    chats' notes, and every item saved from a run that drew on them."""
    return _rowcount(
        await session.execute(
            delete(MemoryItem)
            .where(MemoryItem.account_id == account_id)
            .where(_drew_on(conversation_ids, [contact_id]))
        )
    )


async def delete_for_chats(
    session: AsyncSession,
    account_id: int,
    conversation_ids: Sequence[int],
    contact_ids: Sequence[int],
) -> int:
    """S03.5 per-conversation deletion (FR-PRV-04): the notes about these
    chats, and every item (any scope) saved by a run that drew on the chats
    or their contacts (provenance refs) — it may hold their text. The
    owner's own notes about the people (contact scope, not from their text)
    stay; `delete_for_contact` takes those too (M03 D79).

    Built on `delete_for_conversation` (no contact ids, so the owner's own
    notes about the person stay), plus the items whose refs name only the
    chat's contact (saved from a read about them, e.g. `list_memories`)."""
    if not conversation_ids:
        return 0
    deleted = 0
    for conversation_id in conversation_ids:
        deleted += await delete_for_conversation(session, account_id, conversation_id, ())
    if contact_ids:
        drawn: Any = or_(
            *(
                MemoryItem.provenance.contains({"refs": {"contact_ids": [contact_id]}})
                for contact_id in contact_ids
            )
        )
        deleted += _rowcount(
            await session.execute(
                delete(MemoryItem).where(MemoryItem.account_id == account_id).where(drawn)
            )
        )
    return deleted


async def delete_all(session: AsyncSession, account_id: int) -> int:
    """Delete everything (S03.5): every item of the account, the owner's
    own instructions, preferences and facts included."""
    return _rowcount(
        await session.execute(delete(MemoryItem).where(MemoryItem.account_id == account_id))
    )


async def sweep_forgotten(session: AsyncSession, now: datetime) -> int:
    """Forgotten items whose Undo window has passed are deleted (§10.4)."""
    await session.execute(
        text("SELECT set_config('app.privacy_decided_at', :at, true)"),
        {"at": now.isoformat()},
    )
    return _rowcount(
        await session.execute(
            delete(MemoryItem)
            .where(MemoryItem.status == STATUS_ARCHIVED)
            .where(MemoryItem.archived_at < now - FORGET_UNDO_WINDOW)
        )
    )


async def clear_owner_capture_sources(
    session: AsyncSession,
    *,
    account_id: int,
    turn_ids: Sequence[int],
    everything: bool = False,
) -> None:
    """Drop structural source pointers without reloading or resurrecting prose."""
    statement = select(MemoryItem).where(
        MemoryItem.account_id == account_id,
        MemoryItem.provenance.bool_op("?")("owner_capture"),
    )
    if not everything:
        if not turn_ids:
            return
        statement = statement.where(
            or_(
                *(
                    MemoryItem.provenance.contains({"owner_capture": {"turn_ids": [turn_id]}})
                    for turn_id in turn_ids
                )
            )
        )
    for row in await session.scalars(statement.with_for_update()):
        proof = read_capture(row.provenance.get("owner_capture"), row.content)
        if proof is None:
            # Malformed legacy evidence never establishes trust.
            row.provenance = {
                key: value for key, value in row.provenance.items() if key != "owner_capture"
            }
            continue
        removed = set(proof.turn_ids) if everything else set(turn_ids)
        cleaned = proof.model_copy(
            update={
                "turn_ids": [value for value in proof.turn_ids if value not in removed],
                "source_spans": [
                    span for span in proof.source_spans if span.turn_id not in removed
                ],
            }
        )
        row.provenance = {**row.provenance, "owner_capture": cleaned.model_dump(mode="json")}
