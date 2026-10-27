"""Records requested card operations after commit without contacting Telegram."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.db.uow import after_commit


class RecordingApprovalPresenter:
    def __init__(self) -> None:
        self.presented: list[int] = []
        self.synced: list[int] = []

    def present_after_commit(self, session: AsyncSession, action_id: int) -> None:
        async def present(_: async_sessionmaker[AsyncSession]) -> None:
            self.presented.append(action_id)

        after_commit(session, present)

    def sync_after_commit(self, session: AsyncSession, action_id: int) -> None:
        async def sync(_: async_sessionmaker[AsyncSession]) -> None:
            self.synced.append(action_id)

        after_commit(session, sync)
