"""Guarded cloud merges use isolated SQL fixtures and no live source/Storage calls."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, insert, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema, DropSchema

from predatory_beavers.db.base import Base
from predatory_beavers.db.session import database_connect_args
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.imports.models import MatchSourceSnapshot
from predatory_beavers.modules.matches.models import Match
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.roster.photos import _cached_photo_in
from predatory_beavers.settings import Settings
from predatory_beavers.worker import cloud_state

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "cloud_sports_sync", ROOT / "scripts/cloud_sports_sync.py"
)
assert spec and spec.loader
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)

import import_public_results as importer  # noqa: E402
import sports_refresh_runtime as jobs  # noqa: E402


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request, tmp_path: Path) -> Iterator[Engine]:
    if request.param == "postgresql":
        configured = os.environ.get("PB_TEST_POSTGRES_URL")
        if not configured:
            pytest.skip("Set PB_TEST_POSTGRES_URL to an isolated PostgreSQL instance")
        schema = "sports_test_" + uuid4().hex
        settings = Settings(_env_file=None, database_url=configured, database_schema=schema)
        admin = create_engine(settings.database_url)
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        connect_args = database_connect_args(settings)
        connect_args["options"] += " -cTimeZone=Europe/Moscow"
        database = create_engine(settings.database_url, connect_args=connect_args)
    else:
        schema, admin = None, None
        database = create_engine("sqlite:///" + (tmp_path / "cloud.sqlite").as_posix())
    Base.metadata.create_all(database)
    try:
        yield database
    finally:
        database.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin.dispose()


def fixture_game(score: int = 73) -> dict:
    return {
        "GameID": 123456789,
        "GameDate": "14.11.2026",
        "HasTime": True,
        "GameTimeMsk": "15:30",
        "GameStatus": 1,
        "official_status": "ResultConfirmed",
        "TeamAid": 7433,
        "TeamBid": 7432,
        "TeamNameAru": "Club",
        "TeamNameBru": "Opponent",
        "ScoreA": score,
        "ScoreB": 69,
        "TeamLogoA": None,
        "TeamLogoB": None,
        "ArenaId": 12371,
        "ArenaRu": "Club gym",
        "CompNameRu": "Regional stage",
        "LeagueNameRu": "Regional league",
        "team": "men",
        "external_team_id": "7433",
        "season": "2026-2027",
        "season_comp_id": "999",
        "source_url": "https://asb.sportoteka.org/api/abc/teams/games",
    }


def seed_team(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(insert(Team).values(slug="men", name="Club", category=TeamCategory.MEN))


def stage_game(engine: Engine, directory: Path, score: int = 73):
    directory.mkdir(parents=True, exist_ok=True)
    with engine.connect() as connection:
        before = cloud.read_rows(connection)
    database, staging = cloud.create_stage(directory, before)
    game = fixture_game(score)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with staging.connect() as connection:
        after = cloud.read_rows(connection)
    staging.dispose()
    return before, after


def test_real_sqlite_import_preserves_typed_values_and_editorial_result(engine, tmp_path):
    seed_team(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                username="private-editor",
                email="private@example.test",
                password_hash="never-stage-this",
                role=UserRole.ADMIN,
            )
        )
    before, after = stage_game(engine, tmp_path / "first")
    assert "users" not in before and "sessions" not in before and "content_blocks" not in before
    with engine.begin() as connection:
        cloud.merge_diff(connection, before, after)
        cloud.state_put(connection, "document/results-asb.json", {"games": [fixture_game()]})
    with engine.begin() as connection:
        match = connection.execute(select(Match.__table__)).mappings().one()
        assert match["home_score"] == 73 and match["active"] is True
        assert isinstance(match["starts_at"], datetime)
        assert cloud.canonical(match["starts_at"]) == "2026-11-14T12:30:00+00:00"
        # An editor owns the result bundle after this edit. A later provider
        # correction advances its snapshot without replacing this result.
        connection.execute(update(Match).values(notes="Editor match report", version_id=2))
    before, after = stage_game(engine, tmp_path / "second", score=81)
    with engine.begin() as connection:
        cloud.merge_diff(connection, before, after)
    with engine.connect() as connection:
        match = connection.execute(select(Match.__table__)).mappings().one()
        source = connection.scalar(select(MatchSourceSnapshot.values))
        assert match["notes"] == "Editor match report" and match["home_score"] == 73
        assert match["version_id"] == 2
        assert source["home_score"] == 81
        assert connection.scalar(select(func.count()).select_from(User)) == 1
    # Actual staging tables may contain empty auth metadata, never auth rows.
    (tmp_path / "privacy").mkdir()
    database, staging = cloud.create_stage(tmp_path / "privacy", before)
    try:
        with staging.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(User)) == 0
            assert connection.scalar(text("SELECT count(*) FROM sessions")) == 0
    finally:
        staging.dispose()


@pytest.mark.parametrize("concurrent", ["edit", "delete"])
def test_concurrent_editor_change_rolls_back_entire_source_and_manifest(
    engine, tmp_path, concurrent
):
    seed_team(engine)
    before, after = stage_game(engine, tmp_path / "initial")
    with engine.begin() as connection:
        cloud.merge_diff(connection, before, after)
        cloud.state_put(connection, "document/results-asb.json", {"last_good": True})
    before, after = stage_game(engine, tmp_path / "candidate", score=80)
    # Add a new source row before the conflict to prove the SQL transaction
    # rolls it back too, rather than committing a partially refreshed source.
    match_key = next(iter(after["matches"]))
    new_match = copy.deepcopy(after["matches"][match_key])
    new_match.update(id=uuid4(), external_id="new-before-conflict")
    after["matches"] = {cloud.row_key(Match.__table__, new_match): new_match, **after["matches"]}
    with engine.begin() as connection:
        if concurrent == "edit":
            connection.execute(update(Match).values(notes="Concurrent edit", version_id=9))
        else:
            connection.execute(delete(MatchSourceSnapshot))
            connection.execute(delete(Match))
    with pytest.raises(cloud.SyncConflict), engine.begin() as connection:
        cloud.merge_diff(connection, before, after)
        cloud.state_put(connection, "document/results-asb.json", {"must_not_commit": True})
    with engine.connect() as connection:
        assert cloud.state_get(connection, "document/results-asb.json") == {"last_good": True}
        assert connection.scalar(select(func.count()).select_from(Match)) == (concurrent == "edit")
        assert connection.scalar(select(ArchiveResult.club_score)) == 73


@pytest.mark.parametrize("invalid", ["delete", "team", "provider"])
def test_merge_rejects_deletions_editorial_parents_and_other_providers(engine, tmp_path, invalid):
    seed_team(engine)
    before, after = stage_game(engine, tmp_path / "reject")
    if invalid == "delete":
        after["teams"] = {}
    elif invalid == "team":
        next(iter(after["teams"].values()))["name"] = "Replace editor club name"
    else:
        next(iter(after["matches"].values()))["source"] = "manual"
    with pytest.raises(ValueError), engine.begin() as connection:
        cloud.merge_diff(connection, before, after)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Match)) == 0


def test_source_failure_isolation_last_good_due_times_and_no_secret_logging(
    engine, tmp_path, monkeypatch
):
    settings = Settings(
        _env_file=None,
        env="test",
        sports_sync_interval_seconds=7200,
        sports_sync_fiba_interval_seconds=172800,
    )
    last_success = "2026-01-01T00:00:00+00:00"
    with engine.begin() as connection:
        cloud.state_put(connection, "document/results-asb.json", {"games": [], "last_good": True})
        cloud.state_put(
            connection,
            "status",
            {
                "sources": {
                    "asb_games_men": {"last_success_at": last_success, "status": "succeeded"},
                },
                "last_success_at": last_success,
            },
        )
    monkeypatch.setattr(
        cloud,
        "source_jobs",
        lambda _: [
            ("asb_games_men", "games", "men"),
            ("asb_games_women", "games", "women"),
            ("fiba3x3", "fiba", None),
        ],
    )
    calls = []

    def execute(database, runtime, kind, team, settings):
        calls.append((kind, team))
        if team == "men":
            jobs.atomic_json(runtime / "docs/results-asb.json", {"must_not_save": True})
            raise RuntimeError("SECRET-URL-PASSWORD-MUST-NOT-BE-REPORTED")
        if kind == "fiba":
            return {"status": "auth_required"}
        jobs.atomic_json(runtime / "docs/results-asb.json", {"games": [], "women_refreshed": True})
        return {"status": "ok"}

    monkeypatch.setattr(cloud, "execute_source", execute)
    report = cloud.run_cloud(settings, engine, group="all", force=True)
    assert report["state"] == "partial" and len(calls) == 3
    men, women, fiba = (
        report["sources"][key] for key in ["asb_games_men", "asb_games_women", "fiba3x3"]
    )
    assert men["status"] == "failed" and men["last_success_at"] == last_success
    assert women["status"] == "succeeded" and women["last_success_at"]
    assert fiba["status"] == "auth_required" and fiba["last_success_at"] is None
    assert men["interval_seconds"] == 7200 and fiba["interval_seconds"] == 172800
    assert "SECRET-URL" not in json.dumps(report)
    with engine.connect() as connection:
        assert cloud.state_get(connection, "status") == report
        assert cloud.state_get(connection, "document/results-asb.json")["women_refreshed"]
    calls.clear()
    cloud.run_cloud(settings, engine, group="all")
    assert calls == []


@pytest.mark.parametrize("scope", ["session", "transaction"])
def test_postgres_lock_skips_without_touching_status(engine, scope):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory locks only")
    with engine.connect() as held:
        function = "pg_advisory_lock" if scope == "session" else "pg_advisory_xact_lock"
        held.execute(text(f"SELECT {function}(:key)"), {"key": cloud.LOCK_ID})
        if scope == "session":
            held.commit()
        try:
            report = cloud.run_cloud(Settings(_env_file=None), engine, group="all", force=True)
            assert report == {"state": "skipped", "reason": "another_refresh_is_running"}
        finally:
            if scope == "session":
                held.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": cloud.LOCK_ID})
            held.commit()


def test_real_bundle_runs_asb_job_staging_and_guarded_cloud_commit(engine, tmp_path, monkeypatch):
    configured = os.environ.get("TEST_DEPLOYMENT_BUNDLE")
    if not configured:
        pytest.skip("Set TEST_DEPLOYMENT_BUNDLE to an immutable private export")
    from predatory_beavers.deployment.transfer import source_engine

    bundle = Path(configured).absolute()
    snapshot = bundle / "data/local.db"
    checksum = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    source = source_engine(snapshot)
    try:
        with source.connect() as connection:
            rows = cloud.read_rows(connection)
    finally:
        source.dispose()
    runtime = bundle / "data/sports-sync/working/docs"
    documents = {
        name: json.loads((runtime / name).read_text(encoding="utf-8"))
        for name in cloud.MANIFESTS
        if (runtime / name).is_file()
    }
    with engine.begin() as connection:
        for name, table in cloud.TABLES.items():
            for row in rows[name].values():
                connection.execute(insert(table).values(**cloud.sql_values(row)))
        for filename, document in documents.items():
            cloud.state_put(connection, "document/" + filename, document)
    games = copy.deepcopy(
        [
            row
            for row in documents["results-asb.json"]["games"]
            if row["team"] == "men" and row["season"] == "2026-2027"
        ]
    )
    game = next(row for row in games if row.get("GameStatus") == 1 and row.get("HasTime"))
    game["ScoreA"] += 1
    monkeypatch.setattr(jobs, "season_now", lambda: "2026-2027")
    monkeypatch.setattr(jobs, "fetch_games", lambda *args: games)
    monkeypatch.setattr(cloud, "source_jobs", lambda _: [("asb_games_men", "games", "men")])
    report = cloud.run_cloud(Settings(_env_file=None, env="test"), engine, group="asb", force=True)
    assert report["state"] == "succeeded", report
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(User)) == 0
        actual = cloud.state_get(connection, "document/results-asb.json")
        refreshed = next(row for row in actual["games"] if row["GameID"] == game["GameID"])
        assert refreshed["ScoreA"] == game["ScoreA"]
        assert (
            connection.scalar(
                select(MatchSourceSnapshot.values).where(
                    MatchSourceSnapshot.external_id == str(game["GameID"])
                )
            )["home_score"]
            == game["ScoreA"]
        )
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == checksum


def portrait(content: bytes, source: str = "https://asb.example.test/player.jpg"):
    key = hashlib.sha256(source.encode()).hexdigest()
    entry = {"source_url": source, "sha256": hashlib.sha256(content).hexdigest()}
    return key, entry


def test_failed_replica_refresh_keeps_previous_index_and_photo(tmp_path, monkeypatch):
    old, new = b"old webp", b"new webp"
    key, previous = portrait(old)
    _, updated = portrait(new)
    second, missing = portrait(b"missing", "https://asb.example.test/other.jpg")
    directory = tmp_path / "replica"
    cloud_state.replace_bytes(directory / "index.json", json.dumps({key: previous}).encode())
    cloud_state.replace_bytes(directory / "objects" / f"{previous['sha256']}.webp", old)

    class Storage:
        async def read(self, *, key):
            if updated["sha256"] in key:
                return new
            raise RuntimeError("unavailable")

    async def state(*args):
        return {key: updated, second: missing}

    monkeypatch.setattr(cloud_state, "read_cloud_state", state)
    with pytest.raises(RuntimeError):
        asyncio.run(cloud_state.pull_portrait_cache(None, Storage(), directory))
    assert json.loads((directory / "index.json").read_text()) == {key: previous}
    assert _cached_photo_in(directory, previous["source_url"]).read_bytes() == old


def test_portrait_upload_skips_unchanged_objects_and_checks_hashes(tmp_path):
    settings = Settings(_env_file=None, media_storage_path=tmp_path / "storage")
    directory = tmp_path / "photos"
    key, entry = portrait(b"portrait webp")
    directory.mkdir()
    (directory / f"{key}.webp").write_bytes(b"portrait webp")
    (directory / "index.json").write_text(json.dumps({key: entry}))
    asyncio.run(cloud.portrait_objects(settings, directory, upload=True))
    stored = tmp_path / "storage" / cloud.PORTRAIT_PREFIX / f"{entry['sha256']}.webp"
    assert stored.read_bytes() == b"portrait webp"
    stored.unlink()
    asyncio.run(cloud.portrait_objects(settings, directory, upload=True, previous={key: entry}))
    assert not stored.exists(), "A durable previous index avoids repeated storage uploads"
    (directory / f"{key}.webp").write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        asyncio.run(cloud.portrait_objects(settings, directory, upload=True))
