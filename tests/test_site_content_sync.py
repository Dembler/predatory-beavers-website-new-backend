import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from predatory_beavers.settings import get_settings
from scripts.sync_site_content import stable_id, sync_content


@pytest.fixture
def sync_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    database = tmp_path / "sync.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO venues (id,name,address,description,source,external_id,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "a" * 32,
                "Existing hall",
                "Адрес не указан (ASB)",
                "Импортировано из ASB; адрес требует уточнения",
                "asb",
                "42",
                "2026-01-01",
                "2026-01-01",
            ),
        )
    try:
        yield database
    finally:
        get_settings.cache_clear()


def write_sync_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "content": [
                    {
                        "key": "captain-men",
                        "section": "captains",
                        "title": "Captain",
                        "body": "Men's team",
                        "subtitle": "Club",
                    }
                ],
                "venues": [
                    {
                        "key": "existing",
                        "external_id": "42",
                        "name": "Existing hall",
                        "address": "Known address",
                        "latitude": 51.6,
                        "longitude": 39.2,
                    },
                    {"key": "new", "name": "New hall", "address": "Another address"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def rows(database: Path, table: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()


def test_sync_replaces_only_missing_fields_and_is_idempotent(
    sync_database: Path,
    tmp_path: Path,
) -> None:
    manifest = write_sync_manifest(tmp_path)
    original_content = rows(sync_database, "content_blocks")
    before_bytes = sync_database.read_bytes()
    preview = sync_content(sync_database, manifest)
    assert preview["changes"] == 3
    assert preview["backup"] is None
    assert sync_database.read_bytes() == before_bytes
    result = sync_content(sync_database, manifest, apply=True)
    backup = Path(result["backup"])
    assert rows(backup, "content_blocks") == original_content
    assert all(row in rows(sync_database, "content_blocks") for row in original_content)
    with sqlite3.connect(sync_database) as connection:
        assert connection.execute(
            "SELECT id,address,description,source,external_id,latitude,longitude FROM venues "
            "WHERE id=?",
            ("a" * 32,),
        ).fetchone() == ("a" * 32, "Known address", None, "asb", "42", 51.6, 39.2)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    audit = rows(sync_database, "admin_audit_log")
    repeated = sync_content(sync_database, manifest, apply=True)
    assert repeated["changes"] == 0
    assert repeated["backup"] is None
    assert rows(sync_database, "admin_audit_log") == audit


def test_sync_preserves_manual_details_and_hidden_or_deleted_content(
    sync_database: Path,
    tmp_path: Path,
) -> None:
    manifest = write_sync_manifest(tmp_path)
    sync_content(sync_database, manifest, apply=True)
    with sqlite3.connect(sync_database) as connection:
        connection.execute(
            "UPDATE venues SET address='Edited address',description='Edited description',"
            "latitude=50,longitude=40 WHERE id=?",
            ("a" * 32,),
        )
        connection.execute(
            "UPDATE content_blocks SET title='Edited name',active=0,is_deleted=1 WHERE id=?",
            (stable_id("content:captain-men"),),
        )
    before_content = rows(sync_database, "content_blocks")
    before_venues = rows(sync_database, "venues")
    assert sync_content(sync_database, manifest, apply=True)["changes"] == 0
    assert rows(sync_database, "content_blocks") == before_content
    assert rows(sync_database, "venues") == before_venues


def test_sync_rejects_bad_urls_before_writing(sync_database: Path, tmp_path: Path) -> None:
    manifest = write_sync_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["content"][0]["link_url"] = "javascript:alert(1)"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    before = sync_database.read_bytes()
    with pytest.raises(ValidationError):
        sync_content(sync_database, manifest, apply=True)
    assert sync_database.read_bytes() == before


def test_sync_rejects_ambiguous_venue_identity(sync_database: Path, tmp_path: Path) -> None:
    manifest = write_sync_manifest(tmp_path)
    with sqlite3.connect(sync_database) as connection:
        connection.execute(
            "INSERT INTO venues (id,name,address,source,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("b" * 32, "Existing hall", "Manual address", "manual", "2026-01-01", "2026-01-01"),
        )
    before = sync_database.read_bytes()
    with pytest.raises(ValueError, match="Ambiguous venue"):
        sync_content(sync_database, manifest, apply=True)
    assert sync_database.read_bytes() == before
