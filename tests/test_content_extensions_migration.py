import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config

from predatory_beavers.settings import get_settings

NEW_SECTIONS = ("contact_channels", "captains", "player_roles", "viewing_tips")


@pytest.fixture
def migration_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Config]]:
    database = tmp_path / "extensions.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    try:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(config, "0008")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO content_blocks "
                "(id,section,title,body,sort_order,active,is_deleted,deleted_at,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    "history",
                    "Preserved draft",
                    "Existing body\nMore text",
                    45,
                    0,
                    1,
                    "2026-02-03T00:00:00",
                    "2026-01-01T00:00:00",
                    "2026-02-03T00:00:00",
                ),
            )
        yield database, config
    finally:
        get_settings.cache_clear()


def database_rows(database: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(database) as connection:
        return {
            name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }


def content_indexes(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        row[1]: connection.execute(f'PRAGMA index_info("{row[1]}")').fetchall()
        for row in connection.execute("PRAGMA index_list(content_blocks)").fetchall()
    }


def test_content_extensions_upgrade_and_clean_downgrade_preserve_old_data(
    migration_db: tuple[Path, Config],
) -> None:
    database, config = migration_db
    before = database_rows(database)
    with sqlite3.connect(database) as connection:
        original_columns = connection.execute("PRAGMA table_info(content_blocks)").fetchall()
        indexes = content_indexes(connection)
    command.upgrade(config, "0009")
    upgraded = database_rows(database)
    for table, rows in before.items():
        if table == "content_blocks":
            assert [row[: len(original_columns)] for row in upgraded[table]] == rows
        elif table != "alembic_version":
            assert upgraded[table] == rows
    with sqlite3.connect(database) as connection:
        columns = connection.execute("PRAGMA table_info(content_blocks)").fetchall()
        additions = {row[1]: (row[2], row[3], row[4]) for row in columns[len(original_columns) :]}
        assert additions == {
            "subtitle": ("VARCHAR(200)", 0, None),
            "link_url": ("VARCHAR(2048)", 0, None),
            "link_label": ("VARCHAR(100)", 0, None),
        }
        assert connection.execute(
            "SELECT subtitle,link_url,link_label FROM content_blocks"
        ).fetchall() == [(None, None, None)] * len(before["content_blocks"])
        assert content_indexes(connection) == indexes
        for section in NEW_SECTIONS:
            connection.execute(
                "INSERT INTO content_blocks (id,section,title,body,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (uuid4().hex, section, "Allowed", "Body", "2026-01-01", "2026-01-01"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "INSERT INTO content_blocks (id,section,title,body,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (uuid4().hex, "invalid", "Invalid", "Body", "2026-01-01", "2026-01-01"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "INSERT INTO content_blocks "
                "(id,section,title,body,sort_order,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uuid4().hex, "history", "Invalid order", "Body", -1, "2026-01-01", "2026-01-01"),
            )
        connection.rollback()
    command.downgrade(config, "0008")
    assert database_rows(database) == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("PRAGMA table_info(content_blocks)").fetchall() == original_columns
        )
        assert content_indexes(connection) == indexes
    command.upgrade(config, "0009")
    assert database_rows(database) == upgraded


@pytest.mark.parametrize("enrichment", ["section", "subtitle", "link_url", "link_label"])
def test_unsafe_content_downgrade_refuses_even_soft_deleted_inactive_content(
    migration_db: tuple[Path, Config], enrichment: str
) -> None:
    database, config = migration_db
    command.upgrade(config, "0009")
    with sqlite3.connect(database) as connection:
        value = {
            "section": "captains",
            "subtitle": "Retained metadata",
            "link_url": "https://example.test/retained",
            "link_label": "Open",
        }[enrichment]
        connection.execute(
            f"UPDATE content_blocks SET {enrichment} = ? WHERE is_deleted=1 AND active=0", (value,)
        )
        columns = connection.execute("PRAGMA table_info(content_blocks)").fetchall()
        indexes = content_indexes(connection)
    before = database_rows(database)
    with pytest.raises(RuntimeError):
        command.downgrade(config, "0008")
    assert database_rows(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA table_info(content_blocks)").fetchall() == columns
        assert content_indexes(connection) == indexes
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0009",)
