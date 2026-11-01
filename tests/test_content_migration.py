import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from predatory_beavers.settings import get_settings


def test_content_migration_seeds_once_and_preserves_club_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "migration.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(config, "0006")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO teams (id, slug, name, category, active, is_deleted, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "12345678123456781234567812345678",
                    "men",
                    "Existing team",
                    "MEN",
                    True,
                    False,
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM content_blocks").fetchone() == (4,)
            assert connection.execute("SELECT name FROM teams").fetchone() == ("Existing team",)
            assert connection.execute(
                "SELECT title FROM content_blocks WHERE section = 'history'",
            ).fetchone() == ("Команда, университет, характер",)
        command.downgrade(config, "0006")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT name FROM teams").fetchone() == ("Existing team",)
        command.upgrade(config, "head")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM content_blocks").fetchone() == (4,)
    finally:
        get_settings.cache_clear()
