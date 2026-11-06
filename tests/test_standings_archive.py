import asyncio
import importlib.util
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.matches.models import ClubSide, Competition, Match, MatchStatus
from predatory_beavers.modules.standings.models import StandingsSnapshot
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "asb_standings_import", ROOT / "scripts/import_asb_standings.py"
)
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)


def table_payload(competition_id: str = "60001", **changes: object) -> dict:
    return {
        "team": "men",
        "external_team_id": "7433",
        "season": "2025-2026",
        "competition_id": competition_id,
        "name": "АСБ · Воронежская область (м) · Круговой турнир",
        "path": ["Сезон 2025/2026", "Воронежская область (м)", "Круговой турнир"],
        "source_url": f"https://asb.infobasket.su/Widget/CompTeamResults/{competition_id}?format=json",
        "last_game_at": "2026-02-02T00:00:00Z",
        "last_game_precision": "day",
        "stage_order": 2,
        "rows": [
            {
                "position": 1,
                "team_name": "ВГУ",
                "external_team_id": "7433",
                "played": 2,
                "wins": 2,
                "losses": 0,
                "draws": 0,
                "table_points": 4,
                "points_for": 150,
                "points_against": 120,
            }
        ],
        **changes,
    }


@pytest.fixture
def archive_db(tmp_path: Path) -> tuple[Path, Settings, str, str]:
    database = tmp_path / "standings.sqlite"
    settings = Settings(
        env="test", database_url="sqlite+aiosqlite:///" + database.as_posix(), log_json=False
    )

    async def seed() -> tuple[str, str]:
        engine = create_engine(settings)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with create_session_factory(engine)() as session:
            men = Team(slug="men", name="ВГУ", category=TeamCategory.MEN, active=True)
            women = Team(slug="women", name="ВГУ", category=TeamCategory.WOMEN, active=True)
            calendar = Competition(
                name="АСБ · весь сезон",
                season="2025-2026",
                source="asb",
                external_id="49695",
                active=True,
            )
            session.add_all([men, women, calendar])
            await session.flush()
            session.add(
                Match(
                    team_id=men.id,
                    competition_id=calendar.id,
                    starts_at=datetime(2026, 1, 1, tzinfo=UTC),
                    club_side=ClubSide.HOME,
                    home_team_name="ВГУ",
                    away_team_name="Редакторский соперник",
                    home_score=99,
                    away_score=70,
                    status=MatchStatus.FINISHED,
                    source="manual",
                    external_id=None,
                )
            )
            await session.commit()
            result = str(men.id), str(calendar.id)
        await engine.dispose()
        return result

    team_id, calendar_id = asyncio.run(seed())
    return database, settings, team_id, calendar_id


def test_official_table_rejects_registration_lists_and_wrong_identities() -> None:
    raw = {
        "CompID": 60001,
        "TeamID": 7433,
        "Place": 1,
        "CompTeamName": {"CompTeamNameRu": "ВГУ"},
        "Standings": {
            "StandingGame": None,
            "StandingWin": None,
            "StandingLose": None,
            "StandingPoints": None,
            "StandingGoalPlus": None,
            "StandingGoalMinus": None,
        },
    }
    with pytest.raises(ValidationError):
        sync.OfficialTable.model_validate(table_payload(rows=[raw]))
    raw["Standings"] = {
        "StandingGame": 2,
        "StandingWin": 2,
        "StandingLose": 0,
        "StandingPoints": 4,
        "StandingGoalPlus": 150,
        "StandingGoalMinus": 120,
    }
    assert sync.OfficialTable.model_validate(table_payload(rows=[raw])).rows[0].wins == 2
    raw["CompID"] = 60002
    with pytest.raises(ValidationError, match="another competition"):
        sync.OfficialTable.model_validate(table_payload(rows=[raw]))
    for changes in [
        {"external_team_id": "7695"},
        {"source_url": "https://example.com/60001"},
        {"last_game_at": "2024-12-01T00:00:00Z"},
        {"season": "2024-2026"},
    ]:
        with pytest.raises(ValidationError):
            sync.OfficialTable.model_validate(table_payload(**changes))


def test_archive_import_is_idempotent_and_leaves_calendar_unchanged(archive_db) -> None:
    database, _, _, _ = archive_db
    table = sync.OfficialTable.model_validate(table_payload())
    with sqlite3.connect(database) as connection:
        before_matches = connection.execute("SELECT * FROM matches").fetchall()
        before_competitions = connection.execute("SELECT * FROM competitions").fetchall()
    first = sync.apply_tables(database, [table], datetime(2026, 11, 5, tzinfo=UTC))
    assert first["snapshots_created"] == first["competitions_created"] == 1
    second = sync.apply_tables(database, [table], datetime(2026, 11, 6, tzinfo=UTC))
    assert second["snapshots_unchanged"] == 1
    assert second["snapshots_created"] == second["snapshots_updated"] == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM matches").fetchall() == before_matches
        assert all(
            row in connection.execute("SELECT * FROM competitions").fetchall()
            for row in before_competitions
        )
        assert connection.execute("SELECT count(*) FROM standings_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM admin_audit_log").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_season_anchor_is_relinked_only_for_identical_statistics(archive_db) -> None:
    database, settings, team_id, calendar_id = archive_db
    table = sync.OfficialTable.model_validate(table_payload())

    async def seed() -> str:
        from uuid import UUID

        engine = create_engine(settings)
        async with create_session_factory(engine)() as session:
            legacy = StandingsSnapshot(
                team_id=UUID(team_id),
                competition_id=UUID(calendar_id),
                rows=sync.normalized_rows(table),
                source="asb",
                source_reference="60001",
                fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
                is_current=True,
            )
            session.add(legacy)
            await session.commit()
            result = str(legacy.id)
        await engine.dispose()
        return result

    legacy_id = asyncio.run(seed())
    counts = sync.apply_tables(database, [table], datetime(2026, 11, 5, tzinfo=UTC))
    assert counts["snapshots_relinked"] == 1
    assert counts["snapshots_created"] == 0
    with TestClient(create_app(settings)) as client:
        options = client.get("/api/v1/standings/options?team=men").json()["data"]
        assert len(options["competitions"]) == 1
        assert options["competitions"][0]["snapshot_id"] == legacy_id
        assert options["competitions"][0]["id"] != calendar_id
        assert client.get("/api/v1/standings?team=men").json()["data"]["rows"][0]["wins"] == 2


def test_editor_snapshot_is_preserved(archive_db) -> None:
    database, settings, team_id, _ = archive_db
    table = sync.OfficialTable.model_validate(table_payload())

    async def seed() -> None:
        from uuid import UUID

        engine = create_engine(settings)
        async with create_session_factory(engine)() as session:
            competition = Competition(
                name="Редакторское название",
                season=table.season,
                source="asb",
                external_id=table.competition_id,
                active=True,
            )
            session.add(competition)
            await session.flush()
            session.add(
                StandingsSnapshot(
                    team_id=UUID(team_id),
                    competition_id=competition.id,
                    rows=sync.normalized_rows(table),
                    source="manual",
                    fetched_at=datetime(2026, 11, 1, tzinfo=UTC),
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())
    counts = sync.apply_tables(database, [table], datetime(2026, 11, 5, tzinfo=UTC))
    assert counts["editor_snapshots_preserved"] == 1
    assert counts["snapshots_created"] == counts["snapshots_updated"] == 0


def test_public_options_and_default_follow_actual_season_and_game_chronology(archive_db) -> None:
    database, settings, _, _ = archive_db
    older = sync.OfficialTable.model_validate(
        table_payload("60002", season="2024-2025", last_game_at="2025-03-01T00:00:00Z")
    )
    regional = sync.OfficialTable.model_validate(table_payload())
    later = sync.OfficialTable.model_validate(
        table_payload(
            "60003",
            name="АСБ · Лига Белова · Группа В",
            last_game_at="2026-03-20T00:00:00Z",
            stage_order=1,
        )
    )
    unplayed = sync.OfficialTable.model_validate(
        table_payload(
            "60004",
            name="АСБ · следующий этап",
            last_game_at=None,
            stage_order=999,
            rows=[
                {
                    **table_payload()["rows"][0],
                    "played": 0,
                    "wins": 0,
                    "losses": 0,
                    "table_points": 0,
                    "points_for": 0,
                    "points_against": 0,
                }
            ],
        )
    )
    girls = sync.OfficialTable.model_validate(
        table_payload(
            "60005",
            team="women",
            external_team_id="7695",
            rows=[{**table_payload()["rows"][0], "external_team_id": "7695"}],
        )
    )
    sync.apply_tables(
        database, [older, regional, later, unplayed, girls], datetime(2026, 11, 5, tzinfo=UTC)
    )
    with sqlite3.connect(database) as connection:
        # Importing an older stage more recently must not change the default.
        connection.execute(
            "UPDATE standings_snapshots SET fetched_at='2028-01-01T00:00:00+00:00' "
            "WHERE source_reference IN ('60001','60002')"
        )
    with TestClient(create_app(settings)) as client:
        options = client.get("/api/v1/standings/options?team=men").json()["data"]
        assert options["seasons"] == ["2025-2026", "2024-2025"]
        assert len(options["competitions"]) == 4
        assert all(item["row_count"] == 1 for item in options["competitions"])
        assert options["competitions"][0]["source_reference"] == "60003"
        current = client.get("/api/v1/standings?team=men").json()["data"]
        assert current["competition_id"] == options["default_competition_id"]
        assert current["source_reference"] == "60003"
        assert current["source_url"] == str(later.source_url)
        assert (
            client.get("/api/v1/standings?team=men&season=2024-2025").json()["data"][
                "source_reference"
            ]
            == "60002"
        )
        assert (
            client.get(
                f"/api/v1/standings?team=men&season=2024-2025&competition_id={current['competition_id']}"
            ).status_code
            == 404
        )
        girls_options = client.get("/api/v1/standings/options?team=women").json()["data"]
        assert len(girls_options["competitions"]) == 1
        assert girls_options["competitions"][0]["source_reference"] == "60005"
        assert client.get("/api/v1/standings/options?team=missing").json()["data"]["seasons"] == []


def test_source_database_guard_and_fresh_migration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sync, "ROOT", tmp_path)
    source = tmp_path / "data/predatory_beavers.db"
    source.parent.mkdir()
    source.write_bytes(b"preserved original")
    with pytest.raises(ValueError, match="preserved source"):
        sync.apply_tables(source, [], datetime.now(UTC))
    assert source.read_bytes() == b"preserved original"
    database = tmp_path / "migrations.sqlite"
    environment = {
        **os.environ,
        "APP_DATABASE_URL": "sqlite+aiosqlite:///" + database.as_posix(),
        "APP_ENV": "test",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0014"
        columns = {item[1] for item in connection.execute("PRAGMA table_info(standings_snapshots)")}
        assert {
            "source_url",
            "last_game_at",
            "stage_order",
            "tournament_id",
            "stage_kind",
            "games",
        } <= columns


def test_championships_and_playoffs_are_distinct_and_import_idempotently(archive_db) -> None:
    database, settings, _, _ = archive_db
    group = sync.OfficialTable.model_validate(
        table_payload(
            tournament_id="60000",
            tournament_name="Региональный чемпионат",
            stage_name="Групповой этап",
            stage_kind="group",
        )
    )
    game = {
        "external_id": "70001",
        "round_name": "Финал",
        "round": 1,
        "tree": 1,
        "starts_at": "2026-03-01T12:00:00Z",
        "date_precision": "time",
        "home_team_name": "ВГУ",
        "home_team_id": "7433",
        "away_team_name": "ВГТУ",
        "away_team_id": "7432",
        "home_score": 83,
        "away_score": 70,
        "status": "finished",
        "source_url": "https://asb.infobasket.su/Widget/PlayOff/60002?format=json",
    }
    playoff = sync.OfficialTable.model_validate(
        table_payload(
            "60002",
            name="Региональный чемпионат / Плей-офф",
            rows=[],
            games=[game],
            stage_kind="playoff",
            stage_name="Плей-офф",
            tournament_id="60000",
            tournament_name="Региональный чемпионат",
            last_game_at="2026-03-01T12:00:00Z",
            source_url=game["source_url"],
        )
    )
    belov = sync.OfficialTable.model_validate(
        table_payload(
            "60003",
            tournament_id="61000",
            tournament_name="Лига Белова",
            stage_name="Группа А",
            last_game_at="2026-04-01T00:00:00Z",
        )
    )
    tables = [group, playoff, belov]
    sync.apply_tables(database, tables, datetime.now(UTC))
    assert sync.apply_tables(database, tables, datetime.now(UTC))["snapshots_unchanged"] == 3
    with TestClient(create_app(settings)) as client:
        options = client.get("/api/v1/standings/options?team=men").json()["data"]
        assert {item["tournament_id"] for item in options["competitions"]} == {"60000", "61000"}
        stage = next(item for item in options["competitions"] if item["stage_kind"] == "playoff")
        assert stage["game_count"] == 1 and stage["row_count"] == 0
        selected = client.get("/api/v1/standings?team=men&tournament_id=60000").json()["data"]
        assert selected["source_reference"] == "60002"
        assert selected["rows"] == []
        assert selected["games"][0]["home_score"] == 83
        assert selected["games"][0]["away_score"] == 70
        assert (
            client.get(
                f"/api/v1/standings?team=men&tournament_id=61000&competition_id={stage['id']}"
            ).status_code
            == 404
        )
    with pytest.raises(ValidationError, match="Both scores"):
        sync.OfficialTable.model_validate(
            {**playoff.model_dump(mode="json"), "games": [{**game, "away_score": None}]}
        )
    with pytest.raises(ValidationError, match="absent"):
        sync.OfficialTable.model_validate(
            {**playoff.model_dump(mode="json"), "games": [{**game, "home_team_id": "9999"}]}
        )


def test_editor_republication_keeps_stage_in_its_championship(archive_db) -> None:
    from uuid import UUID

    from predatory_beavers.db.uow import SqlAlchemyUnitOfWork
    from predatory_beavers.modules.club.repository import TeamRepository
    from predatory_beavers.modules.matches.repository import CompetitionRepository
    from predatory_beavers.modules.standings.repository import StandingsRepository
    from predatory_beavers.modules.standings.schemas import StandingsPublish
    from predatory_beavers.modules.standings.service import StandingsService

    database, settings, team_id, _ = archive_db
    table = sync.OfficialTable.model_validate(
        table_payload(
            tournament_id="60000",
            tournament_name="Regional championship",
            stage_name="Group stage",
            stage_kind="group",
        )
    )
    sync.apply_tables(database, [table], datetime.now(UTC))

    async def republish() -> str:
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                repository = StandingsRepository(session)
                current = await repository.get_current(team="men", tournament_id="60000")
                assert current is not None
                service = StandingsService(
                    repository,
                    TeamRepository(session),
                    CompetitionRepository(session),
                    SqlAlchemyUnitOfWork(session),
                )
                rows = sync.normalized_rows(table)
                rows[0]["points_for"] = 151
                published = await service.publish(
                    StandingsPublish(
                        team_id=UUID(team_id), competition_id=current.competition_id, rows=rows
                    )
                )
                return str(published.competition_id)
        finally:
            await engine.dispose()

    competition_id = asyncio.run(republish())
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/standings",
            params={"team": "men", "tournament_id": "60000", "competition_id": competition_id},
        )
        assert response.status_code == 200
        current = response.json()["data"]
        assert current["source"] == "manual"
        assert current["source_reference"] == table.competition_id
        assert current["rows"][0]["points_for"] == 151
        assert current["tournament_name"] == "Regional championship"
        assert current["stage_name"] == "Group stage"
        assert current["stage_kind"] == "group"
        assert current["stage_order"] == table.stage_order
        assert current["source_url"] == str(table.source_url)
        assert current["last_game_at"] == "2026-02-02T00:00:00Z"
        assert current["games"] == []
        options = client.get("/api/v1/standings/options?team=men").json()["data"]
        assert len(options["competitions"]) == 1
        assert options["competitions"][0]["tournament_id"] == "60000"
