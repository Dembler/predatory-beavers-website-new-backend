"""Sports refresh exercises real jobs with source fixtures, never live network."""

import asyncio
import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine
from predatory_beavers.modules.matches import models as _matches  # noqa: F401
from predatory_beavers.modules.results import models as _results  # noqa: F401
from predatory_beavers.modules.roster import models as _roster  # noqa: F401
from predatory_beavers.modules.roster.schemas import RosterSnapshotInput
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "sports_refresh_runtime", ROOT / "scripts/sports_refresh_runtime.py"
)
assert spec and spec.loader
refresh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refresh
spec.loader.exec_module(refresh)

import import_public_results as importer  # noqa: E402
import sync_asb_roster as roster  # noqa: E402


@pytest.fixture
def database(tmp_path):
    database = tmp_path / "working.db"
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///" + database.as_posix(),
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
                    "asb_roster_snapshots",
                    "asb_roster_members",
                ]
            ]
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
        await engine.dispose()

    asyncio.run(create())
    return database


@pytest.fixture
def runtime(tmp_path):
    directory = tmp_path / "runtime"
    refresh.atomic_json(directory / "docs/results-asb.json", {"games": [], "coverage": []})
    refresh.atomic_json(directory / "docs/asb-standings.json", {"tables": []})
    refresh.atomic_json(directory / "docs/asb-roster.json", {"snapshots": []})
    refresh.atomic_json(directory / "docs/results-fiba3x3.json", {"results": []})
    return directory


def fixture_game(*, score=73, day="14.11.2026", team="men"):
    return {
        "GameID": 123456789,
        "GameDate": day,
        "HasTime": True,
        "GameTimeMsk": "15:30",
        "GameStatus": 1,
        "official_status": "ResultConfirmed",
        "TeamAid": 7433 if team == "men" else 7695,
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
        "team": team,
        "external_team_id": "7433" if team == "men" else "7695",
        "season": "2026-2027",
        "season_comp_id": "999",
        "source_url": "https://asb.sportoteka.org/api/abc/teams/games",
    }


def modern_game(*, game_id=123456789, score=73):
    return {
        "game": {
            "id": game_id,
            "gameStatus": "ResultConfirmed",
            "hasTime": True,
            "defaultZoneDateTime": "2026-11-14T15:30:00+03:00",
            "score1": score,
            "score2": 69,
        },
        "team1": {"teamId": 7433, "name": "Club", "regionName": "Voronezh"},
        "team2": {"teamId": 7432, "name": "Opponent"},
        "arena": {"id": 12371, "name": "Club gym"},
        "comp": {"name": "Regional stage"},
        "league": {"name": "Regional league"},
    }


def install_game_transport(monkeypatch, items, *, total=None):
    real_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.host == "asb.sportoteka.org"
        if request.url.path == "/api/abc/teams/games":
            return httpx.Response(
                200,
                json={
                    "status": "Ok",
                    "totalCount": len(items) if total is None else total,
                    "items": items,
                },
            )
        if request.url.path == "/api/abc/teams/comps":
            return httpx.Response(200, json={"status": "Ok", "items": [{"compId": 999}]})
        raise AssertionError(f"Unexpected endpoint: {request.url}")

    monkeypatch.setattr(
        refresh.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(refresh, "season_now", lambda: "2026-2027")
    return calls


@pytest.mark.parametrize("has_previous", [True, False])
def test_current_games_use_only_modern_api_even_without_a_legacy_root(monkeypatch, has_previous):
    calls = install_game_transport(monkeypatch, [modern_game()])
    rows = refresh.fetch_games("men", 7433, [fixture_game()] if has_previous else [])
    assert [(row["GameID"], row["ScoreA"], row["season_comp_id"]) for row in rows] == [
        (123456789, 73, "999")
    ]
    assert len(calls) == (1 if has_previous else 2)
    assert all("infobasket.su" not in call and "/Widget/" not in call for call in calls)


@pytest.mark.parametrize("invalid", ["missing", "incomplete", "duplicate"])
def test_invalid_current_calendar_retains_database_and_last_good_manifest(
    monkeypatch, database, runtime, invalid
):
    game = fixture_game()
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    manifest = runtime / "docs/results-asb.json"
    refresh.atomic_json(manifest, {"games": [game], "coverage": []})
    source_before = manifest.read_bytes()
    database_before = database.read_bytes()
    items = [] if invalid == "missing" else [modern_game()]
    if invalid == "duplicate":
        items *= 2
    install_game_transport(monkeypatch, items, total=2 if invalid == "incomplete" else None)
    with pytest.raises(ValueError):
        refresh.sync_games(database, runtime, team="men", preview=False)
    assert database.read_bytes() == database_before
    assert manifest.read_bytes() == source_before


def test_game_refresh_passes_old_manifest_for_safe_existing_match_adoption(
    monkeypatch, database, runtime
):
    game = fixture_game()
    importer.apply_import(database, [importer.asb_result(game)], [], [game])
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM match_source_snapshots")
    manifest = runtime / "docs/results-asb.json"
    refresh.atomic_json(manifest, {"games": [game], "coverage": []})
    changed = fixture_game(score=74, day="15.11.2026")

    def fetch(team, team_id, previous):
        assert team == "men" and team_id == 7433
        assert previous == [game]
        assert refresh.load_json(manifest)["games"] == [game]
        return [changed]

    monkeypatch.setattr(refresh, "fetch_games", fetch)
    monkeypatch.setattr(refresh, "season_now", lambda: "2026-2027")
    counts = refresh.sync_games(database, runtime, team="men", preview=False)
    assert counts["matches_updated"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT home_score,starts_at FROM matches").fetchone() == (
            74,
            "2026-11-15 12:30:00",
        )
    assert refresh.load_json(manifest)["games"] == [changed]


def test_preview_validates_game_sources_without_database_mutations(monkeypatch, database, runtime):
    monkeypatch.setattr(refresh, "fetch_games", lambda *args: [fixture_game()])
    monkeypatch.setattr(refresh, "season_now", lambda: "2026-2027")
    before = database.read_bytes()
    report = refresh.sync_games(database, runtime, team="men", preview=True)
    assert report == {"received": 1, "preview": True}
    assert database.read_bytes() == before


def roster_snapshot(*, games=2, statistics_season="2026-2027", points=30):
    stats = (
        {
            "games_played": games,
            "points_total": points,
            "assists_total": 4,
            "rebounds_total": 10,
            "seconds_total": 2400,
            "statistics_season": statistics_season,
            "statistics_source_url": "https://asb.sportoteka.org/api/abc/teams/stats",
        }
        if statistics_season
        else {
            "games_played": None,
            "points_total": None,
            "assists_total": None,
            "rebounds_total": None,
            "seconds_total": None,
            "statistics_season": None,
            "statistics_source_url": None,
        }
    )
    return RosterSnapshotInput.model_validate(
        {
            "team": "men",
            "season": "2026-2027",
            "external_team_id": "7433",
            "competition_id": "999",
            "is_published": True,
            "source_url": "https://asb.sportoteka.org/api/abc/teams/roster",
            "stats_source_url": "https://asb.sportoteka.org/api/abc/teams/stats",
            "checked_at": "2026-11-15T10:00:00+00:00",
            "players": [
                {
                    "external_player_id": "123",
                    "full_name": "Club Player",
                    "photo_url": None,
                    **stats,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "regression", ["fewer_games", "missing_current_statistics", "missing_total"]
)
def test_roster_statistics_regression_keeps_last_good_database_and_manifest(
    monkeypatch, database, runtime, regression
):
    old = roster_snapshot()
    roster.apply_sync(database, [old])
    manifest = runtime / "docs/asb-roster.json"
    refresh.atomic_json(manifest, {"snapshots": [old.model_dump(mode="json")]})
    before_database, before_manifest = database.read_bytes(), manifest.read_bytes()
    new = (
        roster_snapshot(games=1)
        if regression == "fewer_games"
        else roster_snapshot(statistics_season=None)
    )
    if regression == "missing_total":
        new = roster_snapshot(points=None)
    monkeypatch.setattr(roster, "collect", lambda **kwargs: [new])
    with pytest.raises(ValueError, match="keeping last-good data"):
        refresh.sync_roster(database, runtime, team="men", preview=False)
    assert database.read_bytes() == before_database
    assert manifest.read_bytes() == before_manifest


def test_disappeared_standings_stage_keeps_last_good_table(monkeypatch, database, runtime):
    import collect_asb_standings as collector
    import import_asb_standings as standings_importer

    manifest = runtime / "docs/asb-standings.json"
    refresh.atomic_json(
        manifest,
        {"tables": [{"team": "men", "season": "2026-2027", "competition_id": "999"}]},
    )
    before_database, before_manifest = database.read_bytes(), manifest.read_bytes()
    original_teams = collector.TEAMS

    async def collect(fetch):
        assert collector.TEAMS == {"men": 7433}
        return [], []

    def forbidden(*args):
        raise AssertionError("Disappeared stages must not be imported")

    monkeypatch.setattr(collector, "collect_modern", collect)
    monkeypatch.setattr(standings_importer, "apply_tables", forbidden)
    monkeypatch.setattr(refresh, "season_now", lambda: "2026-2027")
    with pytest.raises(ValueError, match="stages disappeared"):
        refresh.sync_standings(database, runtime, team="men", preview=False)
    assert collector.TEAMS is original_teams
    assert database.read_bytes() == before_database
    assert manifest.read_bytes() == before_manifest


def test_roster_refresh_keeps_verified_historical_statistics_without_refetching_legacy(
    monkeypatch, database, runtime
):
    old = roster_snapshot(statistics_season="2025-2026")
    refresh.atomic_json(
        runtime / "docs/asb-roster.json", {"snapshots": [old.model_dump(mode="json")]}
    )
    fresh = roster_snapshot(statistics_season=None)

    def collect(*, teams):
        assert teams == [("men", 7433)]
        assert (
            roster.historical_statistics(None, fresh.players, 7433, fresh.season) == fresh.players
        )
        return [fresh]

    original = roster.historical_statistics
    monkeypatch.setattr(roster, "collect", collect)
    refresh.sync_roster(database, runtime, team="men", preview=False)
    assert roster.historical_statistics is original
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT statistics_season,games_played,points_total FROM asb_roster_members"
        ).fetchone() == ("2025-2026", 2, 30)


def test_jobs_fail_independently_and_retain_last_success(monkeypatch, database, runtime):
    monkeypatch.setattr(refresh, "working_tree", lambda _: runtime)
    monkeypatch.setattr(refresh, "daily_backup", lambda _: None)
    monkeypatch.setattr(refresh, "migrate_if_needed", lambda _: None)
    calls = []

    def dataset(kind, failed_team=None):
        def run(db, directory, *, team, preview):
            calls.append((kind, team))
            if team == failed_team:
                raise httpx.ConnectError("Offline source")
            return {"updated": 1}

        return run

    monkeypatch.setattr(refresh, "sync_games", dataset("games", "men"))
    monkeypatch.setattr(refresh, "sync_match_statistics", dataset("match_statistics"))
    monkeypatch.setattr(refresh, "sync_standings", dataset("standings", "women"))
    monkeypatch.setattr(refresh, "sync_roster", dataset("roster"))

    def portraits(*args, **kwargs):
        calls.append(("portraits", None))
        raise ValueError("Broken portrait")

    monkeypatch.setattr(refresh, "sync_portraits", portraits)
    monkeypatch.setattr(refresh, "sync_fiba", lambda *args, **kwargs: {"status": "auth_required"})
    source_file = database.parent / "sports-sync/sources.json"
    last_success = "2026-11-14T10:00:00+00:00"
    refresh.atomic_json(source_file, {"asb_games_men": {"last_success_at": last_success}})
    report = refresh.run_refresh(database)
    assert report["status"] == "partial"
    assert calls == [
        ("games", "men"),
        ("games", "women"),
        ("match_statistics", "men"),
        ("match_statistics", "women"),
        ("standings", "men"),
        ("standings", "women"),
        ("roster", "men"),
        ("roster", "women"),
        ("portraits", None),
    ]
    states = refresh.load_json(source_file)
    assert states["asb_games_men"]["status"] == "failed"
    assert states["asb_games_men"]["last_success_at"] == last_success
    assert states["asb_games_women"]["status"] == "succeeded"
    assert states["asb_games_women"]["last_success_at"]
    assert states["asb_portraits"]["status"] == "failed"
    assert states["fiba3x3"]["status"] == "auth_required"


def test_full_preview_does_not_migrate_backup_write_database_or_record_success(
    monkeypatch, database, runtime
):
    monkeypatch.setattr(refresh, "working_tree", lambda _: runtime)
    before = database.read_bytes()
    source_file = database.parent / "sports-sync/sources.json"
    refresh.atomic_json(source_file, {"asb_games_men": {"last_success_at": "old"}})
    old_status = source_file.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview must not apply database writes")

    monkeypatch.setattr(refresh, "daily_backup", forbidden)
    monkeypatch.setattr(refresh, "migrate_if_needed", forbidden)
    monkeypatch.setattr(importer, "apply_import", forbidden)
    monkeypatch.setattr(roster, "apply_sync", forbidden)
    monkeypatch.setattr(refresh, "fetch_games", lambda *args: [fixture_game()])
    monkeypatch.setattr(roster, "collect", lambda **kwargs: [roster_snapshot()])
    for name in ("sync_match_statistics", "sync_standings", "sync_portraits", "sync_fiba"):
        monkeypatch.setattr(refresh, name, lambda *args, **kwargs: {"preview": True})
    report = refresh.run_refresh(database, preview=True)
    assert report["status"] == "succeeded"
    assert report["preview"] is True
    assert database.read_bytes() == before
    assert source_file.read_bytes() == old_status
    assert not (database.parent / "backups").exists()


def test_daily_backups_are_consistent_once_daily_and_keep_only_seven(monkeypatch, database):
    day = [datetime(2026, 11, 1, tzinfo=UTC)]
    monkeypatch.setattr(refresh, "datetime", SimpleNamespace(now=lambda _: day[0]))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE backup_sentinel (value INTEGER)")
        connection.execute("INSERT INTO backup_sentinel VALUES (0)")
    unrelated = database.parent / "backups/sports-sync/other-2020-01-01.db"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"Other database backup")
    for index in range(10):
        day[0] = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=index)
        backup = refresh.daily_backup(database)
        with sqlite3.connect(backup) as connection:
            assert connection.execute("SELECT value FROM backup_sentinel").fetchone() == (index,)
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        connection.close()
        original = backup.read_bytes()
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE backup_sentinel SET value=?", (index + 1,))
        assert refresh.daily_backup(database) == backup
        assert backup.read_bytes() == original
    backups = sorted(unrelated.parent.glob("working-*.db"))
    assert [path.name for path in backups] == [
        f"working-2026-11-{day:02}.db" for day in range(4, 11)
    ]
    assert unrelated.read_bytes() == b"Other database backup"
    assert not list(unrelated.parent.glob("*.tmp"))


def test_source_interval_skips_only_not_due_jobs_without_losing_their_status(
    monkeypatch, database, runtime
):
    monkeypatch.setattr(refresh, "working_tree", lambda _: runtime)
    monkeypatch.setattr(refresh, "daily_backup", lambda _: None)
    monkeypatch.setattr(refresh, "migrate_if_needed", lambda _: None)
    calls = []

    def dataset(kind):
        def run(*args, team, **kwargs):
            calls.append((kind, team))
            return {"updated": 1}

        return run

    monkeypatch.setattr(refresh, "sync_games", dataset("games"))
    monkeypatch.setattr(refresh, "sync_match_statistics", dataset("match_statistics"))
    monkeypatch.setattr(refresh, "sync_standings", dataset("standings"))
    monkeypatch.setattr(refresh, "sync_roster", dataset("roster"))
    monkeypatch.setattr(refresh, "sync_portraits", lambda *args, **kwargs: {"verified": 2})

    def forbidden_fiba(*args, **kwargs):
        raise AssertionError("FIBA source is not due yet")

    monkeypatch.setattr(refresh, "sync_fiba", forbidden_fiba)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    previous = {
        "asb_games_men": {
            "status": "succeeded",
            "next_attempt_at": future,
            "last_success_at": "2026-11-14T10:00:00+00:00",
        },
        "fiba3x3": {"status": "auth_required", "next_attempt_at": future, "last_success_at": None},
    }
    source_file = database.parent / "sports-sync/sources.json"
    refresh.atomic_json(source_file, previous)
    report = refresh.run_refresh(database)
    assert report["status"] == "partial"
    assert ("games", "men") not in calls
    assert ("games", "women") in calls
    states = refresh.load_json(source_file)
    assert states["asb_games_men"] == previous["asb_games_men"]
    assert states["fiba3x3"] == previous["fiba3x3"]
    assert states["asb_games_women"]["next_attempt_at"] > states["asb_games_women"]["finished_at"]


def test_atomic_manifest_replacement_keeps_prior_document_until_complete(tmp_path, monkeypatch):
    destination = tmp_path / "manifest.json"
    refresh.atomic_json(destination, {"state": "last_good"})
    original = Path.replace
    observed = []

    def replace(source, target):
        if target == destination:
            assert refresh.load_json(destination) == {"state": "last_good"}
            assert refresh.load_json(source) == {"state": "new"}
            observed.append(True)
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    refresh.atomic_json(destination, {"state": "new"})
    assert observed == [True]
    assert refresh.load_json(destination) == {"state": "new"}
    assert list(tmp_path.iterdir()) == [destination]


def test_existing_unmigrated_database_triggers_migration(tmp_path, monkeypatch):
    database = tmp_path / "unmigrated.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER)")
    called = []
    monkeypatch.setattr(importer, "migrate", lambda path: called.append(path))
    refresh.migrate_if_needed(database)
    assert called == [database]


def test_fiba_failure_preserves_imported_results_while_success_updates_existing_game(
    monkeypatch, database, runtime
):
    import collect_fiba_games as collector

    watchlist = database.parent / "sports-sync/fiba-watchlist.json"
    refresh.atomic_json(watchlist, {"version": 1, "registrations": []})
    result = importer.asb_result(fixture_game()).model_dump(mode="json")
    result.update(
        source="fiba3x3",
        external_id="verified-fiba-game",
        discipline="3x3",
        club_score=21,
        opponent_score=14,
        source_url="https://play.fiba3x3.com/events/verified-event",
        source_label="FIBA 3x3",
    )
    importer.apply_import(database, [importer.PublicResult.model_validate(result)], [], [])
    manifest = runtime / "docs/results-fiba3x3.json"
    refresh.atomic_json(manifest, {"results": [result]})
    before_database, before_manifest = database.read_bytes(), manifest.read_bytes()
    monkeypatch.setattr(
        collector,
        "collect_watched",
        lambda *args, **kwargs: {"status": "auth_required", "results": []},
    )
    report = refresh.sync_fiba(database, runtime, preview=False)
    assert report["status"] == "auth_required" and report["results"] == 0
    assert database.read_bytes() == before_database
    assert manifest.read_bytes() == before_manifest
    corrected = {**result, "opponent_score": 15}
    coverage = {
        "provider": "fiba3x3",
        "team": "men",
        "season": result["season"],
        "records": 1,
        "status": "partial",
        "source_url": result["source_url"],
        "note": "Only explicitly watched registrations",
    }
    monkeypatch.setattr(
        collector,
        "collect_watched",
        lambda *args, **kwargs: {
            "status": "ok",
            "results": [corrected],
            "registrations": [
                {
                    "registration_id": "watched-registration",
                    "team": "men",
                    "season": result["season"],
                    "status": "ok",
                    "external_ids": [result["external_id"]],
                }
            ],
            "coverage": [coverage],
        },
    )
    report = refresh.sync_fiba(database, runtime, preview=False)
    assert report["counts"]["archive_updated"] == 1
    assert refresh.load_json(manifest)["results"] == [corrected]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT source,club_score,opponent_score FROM archive_results"
        ).fetchone() == ("fiba3x3", 21, 15)
        assert connection.execute("SELECT count(*) FROM matches").fetchone() == (0,)
        assert connection.execute(
            "SELECT status,records,note FROM archive_coverage WHERE provider='fiba3x3'"
        ).fetchone() == ("partial", 1, coverage["note"])
    before_database, before_manifest = database.read_bytes(), manifest.read_bytes()
    monkeypatch.setattr(
        collector,
        "collect_watched",
        lambda *args, **kwargs: {
            "status": "ok",
            "results": [],
            "registrations": [
                {
                    "registration_id": "watched-registration",
                    "team": "men",
                    "season": result["season"],
                    "status": "ok",
                    "external_ids": [],
                }
            ],
            "coverage": [{**coverage, "records": 0}],
        },
    )
    with pytest.raises(ValueError, match="FIBA games disappeared"):
        refresh.sync_fiba(database, runtime, preview=False)
    assert database.read_bytes() == before_database
    assert manifest.read_bytes() == before_manifest
