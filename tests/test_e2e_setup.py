import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from predatory_beavers.settings import get_settings
from scripts import setup_e2e


def test_e2e_setup_without_uploaded_media_keeps_source_and_reuses_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = setup_e2e.ROOT
    source = tmp_path / "data/local.db"
    source.parent.mkdir()
    database = source.with_name("e2e.db")
    media = tmp_path / "data/e2e-media"
    credentials = tmp_path / "data/e2e-credentials.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_e2e, "ROOT", tmp_path)
    monkeypatch.setattr(setup_e2e, "DATABASE", database)
    monkeypatch.setattr(setup_e2e, "MEDIA", media)
    monkeypatch.setattr(setup_e2e, "CREDENTIALS", credentials)
    for key, value in {
        "APP_DATABASE_URL": f"sqlite+aiosqlite:///{source.as_posix()}",
        "APP_MEDIA_STORAGE_PATH": str(tmp_path / "data/media"),
        "APP_ENV": "test",
        "APP_COOKIE_SECURE": "false",
        "APP_RELOAD": "false",
        "APP_CORS_ORIGINS": '["http://localhost:5173"]',
        "APP_E2E_RUNTIME_TOKEN": "preparation-token",
    }.items():
        monkeypatch.setenv(key, value)

    def migration_config(_path: str) -> Config:
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "migrations"))
        return config

    monkeypatch.setattr(setup_e2e, "Config", migration_config)
    get_settings.cache_clear()
    try:
        command.upgrade(migration_config(""), "0006")
        original = source.read_bytes()
        assert not (tmp_path / "data/media").exists()
        setup_e2e.prepare()
        assert source.read_bytes() == original
        assert media.is_dir()
        assert list(media.iterdir()) == []
        runtime = json.loads(credentials.read_text(encoding="utf-8"))
        assert runtime["databasePath"] == str(database)
        assert runtime["mediaPath"] == str(media)
        assert len(runtime["runtimeToken"]) >= 32
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                ScriptDirectory.from_config(migration_config("")).get_current_head(),
            )
            assert connection.execute(
                "SELECT count(*) FROM users WHERE role='ADMIN'"
            ).fetchone() == (1,)
        setup_e2e.prepare()
        assert json.loads(credentials.read_text(encoding="utf-8")) == runtime
        assert source.read_bytes() == original
    finally:
        get_settings.cache_clear()
