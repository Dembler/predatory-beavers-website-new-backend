from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.db.uow import SqlAlchemyUnitOfWork
from predatory_beavers.modules.club.models import MediaAsset
from predatory_beavers.settings import Settings


def _settings(database_path: Path) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        database_busy_timeout_ms=4321,
    )


def test_settings_reject_non_sqlite_database() -> None:
    with pytest.raises(ValidationError, match=r"must use sqlite\+aiosqlite"):
        Settings(env="test", database_url="postgresql+asyncpg://localhost/example")


@pytest.mark.asyncio
async def test_sqlite_engine_enables_integrity_pragmas(tmp_path: Path) -> None:
    engine = create_engine(_settings(tmp_path / "pragmas.sqlite"))
    try:
        async with engine.connect() as connection:
            foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
            busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
        assert foreign_keys == 1
        assert busy_timeout == 4321
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unit_of_work_commits_and_rolls_back(tmp_path: Path) -> None:
    engine = create_engine(_settings(tmp_path / "uow.sqlite"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE values_log (value INTEGER NOT NULL)"))

        factory = create_session_factory(engine)
        async with factory() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)

            async with unit_of_work:
                await session.execute(text("INSERT INTO values_log (value) VALUES (1)"))

            assert await session.scalar(text("SELECT COUNT(*) FROM values_log")) == 0

            async with unit_of_work:
                await session.execute(text("INSERT INTO values_log (value) VALUES (2)"))
                await unit_of_work.commit()

            assert await session.scalar(text("SELECT COUNT(*) FROM values_log")) == 1

            with pytest.raises(RuntimeError, match="abort transaction"):
                async with unit_of_work:
                    await session.execute(text("INSERT INTO values_log (value) VALUES (3)"))
                    raise RuntimeError("abort transaction")

            assert await session.scalar(text("SELECT value FROM values_log")) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unit_of_work_recovers_session_after_failed_flush(tmp_path: Path) -> None:
    engine = create_engine(_settings(tmp_path / "failed-flush.sqlite"))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        async with factory() as session:
            uow = SqlAlchemyUnitOfWork(session)

            def asset(key: str) -> MediaAsset:
                return MediaAsset(storage_key=key, mime="image/webp", size=1, checksum="same")

            async with uow:
                session.add(asset("first"))
                await session.flush()
                await uow.commit()
            with pytest.raises(IntegrityError):
                async with uow:
                    session.add(asset("duplicate"))
                    await session.flush()
            async with uow:
                assert await session.scalar(text("SELECT COUNT(*) FROM media_assets")) == 1
                await uow.commit()
    finally:
        await engine.dispose()
