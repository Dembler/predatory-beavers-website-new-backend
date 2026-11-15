from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from predatory_beavers.settings import Settings


def database_connect_args(settings: Settings) -> dict[str, Any]:
    """Shared by async API/migrations and synchronous PostgreSQL transfer tools."""
    if make_url(settings.database_url).get_backend_name() != "postgresql":
        return {}
    return {
        "connect_timeout": settings.database_connect_timeout_seconds,
        "prepare_threshold": None,
        # Settings validates this identifier; never interpolate an unchecked search path.
        # Supavisor accepts --name=value; compact -cname=value is ignored by its parser.
        "options": f"--search_path={settings.database_schema}",
    }


def create_engine(settings: Settings) -> AsyncEngine:
    options: dict[str, Any] = {}
    if make_url(settings.database_url).get_backend_name() == "postgresql":
        options.update(
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
            connect_args=database_connect_args(settings),
        )
    engine = create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        hide_parameters=True,
        **options,
    )
    if engine.url.get_backend_name() == "sqlite":
        database_path = engine.url.database
        if database_path and database_path != ":memory:":
            Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        _configure_sqlite(engine, settings.database_busy_timeout_ms)
    return engine


def _configure_sqlite(engine: AsyncEngine, busy_timeout_ms: int) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragmas(dbapi_connection: Any, connection_record: Connection) -> None:
        del connection_record
        dbapi_connection.create_function(
            "casefold", 1, lambda value: str(value or "").casefold(), deterministic=True
        )
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        finally:
            cursor.close()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
