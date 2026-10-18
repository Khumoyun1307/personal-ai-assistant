"""Engine and session factory (ARCHITECTURE §3, §14.4).

No module-level singletons: `bootstrap.py` builds these and passes them in
(AGENTS §5). Holding an engine in a module global makes it impossible to run
two configurations in one process, which the tests need.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from assistant.db.base import APP_SCHEMA, JOBS_SCHEMA


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine.

    `hide_parameters` keeps bound values — which include message text — out of
    SQLAlchemy's exception messages (ARCHITECTURE §14.4). `search_path` covers
    both schemas so procrastinate's unqualified SQL resolves.
    """
    return create_async_engine(
        database_url,
        echo=echo,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={APP_SCHEMA},{JOBS_SCHEMA},public"},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory for the unit of work.

    `expire_on_commit=False` because async code must never trigger a lazy
    refresh after commit (AGENTS §10); attributes read afterwards would emit
    I/O outside the transaction.
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
