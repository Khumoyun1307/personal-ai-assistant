"""Synthetic C6 settlement seam; production has no passing default."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.proposals import CurrentOwnerFence, UnavailableOwnerFence
from assistant.conversations.models import Conversation, Message


class FakeOwnerFence:
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
        await session.scalar(
            select(Conversation.id)
            .where(Conversation.account_id == account_id, Conversation.id == conversation_id)
            .with_for_update()
        )
        watermark = await session.scalar(
            select(Message.id)
            .where(
                Message.account_id == account_id,
                Message.conversation_id == conversation_id,
                Message.direction == "contact",
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        return CurrentOwnerFence(1, watermark)

    async def bind_action(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        action_id: int,
        version: int,
        fence: CurrentOwnerFence,
        request_id: int,
        requested_at: datetime,
    ) -> None:
        return None
