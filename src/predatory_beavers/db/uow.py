from types import TracebackType
from typing import Protocol, Self

from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction


class UnitOfWork(Protocol):
    """Transaction boundary owned by an application use case."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class SqlAlchemyUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._transaction: AsyncSessionTransaction | None = None
        self._committed = False

    async def __aenter__(self) -> Self:
        if self._transaction is not None:
            raise RuntimeError("UnitOfWork cannot be nested")
        self._transaction = self._session.get_transaction()
        if self._transaction is None:
            self._transaction = await self._session.begin()
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        transaction = self._transaction
        self._transaction = None
        if transaction is None or not transaction.is_active:
            return
        if exc_type is not None or not self._committed:
            await transaction.rollback()
        else:
            await transaction.commit()

    async def commit(self) -> None:
        if self._transaction is None:
            await self._session.commit()
        else:
            await self._transaction.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self._transaction is None:
            await self._session.rollback()
        elif self._transaction.is_active:
            await self._transaction.rollback()
        self._committed = False
