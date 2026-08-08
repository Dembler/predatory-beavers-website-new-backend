from collections.abc import AsyncIterator

from dishka import AsyncContainer, Provider, Scope, make_async_container, provide
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.db.uow import SqlAlchemyUnitOfWork, UnitOfWork
from predatory_beavers.settings import Settings


class CoreProvider(Provider):
    scope = Scope.APP

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    @provide
    def settings(self) -> Settings:
        return self._settings

    @provide
    def engine(self, settings: Settings) -> AsyncEngine:
        return create_engine(settings)

    @provide
    def session_factory(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return create_session_factory(engine)

    @provide(scope=Scope.REQUEST)
    async def session(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    @provide(scope=Scope.REQUEST)
    def unit_of_work(self, session: AsyncSession) -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session)


def build_container(settings: Settings, *providers: Provider) -> AsyncContainer:
    return make_async_container(CoreProvider(settings), *providers)
