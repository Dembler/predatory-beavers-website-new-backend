import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from predatory_beavers.settings import get_settings
from scripts.import_legacy_content import LegacyManifest, import_content, plan_import


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    database = tmp_path / "content.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "0008")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO content_blocks "
                "(id, section, title, body, sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "11111111111111111111111111111111",
                    "history",
                    "Manually edited history",
                    "Manual content.\n\nAdditional text.",
                    4000,
                    "2026-01-01T00:00:00",
                    "2026-01-02T00:00:00",
                ),
            )
            connection.execute(
                "INSERT INTO teams (id, slug, name, category, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "22222222222222222222222222222222",
                    "men",
                    "Existing team",
                    "MEN",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )
            connection.execute(
                "INSERT INTO admin_audit_log "
                "(id, actor_username, action, entity_type, entity_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "33333333333333333333333333333333",
                    "existing-editor",
                    "UPDATE",
                    "content_block",
                    "11111111111111111111111111111111",
                    "2026-01-02T00:00:00",
                ),
            )
        yield database
    finally:
        get_settings.cache_clear()


def write_manifest(tmp_path: Path, entries: list[dict[str, str]] | None = None) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "repository": "https://github.com/example/legacy-club",
                "revision": "a" * 40,
                "source_files": {"index.html": "b" * 64},
                "rules_reference": "https://example.org/rules",
                "entries": entries if entries is not None else [entry("legacy-history")],
            }
        ),
        encoding="utf-8",
    )
    return path


def entry(
    source_key: str, *, section: str = "history", body: str = "Legacy history."
) -> dict[str, str]:
    return {
        "source_key": source_key,
        "section": section,
        "title": "Legacy club content",
        "body": body,
        "source_path": "index.html",
    }


def snapshot(database: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(database) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {
            name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for (name,) in tables
        }


def assert_existing_rows_preserved(
    before: dict[str, list[tuple[object, ...]]],
    after: dict[str, list[tuple[object, ...]]],
) -> None:
    assert before.keys() == after.keys()
    for table, rows in before.items():
        if table in {"content_blocks", "admin_audit_log"}:
            assert all(row in after[table] for row in rows), table
        else:
            assert after[table] == rows, table


def test_preview_is_read_only_and_apply_preserves_data_with_backup(
    tmp_path: Path, database: Path
) -> None:
    manifest_path = write_manifest(tmp_path)
    files_before = sorted(tmp_path.iterdir())
    before_bytes = database.read_bytes()
    before = snapshot(database)
    with sqlite3.connect(database) as connection:
        planned = plan_import(
            connection, LegacyManifest.model_validate_json(manifest_path.read_bytes())
        )
    assert [item["status"] for item in planned] == ["add"]

    result = import_content(database, manifest_path)
    assert result["apply"] is False
    assert result["backup"] is None
    assert sorted(tmp_path.iterdir()) == files_before
    assert database.read_bytes() == before_bytes
    assert snapshot(database) == before

    result = import_content(database, manifest_path, apply=True)
    assert result["added"] == 1
    assert result["skipped"] == 0
    assert result["apply"] is True
    assert isinstance(result["backup"], str)
    backup = Path(result["backup"])
    assert backup != database
    assert snapshot(backup) == before
    after = snapshot(database)
    assert_existing_rows_preserved(before, after)
    assert len(after["content_blocks"]) == len(before["content_blocks"]) + 1
    assert len(after["admin_audit_log"]) == len(before["admin_audit_log"]) + 1
    with sqlite3.connect(database) as connection:
        inserted = connection.execute(
            "SELECT id, section, title, body, sort_order, active, is_deleted "
            "FROM content_blocks WHERE id = ?",
            (planned[0]["id"],),
        ).fetchone()
        assert inserted is not None
        assert inserted[:4] == (
            planned[0]["id"],
            "history",
            "Legacy club content",
            "Legacy history.",
        )
        assert inserted[4] > 4000
        assert inserted[5:] == (1, 0)
        assert connection.execute(
            "SELECT count(*) FROM admin_audit_log WHERE action = 'IMPORT'"
        ).fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    after_files = sorted(tmp_path.iterdir())
    repeated = import_content(database, manifest_path, apply=True)
    assert repeated["added"] == 0
    assert repeated["skipped"] == 1
    assert repeated["backup"] is None
    assert snapshot(database) == after
    assert sorted(tmp_path.iterdir()) == after_files


@pytest.mark.parametrize("is_deleted", [False, True])
def test_changed_existing_id_is_skipped_without_overwriting_or_restoring(
    tmp_path: Path, database: Path, is_deleted: bool
) -> None:
    manifest_path = write_manifest(tmp_path)
    with sqlite3.connect(database) as connection:
        item_id = plan_import(
            connection, LegacyManifest.model_validate_json(manifest_path.read_bytes())
        )[0]["id"]
    import_content(database, manifest_path, apply=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE content_blocks SET title = ?, body = ?, active = ?, is_deleted = ?, "
            "updated_at = ? WHERE id = ?",
            ("Editor's title", "Editor's body", False, is_deleted, "2026-02-03T04:05:06", item_id),
        )
    changed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed_manifest["revision"] = "c" * 40
    changed_manifest["entries"][0]["body"] = "Changed source body."
    manifest_path.write_text(json.dumps(changed_manifest), encoding="utf-8")
    before = snapshot(database)
    result = import_content(database, manifest_path, apply=True)
    assert result["added"] == 0
    assert result["skipped"] == 1
    assert result["items"][0]["status"] == "skip_existing_key"
    assert snapshot(database) == before


def test_duplicate_normalized_body_is_skipped_only_in_same_section(
    tmp_path: Path, database: Path
) -> None:
    body = "  Manual content. \n Additional   text.  "
    manifest_path = write_manifest(
        tmp_path,
        [entry("duplicate-history", body=body), entry("new-rules", section="rules", body=body)],
    )
    before = snapshot(database)
    result = import_content(database, manifest_path, apply=True)
    assert result["added"] == 1
    assert result["skipped"] == 1
    assert [item["status"] for item in result["items"]] == ["skip_existing_text", "add"]
    assert_existing_rows_preserved(before, snapshot(database))


def test_existing_row_mutation_rolls_back_the_whole_import(tmp_path: Path, database: Path) -> None:
    manifest_path = write_manifest(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER mutate_existing_content AFTER INSERT ON content_blocks "
            "BEGIN UPDATE content_blocks SET body = 'Unexpected mutation' "
            "WHERE id = '11111111111111111111111111111111'; END"
        )
    before = snapshot(database)
    with pytest.raises(RuntimeError):
        import_content(database, manifest_path, apply=True)
    assert snapshot(database) == before


@pytest.mark.parametrize("invalid_field", ["section", "body", "source_key"])
def test_malformed_manifest_is_rejected_without_database_changes(
    tmp_path: Path, database: Path, invalid_field: str
) -> None:
    invalid_entry = entry("invalid")
    invalid_entry[invalid_field] = "unknown" if invalid_field == "section" else "   "
    manifest_path = write_manifest(tmp_path, [invalid_entry])
    before = snapshot(database)
    with pytest.raises(ValidationError):
        import_content(database, manifest_path, apply=True)
    assert snapshot(database) == before
