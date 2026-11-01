import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from predatory_beavers.settings import get_settings


def test_match_logo_migration_preserves_match_and_manual_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "match-logo-migration.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    timestamp = "2026-01-01T00:00:00"
    team_id = "11111111111111111111111111111111"
    competition_id = "22222222222222222222222222222222"
    match_id = "33333333333333333333333333333333"
    home_asset_id = "44444444444444444444444444444444"
    away_asset_id = "55555555555555555555555555555555"
    try:
        command.upgrade(config, "0007")
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executemany(
                "INSERT INTO media_assets "
                "(id, storage_key, mime, size, checksum, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (home_asset_id, "home.webp", "image/webp", 100, "home", timestamp, timestamp),
                    (away_asset_id, "away.webp", "image/webp", 200, "away", timestamp, timestamp),
                ],
            )
            connection.execute(
                "INSERT INTO teams (id, slug, name, category, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (team_id, "men", "Existing club", "MEN", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO competitions "
                "(id, name, season, source, external_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (competition_id, "Existing league", "2025/26", "asb", "123", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO matches (id, team_id, competition_id, starts_at, club_side, "
                "home_team_name, away_team_name, home_score, away_score, status, source, "
                "external_id, home_logo_asset_id, away_logo_asset_id, notes, featured, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match_id,
                    team_id,
                    competition_id,
                    "2026-03-29T12:00:00",
                    "HOME",
                    "Existing club",
                    "Existing opponent",
                    83,
                    53,
                    "FINISHED",
                    "asb",
                    "456",
                    home_asset_id,
                    away_asset_id,
                    "Manually maintained match notes",
                    True,
                    timestamp,
                    timestamp,
                ),
            )
            original_columns = connection.execute("PRAGMA table_info(matches)").fetchall()
            match_columns = ", ".join(column[1] for column in original_columns)
            original_match = connection.execute(f"SELECT {match_columns} FROM matches").fetchall()
            related_tables = ("teams", "competitions", "media_assets", "content_blocks")
            original_related = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in related_tables
            }

        command.upgrade(config, "0008")
        with sqlite3.connect(database) as connection:
            columns = connection.execute("PRAGMA table_info(matches)").fetchall()
            assert columns[: len(original_columns)] == original_columns
            assert [
                (column[1], column[2], column[3]) for column in columns[len(original_columns) :]
            ] == [
                ("home_logo_url", "VARCHAR(2048)", 0),
                ("away_logo_url", "VARCHAR(2048)", 0),
            ]
            assert connection.execute(f"SELECT {match_columns} FROM matches").fetchall() == (
                original_match
            )
            assert connection.execute(
                "SELECT home_logo_url, away_logo_url FROM matches"
            ).fetchone() == (
                None,
                None,
            )
            urls = ("https://asb.example/home.png", "https://asb.example/away.png")
            connection.execute(
                "UPDATE matches SET home_logo_url = ?, away_logo_url = ? WHERE id = ?",
                (*urls, match_id),
            )
            assert connection.execute(
                "SELECT home_logo_url, away_logo_url FROM matches"
            ).fetchone() == (urls)
            for table, rows in original_related.items():
                assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        command.downgrade(config, "0007")
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA table_info(matches)").fetchall() == original_columns
            assert connection.execute("SELECT * FROM matches").fetchall() == original_match
            for table, rows in original_related.items():
                assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0007",
            )
    finally:
        get_settings.cache_clear()
