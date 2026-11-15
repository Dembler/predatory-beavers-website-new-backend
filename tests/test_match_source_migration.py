"""The automation metadata migration must preserve existing editor match rows."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from predatory_beavers.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]


def test_match_source_migration_preserves_existing_editor_match(tmp_path, monkeypatch):
    database = tmp_path / "working.sqlite"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    try:
        with monkeypatch.context() as patch:
            patch.setenv("APP_DATABASE_URL", "sqlite+aiosqlite:///" + database.as_posix())
            get_settings.cache_clear()
            command.upgrade(config, "0019")
            with sqlite3.connect(database) as connection:
                now = "2026-11-15 10:00:00"
                connection.execute(
                    "INSERT INTO teams (id,slug,name,category,active,is_deleted,"
                    "created_at,updated_at) "
                    "VALUES (?,?,?,'MEN',1,0,?,?)",
                    ("a" * 32, "men", "Editor club", now, now),
                )
                connection.execute(
                    "INSERT INTO competitions (id,name,season,source,active,is_deleted,"
                    "created_at,updated_at) VALUES (?,?,?,'manual',1,0,?,?)",
                    ("b" * 32, "Editor competition", "2026-2027", now, now),
                )
                connection.execute(
                    "INSERT INTO matches (id,team_id,competition_id,starts_at,club_side,"
                    "home_team_name,away_team_name,home_score,away_score,status,source,external_id,"
                    "notes,active,is_deleted,created_at,updated_at) "
                    "VALUES (?,?,?,?,'HOME','Club','Opponent',77,70,'CANCELLED','asb','123',"
                    "'Editor decision',0,1,?,?)",
                    ("c" * 32, "a" * 32, "b" * 32, now, now, now),
                )
                before = connection.execute("SELECT * FROM matches").fetchone()
            command.upgrade(config, "head")
            with sqlite3.connect(database) as connection:
                assert connection.execute("SELECT * FROM matches").fetchone() == before
                assert connection.execute("SELECT * FROM match_source_snapshots").fetchall() == []
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    ScriptDirectory.from_config(config).get_current_head(),
                )
    finally:
        get_settings.cache_clear()
