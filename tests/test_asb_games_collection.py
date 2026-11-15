"""Verified ASB status and time precision must survive collection and import."""

import asyncio
import copy
import importlib.util
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine
from predatory_beavers.modules.matches import models as _match_models  # noqa: F401
from predatory_beavers.modules.results import models as _archive_models  # noqa: F401
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


collector = load_script("collect_asb_archive")
importer = load_script("import_public_results")


def modern_fixture(*, has_time=True, status="Scheduled"):
    return dict(
        game=dict(
            id=123456789,
            gameStatus=status,
            hasTime=has_time,
            defaultZoneDateTime="2099-11-14T15:30:00+03:00",
            score1=0,
            score2=0,
        ),
        team1=dict(teamId=7433, name="ВГУ", regionName="Воронеж", logo=None),
        team2=dict(teamId=7432, name="ВГТУ", regionName="Воронеж", logo=None),
        arena=dict(id=12371, name='С/з "ВГУ"'),
        comp=dict(name="Круговой турнир"),
        league=dict(name="Воронежская область (м)"),
    )


def normalized(**kwargs):
    return collector.modern_game(
        modern_fixture(**kwargs),
        "men",
        7433,
        "2099-2100",
        999,
        "https://asb.sportoteka.org/api/abc/teams/games",
    )


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "asb.sqlite"
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///" + path.as_posix(),
        media_storage_path=tmp_path / "media",
        log_json=False,
    )

    async def create():
        engine = create_engine(settings)
        async with engine.begin() as connection:
            tables = [
                Base.metadata.tables[name]
                for name in [
                    "teams",
                    "media_assets",
                    "competitions",
                    "venues",
                    "matches",
                    "archive_results",
                    "archive_coverage",
                    "match_source_snapshots",
                ]
            ]
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
        await engine.dispose()

    asyncio.run(create())
    return path


def test_modern_api_validates_team_and_season_without_inventing_scores():
    game = normalized()
    result = importer.asb_result(game)
    assert result.status == "scheduled"
    assert result.club_score is None and result.opponent_score is None
    assert game["GameTimeMsk"] == "15:30"
    assert collector.modern_game(modern_fixture(), "men", 7433, "2026-2027", 0, "url") is None
    with pytest.raises(ValueError, match="another team's game"):
        collector.modern_game(modern_fixture(), "men", 7695, "2099-2100", 999, "url")


def test_scheduled_fixture_remains_live_in_archive_until_the_result_is_published(database):
    game = normalized()
    result = importer.asb_result(game)
    counts = importer.apply_import(database, [result], [], [game])
    assert counts["matches_created"] == 0
    assert counts["matches_scheduled_archive_only"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status,club_score,opponent_score FROM archive_results"
        ).fetchone() == ("scheduled", None, None)

    game.update(GameStatus=1, official_status="ResultConfirmed", ScoreA=73, ScoreB=69)
    result = importer.asb_result(game)
    counts = importer.apply_import(database, [result], [], [game])
    assert counts["archive_updated"] == 1
    assert counts["matches_created"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
        match = connection.execute(
            "SELECT starts_at,status,home_score,away_score FROM matches"
        ).fetchone()
        assert connection.execute(
            "SELECT status,club_score,opponent_score FROM archive_results"
        ).fetchone() == ("finished", 73, 69)
    assert match == ("2099-11-14 12:30:00", "FINISHED", 73, 69)


def test_reimport_preserves_editor_scores_status_and_other_match_fields(database):
    game = normalized()
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    game.update(GameStatus=1, official_status="ResultConfirmed", ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE matches SET home_score=77,away_score=70,status='CANCELLED',"
            "notes='Editor decision',starts_at='2099-11-15 12:00:00'"
        )
        snapshot = connection.execute("SELECT * FROM matches").fetchone()

    # An upstream correction refreshes the source archive, while every editor
    # override survives. A subsequent identical import must also be idempotent.
    game.update(ScoreA=74, ScoreB=69, GameDate="15.11.2099", GameTimeMsk="18:30")
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["archive_updated"] == 1
    assert counts["matches_preserved"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM matches").fetchone() == snapshot
        assert connection.execute(
            "SELECT status,club_score,opponent_score,event_date FROM archive_results"
        ).fetchone() == ("finished", 74, 69, "2099-11-15")
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["archive_unchanged"] == 1
    assert counts["matches_created"] == 0


def test_published_match_receives_source_score_and_kickoff_corrections(database):
    game = normalized(status="ResultConfirmed")
    game.update(ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with sqlite3.connect(database) as connection:
        first_version = connection.execute("SELECT version_id FROM matches").fetchone()[0]
        # Values written through the ORM use this equivalent representation.
        connection.execute("UPDATE matches SET starts_at=starts_at||'.000000'")

    game.update(ScoreA=74, GameDate="15.11.2099", GameTimeMsk="18:30")
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["matches_updated"] == 1
    with sqlite3.connect(database) as connection:
        match = connection.execute(
            "SELECT starts_at,status,home_score,away_score,version_id FROM matches"
        ).fetchone()
        projection = json.loads(
            connection.execute('SELECT "values" FROM match_source_snapshots').fetchone()[0]
        )
    assert match == ("2099-11-15 15:30:00", "FINISHED", 74, 69, first_version + 1)
    assert projection["home_score"] == 74
    assert projection["starts_at"] == "2099-11-15 15:30:00"
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["matches_updated"] == 0
    assert counts["matches_preserved"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_id FROM matches").fetchone()[0] == match[-1]


def test_one_editor_result_override_preserves_the_coherent_result_group(database):
    game = normalized(status="ResultConfirmed")
    game.update(ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE matches SET notes='Editor decision'")
        before = connection.execute("SELECT * FROM matches").fetchone()
    game.update(ScoreA=74, GameDate="15.11.2099", GameTimeMsk="18:30", CompNameRu="New stage")
    for _ in range(2):
        importer.apply_import(database, [importer.asb_result(game)], [], [game])
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT * FROM matches").fetchone() == before


def test_editor_name_does_not_block_unedited_official_result_fields(database):
    game = normalized(status="ResultConfirmed")
    game.update(ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE matches SET home_team_name='Editor team name'")
    game.update(ScoreA=74, TeamNameAru="Official changed name")
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["matches_updated"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT home_team_name,home_score FROM matches").fetchone() == (
            "Editor team name",
            74,
        )


@pytest.mark.parametrize("editor_sql", ["is_deleted=1", "active=0"])
def test_automatic_refresh_preserves_deleted_and_draft_matches(database, editor_sql):
    game = normalized(status="ResultConfirmed")
    game.update(ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE matches SET " + editor_sql)
        before = connection.execute("SELECT * FROM matches").fetchone()
        source_before = connection.execute("SELECT * FROM match_source_snapshots").fetchone()
    game.update(ScoreA=74, GameDate="15.11.2099")
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["matches_preserved"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM matches").fetchone() == before
        assert (
            connection.execute("SELECT * FROM match_source_snapshots").fetchone() == source_before
        )


@pytest.mark.parametrize("edited", [False, True])
def test_existing_match_adoption_uses_verified_previous_manifest(database, edited):
    game = normalized(status="ResultConfirmed")
    game.update(ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    previous = copy.deepcopy(game)
    with sqlite3.connect(database) as connection:
        # This is how a pre-0020 imported database appears after migration.
        connection.execute("DELETE FROM match_source_snapshots")
        if edited:
            connection.execute("UPDATE matches SET notes='Pre-existing editor note'")
        before = connection.execute("SELECT * FROM matches").fetchone()
    game.update(ScoreA=74, GameDate="15.11.2099", GameTimeMsk="18:30")
    counts = importer.apply_import(
        database, [importer.asb_result(game)], [], [game], previous_games=[previous]
    )
    assert counts["matches_updated"] == (0 if edited else 1)
    with sqlite3.connect(database) as connection:
        current = connection.execute("SELECT * FROM matches").fetchone()
        assert connection.execute("SELECT count(*) FROM match_source_snapshots").fetchone()[0] == 1
        if edited:
            assert current == before
        else:
            assert connection.execute("SELECT home_score,starts_at FROM matches").fetchone() == (
                74,
                "2099-11-15 15:30:00",
            )


def test_existing_match_without_verified_previous_source_is_not_assumed_owned(database):
    game = normalized(status="ResultConfirmed")
    game.update(ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM match_source_snapshots")
        before = connection.execute("SELECT * FROM matches").fetchone()
    game.update(ScoreA=74, GameDate="15.11.2099", GameTimeMsk="18:30")
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["matches_unverified_preserved"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM matches").fetchone() == before
        assert connection.execute("SELECT count(*) FROM match_source_snapshots").fetchone()[0] == 0


def test_official_cancellation_refreshes_an_existing_result_without_creating_fixtures(database):
    game = normalized(status="ResultConfirmed")
    game.update(ScoreA=73, ScoreB=69)
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    game.update(GameStatus=0, official_status="Cancelled")
    counts = importer.apply_import(database, [importer.asb_result(game)], [], [game])
    assert counts["matches_updated"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,home_score,away_score FROM matches"
        ).fetchone() == ("CANCELLED", None, None)


def test_date_only_fixture_remains_in_archive_without_a_fabricated_kickoff(database):
    game = normalized(has_time=False)
    result = importer.asb_result(game)
    counts = importer.apply_import(database, [result], [], [game])
    assert counts["matches_created"] == 0
    assert counts["matches_scheduled_archive_only"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 0
        assert connection.execute(
            "SELECT event_date,date_precision,status FROM archive_results"
        ).fetchone() == ("2099-11-14", "day", "scheduled")


def test_legacy_zero_status_distinguishes_future_schedule_from_missing_result():
    game = normalized()
    game.pop("official_status")
    assert importer.asb_status(game, today=date(2099, 11, 13)) == "scheduled"
    assert importer.asb_status(game, today=date(2099, 11, 15)) == "unpublished"
    game["official_status"] = "Cancelled"
    assert importer.asb_result(game).status == "cancelled"


def test_modern_corrections_override_stale_widget_score_status_and_kickoff(monkeypatch, tmp_path):
    widget = normalized()
    widget.update(GameDate="13.11.2099", GameTimeMsk="11:00", GameStatus=0, ScoreA=0, ScoreB=0)
    modern = modern_fixture(status="ResultConfirmed")
    modern["game"].update(score1=73, score2=69)

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get(self, url):
            if "GetTeamSeasons/7433" in url:
                return Response([dict(CompID=999, Year=2100)])
            if "GetTeamSeasons/7695" in url or "GetTeamComps/" in url:
                return Response([])
            if "TeamGames/7433" in url:
                return Response([widget])
            if "/teams/games?teamId=7433" in url or "/comps/calendar?tag=asb-cfo-36-m" in url:
                return Response(dict(status="Ok", totalCount=1, items=[modern]))
            raise AssertionError(f"Unexpected endpoint: {url}")

    monkeypatch.setattr(collector.httpx, "Client", lambda **_: Client())
    monkeypatch.setattr(collector, "OUT", tmp_path / "manifest.json")
    monkeypatch.setattr(collector, "CACHE", tmp_path / "evidence")
    collected = collector.collect()["games"]
    assert len(collected) == 1
    result = collected[0]
    assert (result["GameStatus"], result["ScoreA"], result["ScoreB"]) == (1, 73, 69)
    assert (result["GameDate"], result["GameTimeMsk"]) == ("14.11.2099", "15:30")
    assert result["official_status"] == "ResultConfirmed"
    assert result["status_source_url"].startswith("https://asb.sportoteka.org/")
