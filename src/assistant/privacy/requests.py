"""Deletions the owner asked for with a button (S03.5; M03 D83, D87, D88, D90).

A button press only records the request: in the press's transaction it
defers `privacy.delete_data(account_id, scope, subject_id, card_id,
turn_cutoff)` with the owner's control lock (`acct:{id}:control`). That lock
serializes it with owner runs and applied confirmations — the way a confirmed
deletion tool already runs — so a run that read a chat before the tap
finishes first, and its answer, held calls and saved notes (all carrying refs
to the chat) are then deleted with the rest. Deleting inline in the press
could not see them: they did not exist yet (safety review item 1). The
revocation card's Delete (scope `managed`) goes the same way (fix round 2,
item 5).

`turn_cutoff` is the newest control turn when the owner tapped: the owner's
own turns after it (a question sent while the deletion waits) keep their
text (D88).

`RequestedDeletions.run` is the job body. Its outcome is decided once, under
the account's row lock (§9.4: the account first), and is on record before the
owner hears it (D90):

- **deleted** — the deletion's own audit row, keyed by the card and written in
  the deletion's transaction; then the counts-only receipt;
- **failed** / **unclear** — an `EVENT_DELETION_FAILED` row, written before
  the owner is told that nothing was deleted (the deletion is one
  transaction) or, when the commit itself raised, that it could not be
  confirmed either way (fix round 3, C).

A re-run of the job (a stalled worker's retry, or one that starts while the
first still runs) takes the same lock, finds either row and does nothing: it
never deletes behind "nothing was deleted" and never sends a second receipt
(at most once, like every control-chat announcement). A job cut off by a
worker shutdown has decided nothing and goes back to the queue
(`jobs/tasks.py`). A failure is never retried automatically (D87): the
message says how to ask again for its scope, and for the managed data — whose
revocation card is used up — it carries a fresh Delete button (fix round 3,
B).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import callbacks, service
from assistant.accounts.models import Account, AuditLogEntry, PendingInput
from assistant.core.channel import Button, ControlChannel, Delivery
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.observability.logging import get_logger
from assistant.privacy import deletion
from assistant.privacy.deletion import DeletionReport, DeletionRequest

logger = get_logger(__name__)

SCOPES = (
    deletion.SCOPE_CONVERSATION,
    deletion.SCOPE_CONTACT,
    deletion.SCOPE_EVERYTHING,
    deletion.SCOPE_MANAGED,
)

#: The audit event of a button deletion that did not go through, or could not
#: be confirmed: `{card_id, scope, outcome}`, ids and enums only. Written
#: before the owner is told; a re-run of the job finds it and does nothing.
EVENT_DELETION_FAILED = "data_deletion_failed"
OUTCOME_FAILED = "failed"
OUTCOME_UNCLEAR = "unclear"

#: The revocation card (S02.4; `telegram/control/handlers.py` answers it). A
#: failed managed-data deletion offers it again with only Delete; its
#: `ref_id` names the failed card, which marks it as a retry: the press then
#: asks nothing more (how to continue was asked with the first card).
MANAGED_DELETE_CARD = "managed_delete"
OP_DELETE_MANAGED = "delete"

#: The check after an error waits at most 15 s for the account row (an
#: in-flight commit of the deletion holds it); then it "could not confirm".
_BOUNDED_LOCK_WAIT = text("SET LOCAL lock_timeout = '15s'")

#: What the owner is told when the press is handled (the receipt follows).
QUEUED_TEXT = (
    "🗑 Deleting now. I finish anything I was doing first, so nothing I write "
    "meanwhile keeps it; you get a receipt when it is done."
)
NOT_FOUND_TEXT = "Nothing was deleted: that chat or contact is no longer there."

#: How to ask again, per scope (fix round 3, B). `/privacy delete` deletes
#: everything, so it is offered for that scope only; the deletion tools need
#: flags that are off by default, so they are not offered.
_PANEL_AGAIN = (
    "To try again, open the chat's panel (/settings → the chat) and tap Delete stored data…."
)
ASK_AGAIN = {
    deletion.SCOPE_CONVERSATION: _PANEL_AGAIN,
    deletion.SCOPE_CONTACT: _PANEL_AGAIN,
    deletion.SCOPE_EVERYTHING: "To try again, send /privacy delete.",
    deletion.SCOPE_MANAGED: "To try again, tap Delete below.",
}
#: The managed scope when no fresh card could be made (the check failed).
ASK_AGAIN_MANAGED_NO_CARD = (
    "A chat's panel in /settings shows what is still stored there; /privacy delete "
    "deletes everything."
)
_ASK_AGAIN_UNKNOWN = "Please ask again."

#: The job failed before its commit: the deletion is one transaction.
FAILED_TEXT = (
    "⚠️ The deletion failed, so nothing was deleted: it is done all at once or "
    "not at all. {ask_again}"
)
#: The commit itself raised, or the check after the error failed.
UNCLEAR_TEXT = (
    "⚠️ The deletion did not finish cleanly and I could not confirm whether it went "
    "through. It is done all at once or not at all, so either all of it was deleted "
    "or none of it. Check before asking again. {ask_again}"
)


def failure_text(scope: str, outcome: str, *, card_offered: bool) -> str:
    """What the owner is told when a deletion did not go through."""
    if scope == deletion.SCOPE_MANAGED and not card_offered:
        ask_again = ASK_AGAIN_MANAGED_NO_CARD
    else:
        ask_again = ASK_AGAIN.get(scope, _ASK_AGAIN_UNKNOWN)
    template = UNCLEAR_TEXT if outcome == OUTCOME_UNCLEAR else FAILED_TEXT
    return template.format(ask_again=ask_again)


async def find_failure(
    session: AsyncSession, account_id: int, request: DeletionRequest
) -> AuditLogEntry | None:
    """The failure this request already reported, if any."""
    return (
        (
            await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.account_id == account_id)
                .where(AuditLogEntry.event == EVENT_DELETION_FAILED)
                .where(AuditLogEntry.details.contains(dict(request.key)))
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _lock_account(session: AsyncSession, account_id: int) -> Account | None:
    """§9.4: the account first, FOR NO KEY UPDATE (ingestion's foreign-key
    share is compatible, as for `delete_everything`), held to the commit."""
    return await session.get(Account, account_id, with_for_update={"key_share": True})


def _known(scope: str) -> str:
    """For logs and the audit row: an enum, never an arbitrary string."""
    return scope if scope in SCOPES else "unknown"


@dataclass
class _Attempt:
    """What the job knew when an error struck."""

    chat_id: int | None = None
    #: Every statement of the deletion ran: an error now came from the
    #: commit, which may have gone through (fix round 3, C).
    committing: bool = False


@dataclass(frozen=True, slots=True)
class _Offer:
    """A fresh card sent with the failure message (`card_id`, its buttons)."""

    card_id: int
    buttons: tuple[Button, ...]


class RequestedDeletions:
    """The job body of `privacy.delete_data`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        channel: ControlChannel,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._channel = channel

    async def run(
        self,
        account_id: int,
        scope: str,
        subject_id: int,
        card_id: int,
        turn_cutoff: int | None = None,
    ) -> str:
        """Delete once; tell the owner what happened, whatever happened
        (invariant 11). Returns a note (an enum) for the job log."""
        request = DeletionRequest.card(card_id)
        attempt = _Attempt()
        try:
            note, report = await self._attempt(
                account_id, scope, subject_id, request, turn_cutoff, attempt
            )
        except Exception as error:
            # Any error: the first read's, the deletion's, the commit's. Type
            # only in the log (§14.4).
            logger.error(
                "requested_deletion_failed",
                account_id=account_id,
                scope=_known(scope),
                exc_type=type(error).__name__,
            )
            if await self._settle(account_id, scope, card_id, attempt) == "deleted":
                return "deleted"  # the commit went through; only its reply was lost
            raise  # the job ends failed: evidence, never a silent success
        if note in ("deleted", "not_found"):
            await self._tell(
                account_id, attempt.chat_id, NOT_FOUND_TEXT if report is None else report.receipt()
            )
            logger.info("requested_deletion_done", account_id=account_id, found=report is not None)
        return note

    async def _attempt(
        self,
        account_id: int,
        scope: str,
        subject_id: int,
        request: DeletionRequest,
        turn_cutoff: int | None,
        attempt: _Attempt,
    ) -> tuple[str, DeletionReport | None]:
        """One transaction: the account lock, the outcome on record, else the
        deletion (which writes its audit row)."""
        if scope not in SCOPES:
            raise ValueError("unknown deletion scope")
        async with transaction(self._session_factory) as uow:
            session = uow.session
            # Held to the commit: a re-run of this job, and the check after an
            # error, take it too, so they see this transaction's outcome and
            # never its middle (fix round 3).
            account = await _lock_account(session, account_id)
            if account is None:
                return "no_account", None
            attempt.chat_id = account.control_chat_id
            if await deletion.find_deletion(session, account_id, request) is not None:
                return "already_done", None  # its receipt went out (at most once)
            if await find_failure(session, account_id, request) is not None:
                return "already_reported", None  # "nothing was deleted" went out
            try:
                report: DeletionReport | None = await self._delete(
                    session, account_id, scope, subject_id, request, turn_cutoff
                )
            except LookupError:
                report = None
            attempt.committing = True
        return ("deleted" if report is not None else "not_found"), report

    async def _settle(self, account_id: int, scope: str, card_id: int, attempt: _Attempt) -> str:
        """After an error: did the deletion commit after all (only the reply to
        it was lost)? Decide once, under the account lock, record it, then
        tell the owner. Returns the outcome."""
        request = DeletionRequest.card(card_id)
        outcome = OUTCOME_UNCLEAR
        counts: dict[str, Any] | None = None
        offer: _Offer | None = None
        try:
            async with transaction(self._session_factory) as uow:
                session = uow.session
                # Bounded: a commit still in flight holds the account row.
                await session.execute(_BOUNDED_LOCK_WAIT)
                account = await _lock_account(session, account_id)
                if account is None:
                    return "no_account"
                attempt.chat_id = account.control_chat_id
                done = await deletion.find_deletion(session, account_id, request)
                if done is not None:
                    outcome = "deleted"
                    counts = dict(done.details or {})
                elif await find_failure(session, account_id, request) is not None:
                    return "already_reported"  # another run of this job told the owner
                else:
                    outcome = OUTCOME_UNCLEAR if attempt.committing else OUTCOME_FAILED
                    session.add(
                        AuditLogEntry(
                            account_id=account_id,
                            at=self._clock.now(),
                            actor="owner",
                            event=EVENT_DELETION_FAILED,
                            details={**request.key, "scope": _known(scope), "outcome": outcome},
                        )
                    )
                    if scope == deletion.SCOPE_MANAGED and attempt.chat_id is not None:
                        offer = await self._retry_card(session, account_id, card_id)
        except Exception as error:
            # Nothing is on record: a re-run may still delete, which "either
            # all of it or none of it" allows.
            logger.error(
                "requested_deletion_check_failed",
                account_id=account_id,
                exc_type=type(error).__name__,
            )
            outcome, counts, offer = OUTCOME_UNCLEAR, None, None
        if counts is not None:
            await self._tell(account_id, attempt.chat_id, deletion.receipt_text(counts))
        else:
            message = failure_text(scope, outcome, card_offered=offer is not None)
            await self._tell(account_id, attempt.chat_id, message, offer)
        return outcome

    async def _retry_card(self, session: AsyncSession, account_id: int, card_id: int) -> _Offer:
        """A fresh revocation card with Delete only (fix round 3, B)."""
        card = await service.create_card(
            session,
            account_id=account_id,
            kind=MANAGED_DELETE_CARD,
            clock=self._clock,
            ref_id=card_id,
        )
        assert card.token is not None
        data = callbacks.encode(MANAGED_DELETE_CARD, card.token, OP_DELETE_MANAGED)
        return _Offer(card.id, (Button("Delete", data),))

    async def _tell(
        self, account_id: int, chat_id: int | None, message: str, offer: _Offer | None = None
    ) -> None:
        if chat_id is None:
            logger.warning("requested_deletion_unannounced", account_id=account_id)
            return
        delivery = await self._channel.send(
            account_id=account_id,
            chat_id=chat_id,
            text=message,
            buttons=offer.buttons if offer is not None else (),
        )
        if not delivery.ok:
            logger.warning("requested_deletion_receipt_undelivered", account_id=account_id)
        if offer is not None:
            await self._record_offer(account_id, offer, delivery)

    async def _record_offer(self, account_id: int, offer: _Offer, delivery: Delivery) -> None:
        """A shown card remembers its message; one that surely never arrived
        is removed (nothing can press it). Best effort: the outcome is
        already on record and told."""
        try:
            async with transaction(self._session_factory) as uow:
                card = await uow.session.get(PendingInput, offer.card_id)
                if card is None:
                    return
                if delivery.ok:
                    card.prompt_message_id = delivery.message_id
                elif not delivery.uncertain:
                    await uow.session.delete(card)
        except Exception as error:
            logger.error(
                "requested_deletion_card_not_recorded",
                account_id=account_id,
                exc_type=type(error).__name__,
            )

    async def _delete(
        self,
        session: AsyncSession,
        account_id: int,
        scope: str,
        subject_id: int,
        request: DeletionRequest,
        turn_cutoff: int | None,
    ) -> DeletionReport:
        if scope == deletion.SCOPE_CONVERSATION:
            return await deletion.delete_conversation_data(
                session,
                account_id=account_id,
                conversation_id=subject_id,
                actor="owner",
                clock=self._clock,
                request=request,
                owner_turns_up_to=turn_cutoff,
            )
        if scope == deletion.SCOPE_CONTACT:
            return await deletion.delete_contact_data(
                session,
                account_id=account_id,
                contact_id=subject_id,
                actor="owner",
                clock=self._clock,
                request=request,
                owner_turns_up_to=turn_cutoff,
            )
        if scope == deletion.SCOPE_MANAGED:
            return await deletion.delete_managed_data(
                session,
                account_id=account_id,
                clock=self._clock,
                actor="owner",
                request=request,
                owner_turns_up_to=turn_cutoff,
            )
        return await deletion.delete_everything(
            session,
            account_id=account_id,
            actor="owner",
            clock=self._clock,
            request=request,
            owner_turns_up_to=turn_cutoff,
        )
