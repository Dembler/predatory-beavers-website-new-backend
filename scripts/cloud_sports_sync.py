"""Run source jobs outside Render, merging only guarded sports changes into SQL.

The short-lived SQLite staging database contains no users, sessions, profiles,
content, or credentials. It is never uploaded or committed. No cloud rows are
deleted. Each source's manifest and report commit together with its guarded diff.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import and_, create_engine, insert, select, text, update
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from predatory_beavers.db.base import Base  # noqa: E402
from predatory_beavers.db.session import database_connect_args  # noqa: E402
from predatory_beavers.integrations.object_storage.local import LocalObjectStorage  # noqa: E402
from predatory_beavers.integrations.object_storage.supabase import (  # noqa: E402
    SupabaseObjectStorage,
)
from predatory_beavers.modules.audit.models import AdminAuditLog  # noqa: E402
from predatory_beavers.modules.auth import models as auth_models  # noqa: E402,F401
from predatory_beavers.modules.club.models import MediaAsset, Team  # noqa: E402
from predatory_beavers.modules.imports.models import (  # noqa: E402
    CloudSportsState,
    MatchSourceSnapshot,
)
from predatory_beavers.modules.matches.models import Competition, Match, Venue  # noqa: E402
from predatory_beavers.modules.results.models import ArchiveCoverage, ArchiveResult  # noqa: E402
from predatory_beavers.modules.roster.models import (  # noqa: E402
    AsbRosterMember,
    AsbRosterSnapshot,
)
from predatory_beavers.modules.standings.models import StandingsSnapshot  # noqa: E402
from predatory_beavers.settings import Settings  # noqa: E402
from predatory_beavers.worker.cloud_state import (  # noqa: E402
    MANIFESTS,
    PORTRAIT_PREFIX,
    portrait_entries,
)

# Order respects the FK graph. Media and teams are read-only parent references.
MODELS = (
    MediaAsset,
    Team,
    Competition,
    Venue,
    Match,
    MatchSourceSnapshot,
    ArchiveResult,
    ArchiveCoverage,
    AsbRosterSnapshot,
    AsbRosterMember,
    StandingsSnapshot,
)
TABLES = {model.__table__.name: model.__table__ for model in MODELS}
WRITABLE = frozenset(TABLES) - {"teams", "media_assets"}
LOCK_ID = 739_412_601


class SyncConflict(RuntimeError):
    """A cloud row changed after collection started; retry with a fresh snapshot."""


def canonical(value: Any) -> Any:
    if isinstance(value, UUID):
        return value.hex
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(item) for item in value]
    return value


def row_key(table: Any, row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(canonical(row[column.name]) for column in table.primary_key)


def sql_values(row: dict[str, Any]) -> dict[str, Any]:
    # SQLite staging returns naive DateTime values which always represent UTC.
    # Send aware UTC to PostgreSQL, independently of the session's TimeZone.
    return {
        key: value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
        if isinstance(value, datetime)
        else value
        for key, value in row.items()
    }


def read_rows(connection: Connection) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    return {
        name: {
            row_key(table, dict(row)): dict(row)
            for row in connection.execute(select(table)).mappings()
        }
        for name, table in TABLES.items()
    }


def state_get(connection: Connection, key: str) -> dict[str, Any]:
    value = connection.scalar(select(CloudSportsState.value).where(CloudSportsState.key == key))
    return value if isinstance(value, dict) else {}


def state_put(connection: Connection, key: str, value: dict[str, Any]) -> None:
    values = {"value": value, "updated_at": datetime.now(UTC)}
    if connection.scalar(select(CloudSportsState.key).where(CloudSportsState.key == key)):
        connection.execute(
            update(CloudSportsState).where(CloudSportsState.key == key).values(**values)
        )
    else:
        connection.execute(insert(CloudSportsState).values(key=key, **values))


def create_stage(directory: Path, rows: dict[str, Any]) -> tuple[Path, Engine]:
    database = directory / "stage.sqlite"
    engine = create_engine("sqlite:///" + database.as_posix())
    # All ORM tables are created only to resolve FKs; only the explicit public
    # sports table allowlist receives rows. Empty auth tables stay empty.
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for name, table in TABLES.items():
            values = [sql_values(row) for row in rows[name].values()]
            if values:
                connection.execute(insert(table), values)
    return database, engine


def prepare_runtime(database: Path, documents: dict[str, dict[str, Any]]) -> Path:
    from sports_refresh_runtime import atomic_json, working_tree

    runtime = working_tree(database)
    for name, document in documents.items():
        target = (
            database.parent / "sports-sync/fiba-watchlist.json"
            if name == "fiba-watchlist.json"
            else runtime / "docs" / name
        )
        atomic_json(target, document)
    return runtime


def merge_diff(
    connection: Connection,
    before: dict[str, Any],
    after: dict[str, Any],
    audits: list[dict[str, Any]] | None = None,
    *,
    provider: str = "asb",
) -> dict[str, int]:
    """Lock, compare the whole old row, and update changed columns only.

    A deletion or edit anywhere in this source's changed set aborts the entire
    source transaction, including its new provider snapshots and manifest.
    """
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for name, table in TABLES.items():
        original, candidate = before[name], after[name]
        if original.keys() - candidate.keys():
            raise ValueError("Sports jobs must never physically delete SQL rows")
        for key, new in candidate.items():
            old = original.get(key)
            if old is not None and canonical(old) == canonical(new):
                continue
            if name not in WRITABLE:
                raise ValueError("Sports jobs cannot modify editorial parent rows")
            source_field = "provider" if name == "archive_coverage" else "source"
            if source_field in new and new[source_field] != provider:
                raise ValueError("A source cannot change another provider's rows")
            if name.startswith("asb_roster_") and provider != "asb":
                raise ValueError("FIBA cannot change ASB roster rows")
            if old is not None and source_field in old and old[source_field] != provider:
                raise ValueError("A source cannot adopt an editorial row")
            predicate = and_(*(column == new[column.name] for column in table.primary_key))
            current = (
                connection.execute(select(table).where(predicate).with_for_update())
                .mappings()
                .first()
            )
            if old is None:
                if current is not None:
                    if canonical(dict(current)) == canonical(new):
                        counts["unchanged"] += 1
                        continue
                    raise SyncConflict("Concurrent source identity exists")
                connection.execute(insert(table).values(**sql_values(new)))
                counts["inserted"] += 1
            else:
                if current is None or canonical(dict(current)) != canonical(old):
                    raise SyncConflict("Concurrent editor change; source transaction preserved")
                changes = {
                    key: value
                    for key, value in new.items()
                    if canonical(value) != canonical(old[key])
                }
                connection.execute(update(table).where(predicate).values(**sql_values(changes)))
                counts["updated"] += 1
    for row in audits or []:
        if row.get("actor_id") is not None or row.get("actor_username") != "asb-standings-sync":
            raise ValueError("Invalid source audit")
        connection.execute(insert(AdminAuditLog).values(**sql_values(row)))
    return counts


def source_jobs(group: str) -> list[tuple[str, str | None, str | None]]:
    jobs = []
    if group in {"all", "asb"}:
        jobs += [
            (f"asb_{kind}_{team}", kind, team)
            for kind in ("games", "standings", "roster")
            for team in ("men", "women")
        ]
        jobs.append(("asb_portraits", "portraits", None))
    if group in {"all", "fiba"}:
        jobs.append(("fiba3x3", "fiba", None))
    return jobs


def execute_source(
    database: Path, runtime: Path, kind: str, team: str | None, settings: Settings
) -> dict[str, Any]:
    import sports_refresh_runtime as jobs

    if kind == "fiba":
        return jobs.sync_fiba(database, runtime, preview=False, api_key=settings.fiba_api_key or "")
    if kind == "portraits":
        return jobs.sync_portraits(database, runtime, preview=False)
    function = {
        "games": jobs.sync_games,
        "standings": jobs.sync_standings,
        "roster": jobs.sync_roster,
    }[kind]
    return function(database, runtime, team=team, preview=False)


async def portrait_objects(
    settings: Settings,
    directory: Path,
    *,
    upload: bool,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        if settings.media_storage_backend == "supabase":
            assert settings.supabase_url and settings.supabase_secret_key
            storage = SupabaseObjectStorage(
                settings.supabase_url,
                settings.supabase_secret_key.get_secret_value(),
                settings.supabase_storage_bucket,
                client=client,
            )
        else:
            storage = LocalObjectStorage(settings.media_storage_path)
        if not upload:
            from predatory_beavers.worker.cloud_state import replace_bytes

            document = portrait_entries(previous or {})
            for key, entry in document.items():
                content = await storage.read(key=PORTRAIT_PREFIX + entry["sha256"] + ".webp")
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise ValueError("Invalid stored portrait")
                replace_bytes(directory / f"{key}.webp", content)
            replace_bytes(directory / "index.json", json.dumps(document).encode())
            return document
        document = portrait_entries(json.loads((directory / "index.json").read_text()))
        if upload:
            stored_hashes = {entry["sha256"] for entry in portrait_entries(previous or {}).values()}
            for key, entry in document.items():
                content = (directory / f"{key}.webp").read_bytes()
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise ValueError("Invalid collected portrait")
                if entry["sha256"] in stored_hashes:
                    continue
                await storage.put(
                    key=PORTRAIT_PREFIX + entry["sha256"] + ".webp",
                    content=content,
                    content_type="image/webp",
                )
        return document


def report_summary(sources: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    states = [row.get("status") for row in sources.values()]
    status = (
        "succeeded"
        if states and all(row == "succeeded" for row in states)
        else ("partial" if "succeeded" in states else "failed")
    )
    finished = datetime.now(UTC).isoformat()
    upcoming = [row["next_attempt_at"] for row in sources.values() if row.get("next_attempt_at")]
    return {
        "state": status,
        "sources": sources,
        "attempt_started_at": previous.get("attempt_started_at"),
        "attempt_finished_at": finished,
        "last_success_at": finished if status == "succeeded" else previous.get("last_success_at"),
        "last_error": None
        if status == "succeeded"
        else "Some official sources could not be refreshed",
        "next_attempt_at": min(upcoming) if upcoming else None,
    }


def run_cloud(
    settings: Settings, engine: Engine, *, group: str, force: bool = False
) -> dict[str, Any]:
    # A dedicated connection holds a SESSION advisory lock across short SQL
    # transactions and network collection. Supabase session/direct DSN required.
    with engine.connect() as lock:
        postgres = engine.dialect.name == "postgresql"
        if postgres and not lock.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}
        ):
            return {"state": "skipped", "reason": "another_refresh_is_running"}
        lock.commit()
        try:
            with engine.begin() as connection:
                previous = state_get(connection, "status")
                sources = previous.get("sources") or {}
            report = {**previous, "attempt_started_at": datetime.now(UTC).isoformat()}
            for name, kind, team in source_jobs(group):
                old = sources.get(name, {})
                now = datetime.now(UTC)
                interval = (
                    settings.sports_sync_fiba_interval_seconds
                    if kind == "fiba"
                    else settings.sports_sync_interval_seconds
                )
                if (
                    not force
                    and old.get("next_attempt_at")
                    and datetime.fromisoformat(old["next_attempt_at"]) > now
                ):
                    continue
                state = {
                    "started_at": now.isoformat(),
                    "last_success_at": old.get("last_success_at"),
                    "interval_seconds": interval,
                }
                with engine.begin() as connection:
                    state_put(
                        connection,
                        "status",
                        {
                            **report,
                            "state": "running",
                            "sources": {**sources, name: {**state, "status": "running"}},
                        },
                    )
                try:
                    # Each source gets a FRESH cloud snapshot, with no long SQL
                    # transaction held open while a third-party site responds.
                    with engine.connect() as connection:
                        if postgres:
                            connection = connection.execution_options(
                                isolation_level="REPEATABLE READ"
                            )
                        with connection.begin():
                            before = read_rows(connection)
                            documents = {
                                filename: state_get(connection, "document/" + filename)
                                for filename in MANIFESTS
                            }
                            old_portraits = state_get(connection, "portraits")
                    documents = {key: value for key, value in documents.items() if value}
                    with tempfile.TemporaryDirectory(prefix="beavers-cloud-sync-") as temporary:
                        database, staging = create_stage(Path(temporary), before)
                        try:
                            runtime = prepare_runtime(database, documents)
                            if kind == "portraits" and old_portraits:
                                asyncio.run(
                                    portrait_objects(
                                        settings,
                                        database.parent / "sports-sync/roster-photos",
                                        upload=False,
                                        previous=old_portraits,
                                    )
                                )
                            result = execute_source(database, runtime, kind, team, settings)
                            status = result.get("status", "succeeded")
                            if status in {"ok", "success", "imported"}:
                                status = "succeeded"
                            with staging.connect() as connection:
                                after = read_rows(connection)
                                audits = [
                                    dict(row)
                                    for row in connection.execute(
                                        select(AdminAuditLog.__table__)
                                    ).mappings()
                                ]
                            from sports_refresh_runtime import load_json

                            changed_files = {
                                "games": "results-asb.json",
                                "standings": "asb-standings.json",
                                "roster": "asb-roster.json",
                                "fiba": "results-fiba3x3.json",
                            }
                            updated_documents = {
                                filename: load_json(runtime / "docs" / filename)
                                for filename in (
                                    [changed_files[kind]] if kind in changed_files else []
                                )
                            }
                            portraits = (
                                asyncio.run(
                                    portrait_objects(
                                        settings,
                                        database.parent / "sports-sync/roster-photos",
                                        upload=True,
                                        previous=old_portraits,
                                    )
                                )
                                if kind == "portraits"
                                else None
                            )
                            with engine.begin() as connection:
                                counts = merge_diff(
                                    connection,
                                    before,
                                    after,
                                    audits,
                                    provider="fiba3x3" if kind == "fiba" else "asb",
                                )
                                for filename, document in updated_documents.items():
                                    if document != documents.get(filename):
                                        state_put(connection, "document/" + filename, document)
                                if portraits is not None:
                                    state_put(connection, "portraits", portraits)
                                state.update(status=status, result={"merge": counts})
                                if status == "succeeded":
                                    state["last_success_at"] = datetime.now(UTC).isoformat()
                                state["finished_at"] = datetime.now(UTC).isoformat()
                                state["next_attempt_at"] = (
                                    now + timedelta(seconds=interval)
                                ).isoformat()
                                sources[name] = state
                                state_put(connection, "status", report_summary(sources, report))
                        finally:
                            staging.dispose()
                except Exception as exc:
                    # Neither URLs, bodies, parameters, nor tracebacks may carry
                    # private DB/Storage/FIBA values into Actions logs or the UI.
                    state.update(
                        status="failed",
                        error=type(exc).__name__,
                        finished_at=datetime.now(UTC).isoformat(),
                        next_attempt_at=(now + timedelta(seconds=interval)).isoformat(),
                    )
                    sources[name] = state
                    with engine.begin() as connection:
                        state_put(connection, "status", report_summary(sources, report))
            final_report = report_summary(sources, report)
            with engine.begin() as connection:
                state_put(connection, "status", final_report)
            return final_report
        finally:
            if postgres:
                lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})
                lock.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["all", "asb", "fiba"], default="all")
    parser.add_argument("--force", action="store_true", help="Run despite the saved due time")
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings(_env_file=None)
        url = make_url(settings.database_url)
        if url.get_backend_name() != "postgresql":
            raise ValueError("Cloud refresh requires PostgreSQL")
        if url.port == 6543:
            raise ValueError("Cloud refresh requires a direct or session-pooler connection")
        # No local .env or working DB is consulted. A separate Actions secret is
        # mandatory; no hard-coded DSN or fallback can connect to an unintended DB.
        if not os.environ.get("APP_DATABASE_URL"):
            raise ValueError("APP_DATABASE_URL must be supplied by the scheduler")
        engine = create_engine(
            url,
            poolclass=NullPool,
            connect_args=database_connect_args(settings),
            hide_parameters=True,
        )
        report = run_cloud(settings, engine, group=args.source, force=args.force)
        print(json.dumps(report, ensure_ascii=True))
        if report["state"] == "failed":
            raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
