"""Deletions the owner asked for with a button (S03.5; M03 D83).

A button press only records the request: in the press's transaction it
defers `privacy.delete_data(account_id, scope, subject_id, card_id)` with
the owner's control lock (`acct:{id}:control`). That lock serializes it with
owner runs and applied confirmations — the way a confirmed deletion tool
already runs — so a run that read a chat before the tap finishes first, and
its answer, held calls and saved notes (all carrying refs to the chat) are
then deleted with the rest. Deleting inline in the press could not see
them: they did not exist yet (safety review item 1).

`RequestedDeletions.run` is the job body: idempotent through the deletion's
audit row (keyed by the card), one transaction, then the receipt.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.core.channel import ControlChannel
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.observability.logging import get_logger
from assistant.privacy import deletion
from assistant.privacy.deletion import DeletionReport, DeletionRequest

logger = get_logger(__name__)

SCOPES = (deletion.SCOPE_CONVERSATION, deletion.SCOPE_CONTACT, deletion.SCOPE_EVERYTHING)

#: What the owner is told when the press is handled (the receipt follows).
QUEUED_TEXT = (
    "🗑 Deleting now. I finish anything I was doing first, so nothing I write "
    "meanwhile keeps it; you get a receipt when it is done."
)
NOT_FOUND_TEXT = "Nothing was deleted: that chat or contact is no longer there."


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

    async def run(self, account_id: int, scope: str, subject_id: int, card_id: int) -> str:
        """Delete once (a re-run finds its audit row); tell the owner."""
        if scope not in SCOPES:
            logger.error("deletion_request_unknown_scope", account_id=account_id)
            return "unknown_scope"
        request = DeletionRequest.card(card_id)
        report: DeletionReport | None = None
        async with transaction(self._session_factory) as uow:
            session = uow.session
            account = await session.get(Account, account_id)
            if account is None:
                return "no_account"
            chat_id = account.control_chat_id
            if await deletion.find_deletion(session, account_id, request) is not None:
                return "already_done"
            try:
                report = await self._delete(session, account_id, scope, subject_id, request)
            except LookupError:
                report = None
        text = NOT_FOUND_TEXT if report is None else report.receipt()
        if chat_id is not None:
            await self._channel.send(account_id=account_id, chat_id=chat_id, text=text)
        logger.info("requested_deletion_done", account_id=account_id, found=report is not None)
        return "deleted" if report is not None else "not_found"

    async def _delete(
        self,
        session: AsyncSession,
        account_id: int,
        scope: str,
        subject_id: int,
        request: DeletionRequest,
    ) -> DeletionReport:
        if scope == deletion.SCOPE_CONVERSATION:
            return await deletion.delete_conversation_data(
                session,
                account_id=account_id,
                conversation_id=subject_id,
                actor="owner",
                clock=self._clock,
                request=request,
            )
        if scope == deletion.SCOPE_CONTACT:
            return await deletion.delete_contact_data(
                session,
                account_id=account_id,
                contact_id=subject_id,
                actor="owner",
                clock=self._clock,
                request=request,
            )
        return await deletion.delete_everything(
            session, account_id=account_id, actor="owner", clock=self._clock, request=request
        )
