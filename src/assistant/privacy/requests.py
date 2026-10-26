"""Deletions the owner asked for with a button (S03.5; M03 D83, D87, D88).

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

`RequestedDeletions.run` is the job body: idempotent through the deletion's
audit row (keyed by the card), one transaction, then the receipt. It is not
retried automatically: any failure is reported to the owner as it is — the
deletion is one transaction, so nothing was deleted, or, when the commit
itself reported an error, whatever the audit row shows (D87).
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.core.channel import ControlChannel
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

#: What the owner is told when the press is handled (the receipt follows).
QUEUED_TEXT = (
    "🗑 Deleting now. I finish anything I was doing first, so nothing I write "
    "meanwhile keeps it; you get a receipt when it is done."
)
NOT_FOUND_TEXT = "Nothing was deleted: that chat or contact is no longer there."
#: The job failed before its commit: the deletion is one transaction.
FAILED_TEXT = (
    "⚠️ The deletion failed, so nothing was deleted: it is done all at once or "
    "not at all. Please ask again — from the chat's panel (/settings), with "
    "/privacy delete, or by asking me."
)
#: The job failed and the check of its audit row failed too.
UNCLEAR_TEXT = (
    "⚠️ The deletion did not finish cleanly and I could not check whether it went "
    "through. It is done all at once or not at all, so either all of it was "
    "deleted or none of it. Look at the chat's panel (/settings) before asking again."
)

_Outcome = Literal["deleted", "failed", "unclear"]


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
        """Delete once (a re-run finds its audit row); tell the owner what
        happened, whatever happened (invariant 11)."""
        if scope not in SCOPES:
            logger.error("deletion_request_unknown_scope", account_id=account_id)
            return "unknown_scope"
        request = DeletionRequest.card(card_id)
        async with transaction(self._session_factory) as uow:
            account = await uow.session.get(Account, account_id)
            if account is None:
                return "no_account"
            chat_id = account.control_chat_id
            if await deletion.find_deletion(uow.session, account_id, request) is not None:
                return "already_done"
        report: DeletionReport | None = None
        try:
            async with transaction(self._session_factory) as uow:
                try:
                    report = await self._delete(
                        uow.session, account_id, scope, subject_id, request, turn_cutoff
                    )
                except LookupError:
                    report = None
        except Exception as error:
            # Any error, the commit's included. Type only in the log (§14.4).
            logger.error(
                "requested_deletion_failed",
                account_id=account_id,
                scope=scope,
                exc_type=type(error).__name__,
            )
            outcome, text = await self._after_failure(account_id, request)
            await self._tell(account_id, chat_id, text)
            if outcome == "deleted":
                return "deleted"
            raise  # the job ends failed: evidence, never a silent success
        text = NOT_FOUND_TEXT if report is None else report.receipt()
        await self._tell(account_id, chat_id, text)
        logger.info("requested_deletion_done", account_id=account_id, found=report is not None)
        return "deleted" if report is not None else "not_found"

    async def _after_failure(
        self, account_id: int, request: DeletionRequest
    ) -> tuple[_Outcome, str]:
        """Did the deletion commit after all? Its audit row says (the commit
        may have succeeded and only the reply to it been lost)."""
        try:
            async with transaction(self._session_factory) as uow:
                done = await deletion.find_deletion(uow.session, account_id, request)
                counts = None if done is None else dict(done.details or {})
        except Exception as error:
            logger.error(
                "requested_deletion_check_failed",
                account_id=account_id,
                exc_type=type(error).__name__,
            )
            return "unclear", UNCLEAR_TEXT
        if counts is None:
            return "failed", FAILED_TEXT
        return "deleted", deletion.receipt_text(counts)

    async def _tell(self, account_id: int, chat_id: int | None, text: str) -> None:
        if chat_id is None:
            logger.warning("requested_deletion_unannounced", account_id=account_id)
            return
        delivery = await self._channel.send(account_id=account_id, chat_id=chat_id, text=text)
        if not delivery.ok:
            logger.warning("requested_deletion_receipt_undelivered", account_id=account_id)

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
