"""Synthetic current-read seam for tests that do not exercise generation races."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.owner_fence import StoredOwnerProposalFence
from assistant.actions.proposals import CurrentOwnerFence, UnavailableOwnerFence
from assistant.conversations.fences import capture_fence, observe_conversations


class FakeOwnerFence(StoredOwnerProposalFence):
    async def check_fence(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        run_id: int,
        fence_id: int | None,
        request_id: int,
        expected_request_version: int,
        conversation_id: int,
        requested_at: datetime,
    ) -> CurrentOwnerFence | UnavailableOwnerFence:
        if fence_id not in (None, 1):
            return UnavailableOwnerFence("not_found")
        reads = await observe_conversations(
            session, account_id=account_id, conversation_ids=(conversation_id,)
        )
        issued = await capture_fence(
            session,
            account_id=account_id,
            run_id=run_id,
            request=None,
            reads=reads,
            requested_at=requested_at,
        )
        return await super().check_fence(
            session,
            account_id=account_id,
            run_id=run_id,
            fence_id=issued,
            request_id=request_id,
            expected_request_version=expected_request_version,
            conversation_id=conversation_id,
            requested_at=requested_at,
        )
