"""Portable settings plus opt-in integration tests against a disposable PostgreSQL.

Set PB_TEST_POSTGRES_URL to an isolated test database. Each test owns a fresh schema;
the application database URL is never used as a fallback.
"""

import asyncio
import os
import runpy
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import DropSchema

from predatory_beavers.api.main import create_app
from predatory_beavers.db.session import (
    create_engine,
    create_session_factory,
    database_connect_args,
)
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.content.models import ContentBlock
from predatory_beavers.modules.imports.models import ImportJob, ImportStatus
from predatory_beavers.modules.matches.models import ClubSide, Competition, Match, MatchStatus
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.roster.models import AsbRosterMember, AsbRosterSnapshot
from predatory_beavers.modules.standings.models import StandingsSnapshot
from predatory_beavers.settings import Settings, get_settings


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_postgres_settings_normalize_driver_without_losing_password(scheme: str) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"{scheme}://editor:p%40ss%25word@localhost:5432/club?sslmode=require",
        database_schema="beavers",
    )
    assert settings.database_url == (
        "postgresql+psycopg://editor:p%40ss%25word@localhost:5432/club?sslmode=require"
    )
    assert "p%40ss" not in repr(settings)
    assert database_connect_args(settings) == {
        "connect_timeout": 15,
        "prepare_threshold": None,
        "options": "--search_path=beavers",
    }


@pytest.mark.parametrize(
    "value",
    [
        "mysql://editor:secret@localhost/club",
        "postgresql+asyncpg://editor:secret@localhost/club",
        "postgresql://editor:secret@localhost/",
        "postgresql://editor:secret@:5432/club",
        "postgresql://editor:secret@localhost:bad/club",
        "sqlite:///local.db",
    ],
)
def test_invalid_database_configuration_does_not_expose_password(value: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url=value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("schema", ["", "pg_catalog", "information_schema", "a,b", "a;drop", "A"])
def test_database_schema_is_a_safe_non_system_identifier(schema: str) -> None:
    with pytest.raises(ValidationError, match="SQL identifier"):
        Settings(_env_file=None, database_schema=schema)


@pytest.mark.parametrize("mode", [None, "disable", "allow", "prefer"])
def test_production_postgres_requires_tls(mode: str | None) -> None:
    url = "postgresql://editor:test-only@database.example.test/club"
    if mode is not None:
        url += f"?sslmode={mode}"
    with pytest.raises(ValidationError, match="explicit TLS"):
        Settings(
            _env_file=None,
            env="prod",
            reload=False,
            database_url=url,
            cookie_secure=True,
            session_cookie_name="__Host-pb_session",
            cors_origins=["https://club.example.test"],
        )


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_production_postgres_accepts_explicit_tls(mode: str) -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        reload=False,
        database_url=f"postgresql://editor:test-only@database.example.test/club?sslmode={mode}",
        cookie_secure=True,
        session_cookie_name="__Host-pb_session",
        cors_origins=["https://club.example.test"],
    )
    assert settings.database_url.endswith(f"sslmode={mode}")


@pytest.mark.skipif(sys.platform != "win32", reason="psycopg Windows event-loop compatibility")
@pytest.mark.parametrize("database", ["sqlite+aiosqlite:///:memory:", "postgresql://u:p@localhost/db"])
def test_windows_server_changes_policy_only_for_postgres(database: str) -> None:
    settings = Settings(_env_file=None, env="test", reload=False, database_url=database)
    with (
        patch("predatory_beavers.settings.get_settings", return_value=settings),
        patch("uvicorn.run"),
        patch("asyncio.set_event_loop_policy") as set_policy,
    ):
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "run.py"), run_name="__main__")
    if database.startswith("postgresql"):
        assert isinstance(set_policy.call_args.args[0], asyncio.WindowsSelectorEventLoopPolicy)
    else:
        set_policy.assert_not_called()


@pytest.fixture
def postgres_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Settings]:
    url = os.environ.get("PB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set PB_TEST_POSTGRES_URL to an isolated PostgreSQL test database")
    # psycopg async does not support Windows' Proactor event loop.
    policy = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    schema = f"test_pg_{uuid4().hex}"
    settings = Settings(
        _env_file=None,
        env="test",
        database_url=url,
        database_schema=schema,
        sports_sync_enabled=False,
        media_storage_backend="local",
        media_storage_path=tmp_path / "media",
        cookie_secure=False,
        log_json=False,
    )
    monkeypatch.setenv("APP_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("APP_DATABASE_SCHEMA", schema)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_SPORTS_SYNC_ENABLED", "false")
    monkeypatch.setenv("APP_MEDIA_STORAGE_BACKEND", "local")
    get_settings.cache_clear()
    engine = create_sync_engine(settings.database_url, connect_args=database_connect_args(settings))
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield settings
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()
        get_settings.cache_clear()
        asyncio.set_event_loop_policy(policy)


@pytest.fixture
def postgres_engine(postgres_settings: Settings) -> Iterator[Engine]:
    engine = create_sync_engine(
        postgres_settings.database_url, connect_args=database_connect_args(postgres_settings)
    )
    try:
        yield engine
    finally:
        engine.dispose()


def test_postgres_migrations_private_schema_and_json_downgrade_guard(
    postgres_settings: Settings, postgres_engine: Engine
) -> None:
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT current_schema()")) == postgres_settings.database_schema
        )
        assert "alembic_version" in inspect(connection).get_table_names()
        assert "users" not in inspect(connection).get_table_names(schema="public")
    with Session(postgres_engine) as session:
        block = ContentBlock(
            section="captains",
            title="Captain",
            body="Contact",
            contacts=[{"kind": "email", "value": "captain@example.test", "label": None}],
        )
        session.add(block)
        session.commit()
        block_id = block.id
    with pytest.raises(RuntimeError, match="Export captain photos and contacts"):
        command.downgrade(Config("alembic.ini"), "0018")
    with Session(postgres_engine) as session:
        saved = session.get(ContentBlock, block_id)
        assert saved is not None and saved.contacts[0]["value"] == "captain@example.test"
        saved.contacts = []
        session.commit()
    command.downgrade(Config("alembic.ini"), "0018")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))


@pytest.mark.skipif(sys.platform != "win32", reason="psycopg Windows event-loop compatibility")
def test_postgres_migrations_work_under_windows_default_proactor_policy(
    postgres_settings: Settings,
) -> None:
    del postgres_settings
    policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        asyncio.set_event_loop_policy(policy)


def test_postgres_partial_indexes_allow_history_and_completed_retries(
    postgres_engine: Engine,
) -> None:
    with Session(postgres_engine) as session:
        team = Team(slug="men", name="Beavers", category=TeamCategory.MEN)
        competition = Competition(name="ASB", season="2026/27")
        session.add_all([team, competition])
        session.flush()
        for current in [False, False, True]:
            session.add(
                StandingsSnapshot(
                    team_id=team.id,
                    competition_id=competition.id,
                    rows=[],
                    fetched_at=datetime.now(UTC),
                    is_current=current,
                )
            )
        session.commit()
        team_id, competition_id = team.id, competition.id
        session.add(
            StandingsSnapshot(
                team_id=team_id,
                competition_id=competition_id,
                rows=[],
                fetched_at=datetime.now(UTC),
                is_current=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.scalar(select(func.count()).select_from(StandingsSnapshot)) == 3
        job = {
            "provider": "asb",
            "team_slug": "men",
            "competition_external_id": "42",
            "standings_external_id": "42",
            "external_team_id": "12",
            "season": "2026/27",
            "request_data": {},
            "actor_username": "test",
        }
        for status in [ImportStatus.COMPLETED, ImportStatus.COMPLETED, ImportStatus.PENDING]:
            session.add(ImportJob(**job, status=status))
        session.commit()
        session.add(ImportJob(**job, status=ImportStatus.RUNNING))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.scalar(select(func.count()).select_from(ImportJob)) == 3


def test_postgres_editor_search_creation_replay_and_optimistic_update(
    postgres_settings: Settings,
) -> None:
    async def seed() -> str:
        engine = create_engine(postgres_settings)
        factory = create_session_factory(engine)
        async with factory() as session:
            team = Team(slug="men", name="Beavers", category=TeamCategory.MEN)
            session.add_all(
                [
                    team,
                    User(
                        username="editor",
                        email="editor@example.test",
                        role=UserRole.EDITOR,
                        password_hash=PasswordSecurity().hash_password("strong-test-password"),
                    ),
                ]
            )
            await session.flush()
            competition = Competition(name="ASB", season="2026 / 2027")
            roster = AsbRosterSnapshot(
                team="men",
                season="2026-2027",
                external_team_id="12",
                competition_id="42",
                is_published=True,
                source_url="https://asb.infobasket.su/team/12",
                stats_source_url="https://asb.infobasket.su/team/12",
                checked_at=datetime.now(UTC),
            )
            session.add_all([competition, roster])
            await session.flush()
            session.add_all(
                [
                    Match(
                        team_id=team.id,
                        competition_id=competition.id,
                        source="asb",
                        external_id="pg-fixture",
                        starts_at=datetime(2026, 6, 30, 21, 30, tzinfo=UTC),
                        club_side=ClubSide.HOME,
                        home_team_name="Beavers",
                        away_team_name="Opponent",
                        home_score=80,
                        away_score=70,
                        status=MatchStatus.FINISHED,
                    ),
                    ArchiveResult(
                        source="fiba3x3",
                        external_id="pg-archive",
                        discipline="3x3",
                        team="men",
                        season="not-a-season",
                        year=2026,
                        tournament="Student cup",
                        event_date=date(2026, 7, 1),
                        date_precision="day",
                        team_name="Beavers",
                        status="participated",
                        source_label="FIBA 3x3",
                    ),
                    StandingsSnapshot(
                        team_id=team.id,
                        competition_id=competition.id,
                        rows=[
                            {
                                "position": 1,
                                "team_name": "Beavers",
                                "played": 1,
                                "wins": 1,
                                "losses": 0,
                                "table_points": 2,
                                "points_for": 80,
                                "points_against": 70,
                            }
                        ],
                        fetched_at=datetime.now(UTC),
                    ),
                    AsbRosterMember(
                        snapshot_id=roster.id,
                        external_player_id="77",
                        full_name="Captain",
                        games_played=1,
                        points_total=12,
                        is_listed=True,
                        statistics_season="2026-2027",
                    ),
                ]
            )
            await session.commit()
            team_id = str(team.id)
        await engine.dispose()
        return team_id

    team_id = asyncio.run(seed())
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(create_app(postgres_settings), backend_options=options) as client:
        assert client.get("/health/ready").status_code == 200
        for endpoint in [
            "/api/v1/home",
            "/api/v1/matches",
            "/api/v1/calendar?season=all",
            "/api/v1/calendar?season=2026-2027",
            "/api/v1/standings/options?team=men",
            "/api/v1/standings?team=men",
            "/api/v1/asb-roster/men",
        ]:
            response = client.get(endpoint)
            assert response.status_code == 200, (endpoint, response.text)
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "editor", "password": "strong-test-password"},
            ).status_code
            == 200
        )
        csrf = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        headers = {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())}
        payload = {"team_id": team_id, "full_name": "Бобёр 50%_ / Капитан"}
        barrier = Barrier(2)

        def save() -> object:
            barrier.wait(timeout=10)
            return client.post("/api/v1/admin/players", headers=headers, json=payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, replay = list(pool.map(lambda _: save(), range(2)))
        assert first.status_code == replay.status_code == 201
        player = first.json()["data"]
        assert replay.json()["data"]["id"] == player["id"]
        assert (
            client.post(
                "/api/v1/admin/players",
                headers={"X-CSRF-Token": csrf},
                json={"team_id": team_id, "full_name": "Other player"},
            ).status_code
            == 201
        )
        for term in ["БОБЁР", "%_", "/", "капитан"]:
            result = client.get("/api/v1/admin/players", params={"q": term})
            assert result.status_code == 200 and result.json()["total"] == 1
        assert client.get("/api/v1/admin/players", params={"q": "absent"}).json()["total"] == 0
        changed = client.post(
            "/api/v1/admin/players", headers=headers, json={**payload, "full_name": "Other"}
        )
        assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
        endpoint = f"/api/v1/admin/players/{player['id']}"
        update = {"full_name": "Updated", "expected_updated_at": player["updated_at"]}
        assert client.patch(endpoint, headers=headers, json=update).status_code == 200
        assert client.patch(endpoint, headers=headers, json=update).status_code == 409
