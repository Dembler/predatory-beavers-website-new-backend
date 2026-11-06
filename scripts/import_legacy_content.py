"""Preview or append reviewed legacy text without updating existing database records."""

import argparse
import hashlib
import json
import sqlite3
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from predatory_beavers.modules.content.schemas import ContentCreate

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/legacy-content-manifest.json"
DEFAULT_DATABASE = ROOT / "data/local.db"
IMPORT_NAMESPACE = UUID("66e65458-0480-4890-a17c-a36e15a71b60")


class LegacyEntry(ContentCreate):
    source_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9-]+$")
    source_path: str = Field(min_length=1, max_length=500)


class LegacyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1]
    repository: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_files: dict[str, str]
    rules_reference: str
    entries: list[LegacyEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source_keys(self) -> "LegacyManifest":
        keys = [entry.source_key for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("source_key must be unique")
        if any(entry.source_path not in self.source_files for entry in self.entries):
            raise ValueError("Each entry must reference a declared source file")
        return self


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def entry_id(manifest: LegacyManifest, entry: LegacyEntry) -> str:
    repository = manifest.repository.rstrip("/").removesuffix(".git")
    return uuid5(IMPORT_NAMESPACE, f"{repository}:{entry.source_key}").hex


def snapshot(connection: sqlite3.Connection) -> dict[str, dict[int, tuple[object, ...]]]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    return {
        table: {
            row[0]: tuple(row[1:])
            for row in connection.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid')
        }
        for table in tables
    }


def assert_preserved(
    connection: sqlite3.Connection,
    before: dict[str, dict[int, tuple[object, ...]]],
) -> None:
    after = snapshot(connection)
    if set(before) != set(after):
        raise RuntimeError("Database schema changed during text import")
    for table, rows in before.items():
        for rowid, values in rows.items():
            if after[table].get(rowid) != values:
                raise RuntimeError(f"Existing {table} row {rowid} changed; rolling back")
        if table not in {"content_blocks", "admin_audit_log"} and after[table] != rows:
            raise RuntimeError(f"Unexpected new rows in {table}; rolling back")


def plan_import(
    connection: sqlite3.Connection, manifest: LegacyManifest
) -> list[dict[str, object]]:
    existing = connection.execute("SELECT id, section, body FROM content_blocks").fetchall()
    ids = {row[0] for row in existing}
    bodies = {(row[1], normalize_text(row[2])) for row in existing}
    last_order = dict(
        connection.execute("SELECT section, max(sort_order) FROM content_blocks GROUP BY section")
    )
    plan: list[dict[str, object]] = []
    for entry in manifest.entries:
        block_id = entry_id(manifest, entry)
        body_key = (entry.section.value, normalize_text(entry.body))
        status = "add"
        if block_id in ids:
            status = "skip_existing_key"
        elif body_key in bodies:
            status = "skip_existing_text"
        order = int(last_order.get(entry.section.value, -1)) + 1
        if status == "add":
            last_order[entry.section.value] = order
            ids.add(block_id)
            bodies.add(body_key)
        plan.append(
            {
                "id": block_id,
                "source_key": entry.source_key,
                "section": entry.section.value,
                "title": entry.title,
                "body": entry.body,
                "subtitle": entry.subtitle,
                "link_url": str(entry.link_url) if entry.link_url else None,
                "link_label": entry.link_label,
                "active": entry.active,
                "sort_order": order,
                "status": status,
            }
        )
    return plan


def import_content(
    database: Path, manifest_path: Path, *, apply: bool = False
) -> dict[str, object]:
    database = database.resolve(strict=True)
    source_database = (ROOT / "data/predatory_beavers.db").resolve()
    if database == source_database:
        raise ValueError("The source snapshot must never be an import target")
    manifest_bytes = manifest_path.read_bytes()
    manifest = LegacyManifest.model_validate_json(manifest_bytes)
    mode = "rw" if apply else "ro"
    source_hash = (
        hashlib.sha256(source_database.read_bytes()).hexdigest()
        if source_database.exists()
        else None
    )
    report: dict[str, object]
    with sqlite3.connect(database.as_uri() + f"?mode={mode}", uri=True, timeout=20) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        before = snapshot(connection)
        plan = plan_import(connection, manifest)
        additions = [item for item in plan if item["status"] == "add"]
        optional_columns = {"subtitle", "link_url", "link_label"}
        database_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(content_blocks)")
        }
        missing_columns = optional_columns - database_columns
        if any(item[column] is not None for item in additions for column in missing_columns):
            raise ValueError("Migrate the database before importing content source metadata")
        report = {
            "repository": manifest.repository,
            "revision": manifest.revision,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "apply": apply,
            "added": len(additions),
            "skipped": len(plan) - len(additions),
            "by_section": dict(Counter(str(item["section"]) for item in additions)),
            "items": plan,
            "backup": None,
            "preserved_existing_rows": sum(len(rows) for rows in before.values()),
        }
        if not apply or not additions:
            if apply:
                connection.rollback()
            return report
        backup = database.with_name(
            f"{database.stem}.before-legacy-content-"
            f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}.db"
        )
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
        report["backup"] = str(backup)
        now = datetime.now(UTC).isoformat()
        columns = ["id", "section", "title", "body", "sort_order", "active"]
        columns.extend(sorted(optional_columns & database_columns))
        columns.extend(["created_at", "updated_at", "is_deleted"])
        placeholders = ",".join("?" for _ in columns)
        for item in additions:
            values = {**item, "created_at": now, "updated_at": now, "is_deleted": 0}
            connection.execute(
                f"INSERT INTO content_blocks ({','.join(columns)}) VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )
        connection.execute(
            "INSERT INTO admin_audit_log (id,actor_username,action,entity_type,entity_id,"
            "after,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                uuid4().hex,
                "system",
                "IMPORT",
                "legacy_content_import",
                manifest.revision,
                json.dumps(
                    {
                        "repository": manifest.repository,
                        "revision": manifest.revision,
                        "manifest_sha256": report["manifest_sha256"],
                        "block_ids": [item["id"] for item in additions],
                        "backup": str(backup),
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        assert_preserved(connection, before)
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite quick_check failed; rolling back")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign key check failed; rolling back")
        if source_hash is not None:
            if hashlib.sha256(source_database.read_bytes()).hexdigest() != source_hash:
                raise RuntimeError("Source snapshot checksum changed; rolling back")
        connection.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = import_content(args.database, args.manifest, apply=args.apply)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "items"}))
    for item in cast(list[dict[str, object]], report["items"]):
        print(json.dumps({key: value for key, value in item.items() if key != "body"}))


if __name__ == "__main__":
    main()
