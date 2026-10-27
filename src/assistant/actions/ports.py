"""Outbound and approval presentation ports; effects live in adapters."""

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.delivery import SendResult


class BusinessSender(Protocol):
    async def send_business_text(
        self, *, account_id: int, conversation_id: int, text: str
    ) -> SendResult: ...

    async def edit_assistant_message(
        self, *, account_id: int, conversation_id: int, assistant_message_ref: int, text: str
    ) -> SendResult: ...

    async def delete_own_business_messages(
        self, *, account_id: int, conversation_id: int, assistant_message_refs: Sequence[int]
    ) -> SendResult: ...


class ApprovalCardPresenter(Protocol):
    def present_after_commit(self, session: AsyncSession, action_id: int) -> None: ...

    def sync_after_commit(self, session: AsyncSession, action_id: int) -> None: ...
