"""Fill missing venue details and publish static site content for admin editing."""

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from predatory_beavers.modules.content.schemas import ContentCreate
from predatory_beavers.modules.matches.schemas import VenueCreate

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = UUID("d79234d5-36ef-4adf-9ea4-772857176681")
PLACEHOLDERS = {
    "",
    "адрес не указан (asb)",
    "адрес не указан",
    "нет информации",
    "информация отсутствует",
    "импортировано из asb; адрес требует уточнения",
}


def missing(value: object) -> bool:
    return value is None or str(value).strip().casefold() in PLACEHOLDERS


def stable_id(key: str) -> str:
    return uuid5(NAMESPACE, key).hex


def normalized_name(value: str) -> str:
    return re.sub(r'[\s"«»\-]+', "", value).casefold()


def read_rows(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    return {row["id"]: dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')}


def plan_sync(connection: sqlite3.Connection, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    content = read_rows(connection, "content_blocks")
    orders: dict[str, int] = {}
    for row in content.values():
        orders[row["section"]] = max(orders.get(row["section"], -1), row["sort_order"])
    for item in manifest["content"]:
        block_id = stable_id("content:" + item["key"])
        # An editor owns an existing imported record, including hidden/deleted records.
        if block_id in content or any(
            row["section"] == item["section"] and row["title"] == item["title"]
            for row in content.values()
        ):
            continue
        section = item["section"]
        orders[section] = orders.get(section, -1) + 1
        values = ContentCreate.model_validate(
            {key: value for key, value in item.items() if key != "key"}
            | {"sort_order": orders[section]}
        ).model_dump(mode="json")
        actions.append(
            {"table": "content_blocks", "id": block_id, "before": None, "values": values}
        )
        content[block_id] = values | {"id": block_id}

    venues = read_rows(connection, "venues")
    for item in manifest["venues"]:
        venue_id = stable_id("venue:" + item["key"])
        external_id = item.get("external_id")
        names = {normalized_name(name) for name in [item["name"], *item.get("aliases", [])]}
        candidates = [
            row
            for row in venues.values()
            if (
                row["id"] == venue_id
                or (external_id and row["source"] == "asb" and row["external_id"] == external_id)
                or normalized_name(row["name"]) in names
            )
        ]
        if len(candidates) > 1:
            raise ValueError(f"Ambiguous venue identity: {item['key']}")
        if candidates:
            row = candidates[0]
            if row["is_deleted"]:
                continue
            values = {}
            if missing(row["address"]):
                values["address"] = item["address"]
            if row["description"] is not None and missing(row["description"]):
                values["description"] = None
            if row["latitude"] is None and row["longitude"] is None and "latitude" in item:
                values.update(latitude=item["latitude"], longitude=item["longitude"])
            VenueCreate.model_validate(row | values)
            if values:
                actions.append(
                    {"table": "venues", "id": row["id"], "before": row, "values": values}
                )
        else:
            values = VenueCreate.model_validate(
                {
                    key: value
                    for key, value in item.items()
                    if key in {"name", "address", "latitude", "longitude"}
                }
                | ({"source": "asb", "external_id": external_id} if external_id else {})
            ).model_dump(mode="json")
            actions.append({"table": "venues", "id": venue_id, "before": None, "values": values})
            venues[venue_id] = values | {"id": venue_id, "is_deleted": False}
    return actions


def sync_content(database: Path, manifest_path: Path, *, apply: bool = False) -> dict[str, Any]:
    database = database.resolve(strict=True)
    source_database = (ROOT / "data/predatory_beavers.db").resolve()
    if database == source_database:
        raise ValueError("The source snapshot is not an import target")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("version") != 1:
        raise ValueError("Unsupported manifest version")
    source_hash = (
        hashlib.sha256(source_database.read_bytes()).hexdigest()
        if source_database.exists()
        else None
    )
    with sqlite3.connect(
        database.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        actions = plan_sync(connection, manifest)
        report: dict[str, Any] = {
            "apply": apply,
            "changes": len(actions),
            "actions": actions,
            "backup": None,
        }
        if not apply or not actions:
            if apply:
                connection.rollback()
            return report
        backup = database.with_name(
            f"{database.stem}.before-content-sync-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}.db"
        )
        with (
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source,
            sqlite3.connect(backup) as target,
        ):
            source.backup(target)
        report["backup"] = str(backup)
        now = datetime.now(UTC).isoformat()
        for action in actions:
            table, entity_id = action["table"], action["id"]
            values = action["values"] | {"updated_at": now}
            if action["before"] is None:
                values |= {"id": entity_id, "created_at": now, "is_deleted": False}
                columns = ",".join(f'"{key}"' for key in values)
                connection.execute(
                    f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in values)})',
                    tuple(values.values()),
                )
            else:
                columns = ",".join(f'"{key}"=?' for key in values)
                connection.execute(
                    f'UPDATE "{table}" SET {columns} WHERE id=?', (*values.values(), entity_id)
                )
            after = dict(
                connection.execute(f'SELECT * FROM "{table}" WHERE id=?', (entity_id,)).fetchone()
            )
            connection.execute(
                "INSERT INTO admin_audit_log (id,actor_username,action,entity_type,entity_id,"
                '"before","after",created_at) VALUES (?,?,?,?,?,?,?,?)',
                (
                    uuid4().hex,
                    "system",
                    "CREATE" if action["before"] is None else "UPDATE",
                    "venue" if table == "venues" else "content_block",
                    entity_id,
                    json.dumps(action["before"], ensure_ascii=False) if action["before"] else None,
                    json.dumps(after, ensure_ascii=False),
                    now,
                ),
            )
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign key check failed")
        if source_hash and hashlib.sha256(source_database.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("Source snapshot changed")
        connection.commit()
        report["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/local.db")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "docs/site-content-sync-manifest.json"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = sync_content(args.database, args.manifest, apply=args.apply)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "actions"}))


if __name__ == "__main__":
    main()
