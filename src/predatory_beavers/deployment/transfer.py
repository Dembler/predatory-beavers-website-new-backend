"""Typed, insert-only transfer from a verified SQLite snapshot into PostgreSQL."""

from __future__ import annotations

import mimetypes
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.pool import NullPool

from predatory_beavers.api.idempotency import AdminCreationKey  # noqa: F401
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import database_connect_args
from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.integrations.object_storage.local import ObjectNotFoundError
from predatory_beavers.modules.achievements import models as achievements_models  # noqa: F401
from predatory_beavers.modules.audit import models as audit_models  # noqa: F401
from predatory_beavers.modules.auth import models as auth_models  # noqa: F401
from predatory_beavers.modules.club import models as club_models  # noqa: F401
from predatory_beavers.modules.content import models as content_models  # noqa: F401
from predatory_beavers.modules.imports import models as imports_models  # noqa: F401
from predatory_beavers.modules.matches import models as matches_models  # noqa: F401
from predatory_beavers.modules.results import models as results_models  # noqa: F401
from predatory_beavers.modules.roster import models as roster_models  # noqa: F401
from predatory_beavers.modules.standings import models as standings_models  # noqa: F401
from predatory_beavers.settings import Settings

# Login sessions and request replay keys belong to the previous installation.
EXCLUDED_TABLES = {"sessions", "admin_creation_keys", "cloud_sync_state"}
TRANSFER_LOCK_ID = 739_412_601


def source_engine(database: Path) -> Engine:
    """Open the immutable exported snapshot without modifying its bytes."""
    return create_engine(
        URL.create("sqlite+pysqlite", database=database.absolute().as_posix()),
        connect_args={"uri": True},
        creator=lambda: sqlite3.connect(
            database.absolute().as_uri() + "?mode=ro&immutable=1", uri=True
        ),
        poolclass=NullPool,
        hide_parameters=True,
    )


def target_engine(settings: Settings) -> Engine:
    if not settings.database_url.startswith("postgresql+psycopg://"):
        raise ValueError("The cloud target must be PostgreSQL")
    return create_engine(
        settings.database_url,
        connect_args=database_connect_args(settings),
        poolclass=NullPool,
        hide_parameters=True,
    )


def transfer_tables(source: Connection) -> list[Table]:
    source_tables = set(inspect(source).get_table_names())
    known = set(Base.metadata.tables) | {"alembic_version"}
    if source_tables - known:
        raise ValueError("The source contains tables unknown to this application version")
    result = []
    for table in Base.metadata.sorted_tables:
        if table.name in EXCLUDED_TABLES or table.name not in source_tables:
            continue
        actual = {column["name"] for column in inspect(source).get_columns(table.name)}
        if actual != set(table.columns.keys()):
            raise ValueError(f"The source schema is out of date: {table.name}")
        result.append(table)
    return result


def snapshot_rows(source: Connection) -> dict[str, list[dict[str, Any]]]:
    return {
        table.name: [dict(row) for row in source.execute(select(table)).mappings()]
        for table in transfer_tables(source)
    }


def assert_target_empty(target: Connection, metadata: MetaData = Base.metadata) -> None:
    for table in metadata.sorted_tables:
        if target.scalar(select(func.count()).select_from(table)):
            raise ValueError(f"Cloud target is not empty: {table.name}; no rows were overwritten")


def insert_snapshot(target: Connection, rows: Mapping[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Caller owns the transaction. Any insert/count/FK failure rolls it all back."""
    assert_target_empty(target)
    counts = {}
    for table in Base.metadata.sorted_tables:
        if table.name in EXCLUDED_TABLES or table.name not in rows:
            continue
        records = rows[table.name]
        records = [
            {
                key: (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC))
                if isinstance(value, datetime)
                else value
                for key, value in row.items()
            }
            for row in records
        ]
        # Small batches also keep psycopg's parameter count bounded for larger archives.
        for offset in range(0, len(records), 100):
            target.execute(table.insert(), records[offset : offset + 100])
        count = target.scalar(select(func.count()).select_from(table))
        if count != len(records):
            raise ValueError(f"Transferred row count differs: {table.name}")
        counts[table.name] = len(records)
    return counts


def media_files(bundle: Path, rows: Mapping[str, list[dict[str, Any]]]) -> dict[str, Path]:
    root = (bundle / "data/media").resolve()
    files = {}
    for row in rows.get("media_assets", []):
        key = row["storage_key"]
        if not isinstance(key, str) or not key or "\\" in key:
            raise ValueError("Invalid media object key in the source")
        path = (root / key).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("A media object referenced by the database is missing")
        files[key] = path
    return files


async def upload_media(storage: ObjectStorage, files: Mapping[str, Path]) -> int:
    """Retries may reuse identical objects but never overwrite different content."""
    for key, path in files.items():
        content = path.read_bytes()
        try:
            existing = await storage.read(key=key)
        except ObjectNotFoundError:
            await storage.put(
                key=key,
                content=content,
                content_type=mimetypes.guess_type(path.name)[0] or "image/webp",
            )
        else:
            if existing != content:
                raise ValueError("A different media object already exists at the target key")
        if await storage.read(key=key) != content:
            raise ValueError("Uploaded media verification failed")
    return len(files)


def lock_transfer(connection: Connection) -> None:
    """Share one lock with the cloud collector and hold it through the transfer."""
    if not connection.scalar(
        text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": TRANSFER_LOCK_ID}
    ):
        raise ValueError("Another cloud data operation is running; retry later")
