"""Repository bases (AGENTS §6.12, §9).

The point of `AccountScopedRepository` is that **forgetting the account filter
is not expressible**. There is no `get(id)` that takes only an id: the account
comes from the repository itself, set at construction, and every query this
class builds starts from a statement that already carries the filter.

Subclasses must build on `self._select()` / `self._filtered()` rather than
writing bare `select(Model)`. A test asserts that each repository refuses to
return another account's rows.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, cast

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.db.base import Base


class AccountScopeError(RuntimeError):
    """A repository was built without, or with an invalid, account context."""


class TenantOwned(Protocol):
    """What a model must expose to be account-scoped.

    The generic bound below is `Base`, because the model has to be a mapped
    class for `select()` to accept it — but `Base` cannot also promise an
    `account_id` column. This protocol names that second requirement so the
    casts below are checked against something explicit instead of `Any`.
    """

    id: Any
    account_id: Any


class AccountScopedRepository[ModelT: Base]:
    """Base for every repository over a tenant-owned table."""

    #: Set by each subclass to its mapped class, which must satisfy
    #: `TenantOwned`. A subclass whose model lacks `account_id` fails loudly on
    #: first use rather than quietly returning every tenant's rows.
    model: ClassVar[Any]

    def __init__(self, session: AsyncSession, account_id: int) -> None:
        if account_id is None or account_id <= 0:
            # Catching this here turns "0 means everything" into a loud error
            # rather than a silent cross-tenant read.
            raise AccountScopeError("a positive account_id is required")
        self._session = session
        self._account_id = account_id

    @property
    def account_id(self) -> int:
        return self._account_id

    @property
    def session(self) -> AsyncSession:
        return self._session

    @property
    def _columns(self) -> TenantOwned:
        """The model's columns, typed by the contract they must satisfy."""
        return cast(TenantOwned, self.model)

    def _scope(self) -> ColumnElement[bool]:
        return cast(ColumnElement[bool], self._columns.account_id == self._account_id)

    def _select(self) -> Select[tuple[ModelT]]:
        """A SELECT already restricted to this account. Always start here."""
        statement: Select[tuple[ModelT]] = select(self.model).where(self._scope())
        return statement

    async def get(self, entity_id: int) -> ModelT | None:
        """Fetch by primary key **within this account**.

        Returns None for a row that exists but belongs to someone else — the
        caller cannot tell the difference, which is the point.
        """
        statement = self._select().where(self._columns.id == entity_id)
        result = await self._session.execute(statement)
        return cast("ModelT | None", result.scalar_one_or_none())

    async def list_all(self, limit: int = 100) -> list[ModelT]:
        result = await self._session.execute(self._select().limit(limit))
        return cast("list[ModelT]", list(result.scalars().all()))

    async def count(self) -> int:
        from sqlalchemy import func

        statement = select(func.count()).select_from(self.model).where(self._scope())
        return int((await self._session.execute(statement)).scalar_one())

    def add(self, entity: ModelT) -> ModelT:
        """Attach a new row, forcing its `account_id` to this repository's.

        A caller that sets the wrong account on an object cannot smuggle it in.
        """
        # The bound is `Base`, which cannot promise `account_id`; the model is
        # required to satisfy `TenantOwned` instead.
        cast(TenantOwned, entity).account_id = self._account_id
        self._session.add(entity)
        return entity
