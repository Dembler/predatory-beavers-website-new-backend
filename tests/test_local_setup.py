import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from predatory_beavers.settings import get_settings
from scripts import setup_local


def test_local_setup_backs_up_migrates_and_keeps_admin_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "predatory_beavers.db"
    credentials_path = tmp_path / "local-admin-credentials.json"
    monkeypatch.setattr(setup_local, "DATABASE", database)
    monkeypatch.setattr(setup_local, "CREDENTIALS", credentials_path)
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    config = Config(str(setup_local.ROOT / "alembic.ini"))
    try:
        command.upgrade(config, "0006")
        before_preview = database.read_bytes()
        setup_local.inspect()
        assert database.read_bytes() == before_preview
        assert not credentials_path.exists()
        setup_local.apply()
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        assert len(credentials["password"]) >= 24
        first_credentials = credentials_path.read_bytes()
        backups = list(tmp_path.glob("predatory_beavers.before-admin-*.db"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("SELECT version_num FROM alembic_version").fetchone() == ("0006",)
        with sqlite3.connect(database) as target:
            assert target.execute("SELECT version_num FROM alembic_version").fetchone() == (
                ScriptDirectory.from_config(config).get_current_head(),
            )
            assert target.execute("SELECT count(*) FROM users WHERE role='ADMIN'").fetchone() == (
                1,
            )
            history = target.execute(
                "SELECT id,title,body,subtitle,link_url,link_label FROM content_blocks "
                "WHERE section='history' ORDER BY id"
            ).fetchall()
            imported_history = [
                entry
                for name in ("legacy-content-manifest.json", "club-history-manifest.json")
                for entry in json.loads(
                    (setup_local.ROOT / "docs" / name).read_text(encoding="utf-8")
                )["entries"]
                if entry["section"] == "history"
            ]
            # One editorial introduction plus every reviewed legacy/archive chapter.
            assert len(history) == 1 + len(imported_history)
            assert sum(bool(row[4]) for row in history) == sum(
                bool(entry.get("link_url")) for entry in imported_history
            )
            assert target.execute(
                "SELECT count(*) FROM asb_roster_members WHERE is_listed=1"
            ).fetchone() == (30,)
        setup_local.apply()
        assert credentials_path.read_bytes() == first_credentials
        with sqlite3.connect(database) as target:
            assert target.execute("SELECT count(*) FROM users WHERE role='ADMIN'").fetchone() == (
                1,
            )
            assert (
                target.execute(
                    "SELECT id,title,body,subtitle,link_url,link_label FROM content_blocks "
                    "WHERE section='history' ORDER BY id"
                ).fetchall()
                == history
            )
    finally:
        get_settings.cache_clear()


def test_first_local_setup_copies_snapshot_without_modifying_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "snapshot.db"
    working = tmp_path / "local.db"
    monkeypatch.setattr(setup_local, "SOURCE_DATABASE", source)
    monkeypatch.setattr(setup_local, "DATABASE", working)
    monkeypatch.setattr(setup_local, "CREDENTIALS", tmp_path / "credentials.json")
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{source.as_posix()}")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(setup_local.ROOT / "alembic.ini")), "0006")
        snapshot = source.read_bytes()
        setup_local.inspect()
        assert not working.exists()
        setup_local.apply()
        assert source.read_bytes() == snapshot
        with sqlite3.connect(working) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            config = Config(str(setup_local.ROOT / "alembic.ini"))
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                ScriptDirectory.from_config(config).get_current_head(),
            )
            admin_count = connection.execute(
                "SELECT count(*) FROM users WHERE role='ADMIN'",
            ).fetchone()
            assert admin_count == (1,)
            assert connection.execute(
                "SELECT count(*) FROM asb_roster_snapshots WHERE is_published=1"
            ).fetchone() == (2,)
    finally:
        get_settings.cache_clear()
