"""Deployment exports keep source accounts/data intact and reject altered bundles."""

import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from predatory_beavers.worker.sports_sync import ProcessLock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export_deployment.py"
spec = importlib.util.spec_from_file_location("export_deployment", SCRIPT)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


@pytest.fixture
def deployment_source(tmp_path):
    data = tmp_path / "source"
    data.mkdir()
    database = data / "local.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, password_hash TEXT);"
            "CREATE TABLE sessions (token_hash TEXT);"
            "INSERT INTO users VALUES (1, 'private-password-hash');"
            "INSERT INTO sessions VALUES ('private-login-session-token');"
            "CREATE TABLE matches (score INTEGER); INSERT INTO matches VALUES (76);"
        )
        connection.commit()
    media = data / "media/images"
    media.mkdir(parents=True)
    (media / "captain.webp").write_bytes(b"photo")
    (media / "credentials.json").write_text('{"password":"private"}', encoding="utf-8")
    (data / ".env").write_text("APP_FIBA_API_KEY=secret", encoding="utf-8")
    (data / "credentials.json").write_text("secret", encoding="utf-8")
    sync = data / "sports-sync"
    (sync / "working/docs").mkdir(parents=True)
    (sync / "roster-photos").mkdir()
    for name in ["status.json", "sources.json", "fiba-watchlist.json"]:
        (sync / name).write_text("{}", encoding="utf-8")
    (sync / "working/docs/asb-roster.json").write_text("{}", encoding="utf-8")
    (sync / "working/docs/results-asb.json").write_text("{}", encoding="utf-8")
    (sync / "working/docs/results-fiba3x3.json").write_text("{}", encoding="utf-8")
    (sync / "roster-photos/player.webp").write_bytes(b"portrait")
    (sync / "roster-photos/index.json").write_text("{}", encoding="utf-8")
    (sync / "roster-photos/.portrait.tmp.webp").write_text("tmp", encoding="utf-8")
    (sync / "roster-photos/portrait.tmp.webp").write_text("tmp", encoding="utf-8")
    (sync / "credentials.json").write_text("private", encoding="utf-8")
    (sync / "working/docs/credentials.json").write_text("private", encoding="utf-8")
    (sync / "request.log").write_text("private", encoding="utf-8")
    return database, data / "media", tmp_path / "bundle"


def test_export_preserves_live_database_and_passwords_and_removes_sessions(deployment_source):
    database, media, destination = deployment_source
    original = database.read_bytes()
    bundle = exporter.export_bundle(database, media, destination)
    assert database.read_bytes() == original
    with closing(sqlite3.connect(database)) as source:
        assert source.execute("SELECT * FROM sessions").fetchall() == [
            ("private-login-session-token",)
        ]
    with closing(sqlite3.connect(bundle / "data/local.db")) as copy:
        assert copy.execute("SELECT * FROM sessions").fetchall() == []
        assert copy.execute("SELECT * FROM users").fetchall() == [(1, "private-password-hash")]
        assert copy.execute("SELECT * FROM matches").fetchall() == [(76,)]
    assert b"private-login-session-token" not in (bundle / "data/local.db").read_bytes()
    assert exporter.verify_bundle(bundle)["sessions_removed"] is True


def test_export_includes_media_runtime_manifests_and_no_credentials(deployment_source):
    database, media, destination = deployment_source
    bundle = exporter.export_bundle(database, media, destination)
    paths = {record["path"] for record in exporter.verify_bundle(bundle)["files"]}
    assert paths == {
        "data/local.db",
        "data/media/images/captain.webp",
        "data/sports-sync/status.json",
        "data/sports-sync/sources.json",
        "data/sports-sync/fiba-watchlist.json",
        "data/sports-sync/working/docs/asb-roster.json",
        "data/sports-sync/working/docs/results-asb.json",
        "data/sports-sync/working/docs/results-fiba3x3.json",
        "data/sports-sync/roster-photos/player.webp",
        "data/sports-sync/roster-photos/index.json",
    }


def test_online_backup_includes_committed_wal_changes_without_checkpointing_source(
    deployment_source,
):
    database, media, destination = deployment_source
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO matches VALUES (81)")
        writer.commit()
        original, wal = database.read_bytes(), database.with_name("local.db-wal").read_bytes()
        bundle = exporter.export_bundle(database, media, destination)
        assert database.read_bytes() == original
        assert database.with_name("local.db-wal").read_bytes() == wal
        with closing(sqlite3.connect(bundle / "data/local.db")) as copied:
            assert copied.execute("SELECT * FROM matches").fetchall() == [(76,), (81,)]
        assert not list(bundle.rglob("*-wal"))
        assert not list(bundle.rglob("*-shm"))


def test_export_rejects_missing_media_before_creating_bundle(deployment_source):
    database, media, destination = deployment_source
    with pytest.raises(ValueError, match="media directory"):
        exporter.export_bundle(database, media.with_name("mistyped-media"), destination)
    assert not destination.exists()


def test_export_refuses_existing_destination_and_busy_sync(deployment_source):
    database, media, destination = deployment_source
    destination.mkdir()
    sentinel = destination / "keep"
    sentinel.write_text("untouched", encoding="utf-8")
    with pytest.raises(FileExistsError):
        exporter.export_bundle(database, media, destination)
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    lock = ProcessLock(database.with_name(database.name + ".sports-sync.lock"))
    assert lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="synchronization is running"):
            exporter.export_bundle(database, media, destination.with_name("other"))
    finally:
        lock.release()
    assert not destination.with_name("other").exists()


@pytest.mark.parametrize("change", ["changed", "missing", "extra"])
def test_verify_rejects_changed_missing_and_unlisted_files(deployment_source, change):
    database, media, destination = deployment_source
    bundle = exporter.export_bundle(database, media, destination)
    photo = bundle / "data/media/images/captain.webp"
    if change == "changed":
        photo.write_bytes(b"other")  # Same size: SHA-256 must catch this.
    elif change == "missing":
        photo.unlink()
    else:
        (bundle / ".env").write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError):
        exporter.verify_bundle(bundle)


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute", "C:/secret", "data/../secret"])
def test_verify_rejects_traversal_paths(deployment_source, unsafe):
    database, media, destination = deployment_source
    bundle = exporter.export_bundle(database, media, destination)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = unsafe
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        exporter.verify_bundle(bundle)


def test_verify_rejects_session_even_with_recomputed_hash(deployment_source):
    database, media, destination = deployment_source
    bundle = exporter.export_bundle(database, media, destination)
    copied_database = bundle / "data/local.db"
    with closing(sqlite3.connect(copied_database)) as connection:
        connection.execute("INSERT INTO sessions VALUES ('new-token')")
        connection.commit()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    record = next(record for record in manifest["files"] if record["path"] == "data/local.db")
    record["sha256"] = hashlib.sha256(copied_database.read_bytes()).hexdigest()
    record["size"] = copied_database.stat().st_size
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="login sessions"):
        exporter.verify_bundle(bundle)


def test_verify_rejects_links(deployment_source, tmp_path):
    database, media, destination = deployment_source
    bundle = exporter.export_bundle(database, media, destination)
    target = tmp_path / "external.webp"
    target.write_bytes(b"photo")
    photo = bundle / "data/media/images/captain.webp"
    photo.unlink()
    try:
        photo.symlink_to(target)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable on this Windows account")
    with pytest.raises(ValueError, match="links"):
        exporter.verify_bundle(bundle)
