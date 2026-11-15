"""Verify immutable SQLite transfer and private media; PostgreSQL tests are opt-in."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import DropSchema

from predatory_beavers.db.base import Base
from predatory_beavers.deployment.transfer import (
    EXCLUDED_TABLES,
    TRANSFER_LOCK_ID,
    assert_target_empty,
    insert_snapshot,
    lock_transfer,
    media_files,
    snapshot_rows,
    source_engine,
    target_engine,
    upload_media,
)
from predatory_beavers.integrations.object_storage.local import ObjectNotFoundError
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "import_cloud_bundle", ROOT / "scripts/import_cloud_bundle.py"
)
assert spec and spec.loader
cloud_cli = importlib.util.module_from_spec(spec)
# CLI imports its sibling export script; its absolute module path is intentional.
sys.path.insert(0, str(ROOT / "scripts"))
spec.loader.exec_module(cloud_cli)


class MemoryStorage:
    def __init__(self, objects: dict[str, bytes] | None = None, *, corrupt: bool = False) -> None:
        self.objects = dict(objects or {})
        self.corrupt = corrupt
        self.writes: list[str] = []

    async def read(self, *, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectNotFoundError(key) from None

    async def put(self, *, key: str, content: bytes, content_type: str) -> None:
        assert content_type == "image/webp"
        self.writes.append(key)
        self.objects[key] = b"corrupt" if self.corrupt else content

    async def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def checked_bundle() -> Path:
    configured = os.environ.get("TEST_DEPLOYMENT_BUNDLE")
    if not configured:
        pytest.skip("Set TEST_DEPLOYMENT_BUNDLE to a private exported bundle")
    bundle = Path(configured).absolute()
    cloud_cli.verify_bundle(bundle)
    return bundle


@pytest.fixture
def postgres_settings() -> Iterator[Settings]:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an isolated test PostgreSQL instance")
    schema = "transfer_test_" + uuid4().hex
    settings = Settings(
        _env_file=None,
        database_url=url,
        database_schema=schema,
        env="test",
        sports_sync_enabled=False,
    )
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", "skip_content_seed=true", "upgrade", "head"],
        cwd=ROOT,
        env=cloud_cli.settings_environment(settings),
        capture_output=True,
        timeout=180,
        check=False,
    )
    if migration.returncode:
        # A credential-free summary still identifies the cause in a local test failure.
        output = migration.stderr.decode("utf-8", errors="replace")
        if "ProactorEventLoop" in output:
            pytest.fail("PostgreSQL migrations require a Windows Selector event loop")
        pytest.fail("Migration of the isolated PostgreSQL schema failed")
    try:
        yield settings
    finally:
        admin = create_engine(settings.database_url)
        try:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            admin.dispose()


def _hashes(directory: Path) -> dict[str, str]:
    return {
        file.relative_to(directory).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in directory.rglob("*")
        if file.is_file()
    }


def _canonical(value):
    if isinstance(value, datetime):
        # SQLite has no timezone storage; imported values represent UTC in both adapters.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def test_checked_real_bundle_is_typed_and_preview_never_contacts_cloud(checked_bundle, monkeypatch):
    before = _hashes(checked_bundle)
    source = source_engine(checked_bundle / "data/local.db")
    try:
        with source.connect() as connection:
            rows = snapshot_rows(connection)
    finally:
        source.dispose()
    assert not set(rows) & EXCLUDED_TABLES
    assert rows["teams"] and rows["players"] and rows["matches"] and rows["media_assets"]
    assert isinstance(rows["teams"][0]["id"], UUID)
    assert isinstance(rows["teams"][0]["active"], bool)
    assert isinstance(rows["matches"][0]["starts_at"], datetime)
    files = media_files(checked_bundle, rows)
    assert len(files) == len(rows["media_assets"])

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview must not migrate, upload, or contact PostgreSQL")

    monkeypatch.setattr(cloud_cli, "target_engine", forbidden)
    monkeypatch.setattr(cloud_cli.subprocess, "run", forbidden)
    import asyncio

    report = asyncio.run(
        cloud_cli.run_transfer(checked_bundle, Settings(_env_file=None), apply=False)
    )
    assert report["mode"] == "bundle_check_only"
    assert report["sessions_transferred"] == 0
    assert report["media_objects"] == len(files)
    assert _hashes(checked_bundle) == before


def test_real_bundle_roundtrips_every_typed_value_into_migrated_postgres(
    checked_bundle, postgres_settings
):
    before = _hashes(checked_bundle)
    source = source_engine(checked_bundle / "data/local.db")
    try:
        with source.connect() as connection:
            rows = snapshot_rows(connection)
    finally:
        source.dispose()
    target = target_engine(postgres_settings)
    try:
        with target.begin() as connection:
            # Other integration suites may migrate a separate schema on this same test DB.
            connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": TRANSFER_LOCK_ID})
            lock_transfer(connection)
            connection.execute(text("SET LOCAL TIME ZONE 'Europe/Moscow'"))
            assert_target_empty(connection)
            counts = insert_snapshot(connection, rows)
        assert counts == {name: len(records) for name, records in rows.items()}
        with target.connect() as connection:
            for table in Base.metadata.sorted_tables:
                actual = [dict(row) for row in connection.execute(select(table)).mappings()]
                if table.name in EXCLUDED_TABLES:
                    assert actual == []
                    continue
                expected = rows.get(table.name, [])

                def key(row, columns=tuple(table.primary_key.columns)):
                    return tuple(str(row[column.name]) for column in columns)

                if [_canonical(row) for row in sorted(actual, key=key)] != [
                    _canonical(row) for row in sorted(expected, key=key)
                ]:
                    pytest.fail(f"Typed values changed during transfer: {table.name}")
            assert connection.scalar(text("SELECT version_num FROM alembic_version"))
        with pytest.raises(ValueError, match="not empty"):
            with target.begin() as connection:
                insert_snapshot(connection, rows)
        with target.connect() as connection:
            for name, count in counts.items():
                assert (
                    connection.scalar(select(func.count()).select_from(Base.metadata.tables[name]))
                    == count
                )
    finally:
        target.dispose()
    assert _hashes(checked_bundle) == before


@pytest.mark.asyncio
async def test_upload_media_is_retryable_only_for_identical_bytes(tmp_path):
    photo = tmp_path / "captain.webp"
    photo.write_bytes(b"photo")
    storage = MemoryStorage()
    assert await upload_media(storage, {"images/captain.webp": photo}) == 1
    assert await upload_media(storage, {"images/captain.webp": photo}) == 1
    assert storage.writes == ["images/captain.webp"]
    storage.objects["images/captain.webp"] = b"different"
    with pytest.raises(ValueError, match="different media object"):
        await upload_media(storage, {"images/captain.webp": photo})
    assert storage.objects["images/captain.webp"] == b"different"


@pytest.mark.asyncio
async def test_upload_media_verifies_content_after_upload(tmp_path):
    photo = tmp_path / "captain.webp"
    photo.write_bytes(b"photo")
    with pytest.raises(ValueError, match="verification failed"):
        await upload_media(MemoryStorage(corrupt=True), {"images/captain.webp": photo})


def test_media_files_rejects_missing_files_and_escaping_keys(tmp_path):
    (tmp_path / "data/media").mkdir(parents=True)
    for key in ["../secret.webp", "images/missing.webp", "images\\photo.webp", ""]:
        with pytest.raises(ValueError):
            media_files(tmp_path, {"media_assets": [{"storage_key": key}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["sqlite", "public_schema", "local_storage"])
async def test_incomplete_cloud_config_never_migrates_or_reads_local_data(
    unsafe, tmp_path, monkeypatch
):
    settings = Settings(
        _env_file=None,
        env="test",
        database_url=(
            "sqlite+aiosqlite:///./data/local.db"
            if unsafe == "sqlite"
            else "postgresql+psycopg://test:fake@localhost/test"
        ),
        database_schema="public" if unsafe == "public_schema" else "beavers",
        media_storage_backend="local" if unsafe == "local_storage" else "supabase",
        supabase_url="https://guard-test.supabase.co",
        supabase_secret_key="sb_secret_guard_test_only",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Unsafe cloud configuration must fail before any data operation")

    monkeypatch.setattr(cloud_cli, "verify_bundle", forbidden)
    monkeypatch.setattr(cloud_cli.subprocess, "run", forbidden)
    monkeypatch.setattr(cloud_cli, "target_engine", forbidden)
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        await cloud_cli.run_transfer(tmp_path / "missing-bundle", settings, apply=True)


def test_unknown_source_tables_rejected_without_mutation(tmp_path):
    database = tmp_path / "source.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE unknown_content (id INTEGER)")
        connection.commit()
    before = database.read_bytes()
    source = source_engine(database)
    try:
        with source.connect() as connection:
            with pytest.raises(ValueError, match="unknown"):
                snapshot_rows(connection)
    finally:
        source.dispose()
    assert database.read_bytes() == before


def test_snapshot_explicitly_excludes_login_sessions_and_request_replay_keys(tmp_path):
    database = tmp_path / "source.db"
    source = create_engine("sqlite+pysqlite:///" + database.as_posix())
    try:
        with source.begin() as connection:
            for name in ["users", "sessions", "admin_creation_keys"]:
                Base.metadata.tables[name].create(connection)
            user_id, now = uuid4(), datetime.now(UTC)
            connection.execute(
                Base.metadata.tables["users"].insert(),
                {
                    "id": user_id,
                    "username": "editor",
                    "email": "editor@example.test",
                    "password_hash": "retained-account-password",
                    "role": "ADMIN",
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                Base.metadata.tables["sessions"].insert(),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "token_hash": "old-token",
                    "csrf_token_hash": "old-csrf",
                    "created_at": now,
                    "expires_at": now,
                    "last_seen_at": now,
                },
            )
            connection.execute(
                Base.metadata.tables["admin_creation_keys"].insert(),
                {
                    "actor_id": user_id,
                    "resource": "players",
                    "key": uuid4(),
                    "request_hash": "old-request",
                    "entity_id": uuid4(),
                    "created_at": now,
                },
            )
    finally:
        source.dispose()
    before = database.read_bytes()
    source = source_engine(database)
    try:
        with source.connect() as connection:
            rows = snapshot_rows(connection)
    finally:
        source.dispose()
    assert set(rows) == {"users"}
    assert rows["users"][0]["password_hash"] == "retained-account-password"
    assert database.read_bytes() == before


def test_insert_failure_rolls_back_all_prior_tables(postgres_settings):
    target = target_engine(postgres_settings)
    user = {
        "id": uuid4(),
        "username": "test-user",
        "email": "test@example.test",
        "password_hash": "retained-password-hash",
        "role": "ADMIN",
        "is_active": True,
        "first_name": None,
        "last_name": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    try:
        # A missing FK fails after the initial user insert. No partial site may remain.
        with pytest.raises(IntegrityError):
            with target.begin() as connection:
                insert_snapshot(
                    connection,
                    {
                        "users": [user],
                        "asb_roster_members": [
                            {
                                "id": uuid4(),
                                "snapshot_id": uuid4(),
                                "external_player_id": "missing",
                                "full_name": "Missing snapshot",
                                "is_listed": True,
                            }
                        ],
                    },
                )
        with target.connect() as connection:
            assert_target_empty(connection)
    finally:
        target.dispose()


def test_full_apply_uses_mocked_private_storage_and_rejects_nonempty_retry(
    checked_bundle, postgres_settings, monkeypatch
):
    before = _hashes(checked_bundle)
    objects, requests = {}, []
    bucket_exists = False

    def handle(request):
        nonlocal bucket_exists
        requests.append(request)
        assert request.url.host == "transfer-test.supabase.co"
        assert request.headers["apikey"] == "sb_secret_transfer_test_only"
        if request.url.path == "/storage/v1/bucket/club-media":
            return httpx.Response(
                200 if bucket_exists else 404,
                json={"id": "club-media", "public": False},
            )
        if request.url.path == "/storage/v1/bucket":
            assert request.method == "POST"
            assert json.loads(request.content)["public"] is False
            bucket_exists = True
            return httpx.Response(200, json={"name": "club-media"})
        key = request.url.path.split("club-media/", 1)[1]
        if request.method == "POST":
            objects[key] = request.content
            return httpx.Response(200, json={})
        assert "/object/authenticated/" in request.url.path
        return httpx.Response(200, content=objects[key]) if key in objects else httpx.Response(404)

    actual_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return actual_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(cloud_cli.httpx, "AsyncClient", client_factory)
    settings = Settings(
        _env_file=None,
        database_url=postgres_settings.database_url,
        database_schema=postgres_settings.database_schema,
        env="test",
        sports_sync_enabled=False,
        media_storage_backend="supabase",
        supabase_url="https://transfer-test.supabase.co",
        supabase_secret_key="sb_secret_transfer_test_only",
    )
    report = asyncio.run(cloud_cli.run_transfer(checked_bundle, settings, apply=True))
    assert report["mode"] == "applied"
    assert report["sessions_transferred"] == 0
    assert len([key for key in objects if key.startswith("images/")]) == report["media_objects"]
    assert any(key.startswith("runtime/portraits/") for key in objects)
    target = target_engine(settings)
    try:
        with target.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(Base.metadata.tables["sessions"])
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count()).select_from(Base.metadata.tables["cloud_sync_state"])
                )
                > 0
            )
        before_retry = len(requests)
        with pytest.raises(ValueError, match="not empty"):
            asyncio.run(cloud_cli.run_transfer(checked_bundle, settings, apply=True))
        assert len(requests) == before_retry
    finally:
        target.dispose()
    assert _hashes(checked_bundle) == before
