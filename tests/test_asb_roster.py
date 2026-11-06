import asyncio
import importlib.util
import sqlite3
import sys
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine
from predatory_beavers.modules.roster.models import AsbRosterMember
from predatory_beavers.modules.roster.photos import PHOTO_DIRECTORY, cached_photo
from predatory_beavers.modules.roster.router import current_season, player_read
from predatory_beavers.modules.roster.schemas import RosterMemberInput, RosterSnapshotInput
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "asb_roster_sync", ROOT / "scripts/sync_asb_roster.py"
)
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)


@pytest.fixture
def roster_db(tmp_path: Path) -> tuple[Path, Settings]:
    database = tmp_path / "roster.sqlite"
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///" + database.as_posix(),
        media_storage_path=tmp_path / "media",
        log_json=False,
    )

    async def create() -> None:
        engine = create_engine(settings)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return database, settings


def test_real_roster_is_current_only_idempotent_and_has_no_personal_fields(
    roster_db: tuple[Path, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings = roster_db
    monkeypatch.setattr(
        "predatory_beavers.modules.roster.router.current_season", lambda: "2026-2027"
    )
    snapshots = sync.load_manifest()
    assert len(snapshots) == 2
    assert all(row.season == "2026-2027" and row.is_published for row in snapshots)
    assert [len(row.players) for row in snapshots] == [15, 15]
    first = sync.apply_sync(database, snapshots)
    assert first["members_created"] == 30
    backup = sync.backup_database(database)
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT * FROM asb_roster_members ORDER BY id").fetchall()
        connection.execute("CREATE TABLE editor_sentinel (value TEXT)")
        connection.execute("INSERT INTO editor_sentinel VALUES ('keep unchanged')")
    second = sync.apply_sync(database, snapshots)
    assert second["members_created"] == second["members_updated"] == 0
    assert second["members_unchanged"] == 30
    with sqlite3.connect(database) as connection, sqlite3.connect(backup) as copied:
        assert rows == connection.execute("SELECT * FROM asb_roster_members ORDER BY id").fetchall()
        assert rows == copied.execute("SELECT * FROM asb_roster_members ORDER BY id").fetchall()
        assert (
            connection.execute("SELECT value FROM editor_sentinel").fetchone()[0]
            == "keep unchanged"
        )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/asb-roster/men")
        assert response.status_code == 200, response.text
        roster = response.json()["data"]
        assert roster["season"] == "2026-2027" and roster["is_current_season"] is True
        assert len(roster["players"]) == 15
        assert "статистик" in roster["notice"].lower()
        player = next(player for player in roster["players"] if player["statistics_season"])
        assert player["photo_url"].startswith("/api/v1/asb-roster/men/players/")
        assert player["statistics_season"] <= "2026-2027"
        assert player["statistics_source_url"].startswith(
            "https://asb.infobasket.su/Widget/TeamStats/7433?"
        )
        assert not {"birth_date", "birthday", "age", "contacts"} & set(player)
        detail = client.get(f"/api/v1/asb-roster/men/players/{player['id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["player"] == player
        assert client.get(f"/api/v1/asb-roster/women/players/{player['id']}").status_code == 404
        assert client.get(f"/api/v1/asb-roster/men/players/{uuid4()}").status_code == 404


def test_all_bundled_photos_are_local_correct_portraits_and_current_members_only(
    roster_db: tuple[Path, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings = roster_db
    monkeypatch.setattr(
        "predatory_beavers.modules.roster.router.current_season", lambda: "2026-2027"
    )
    snapshots = sync.load_manifest()
    sync.apply_sync(database, snapshots)
    assert len(list(PHOTO_DIRECTORY.glob("*.webp"))) == 30
    with TestClient(create_app(settings)) as client:
        for snapshot in snapshots:
            roster = client.get(f"/api/v1/asb-roster/{snapshot.team}").json()["data"]
            originals = {player.external_player_id: player for player in snapshot.players}
            for player in roster["players"]:
                original = originals[player["external_player_id"]]
                assert original.photo_url
                local = cached_photo(str(original.photo_url))
                assert local and player["photo_url"].startswith("/api/v1/asb-roster/")
                response = client.get(player["photo_url"])
                assert response.status_code == 200
                assert response.headers["content-type"] == "image/webp"
                assert response.content == local.read_bytes()
                with Image.open(BytesIO(response.content)) as portrait:
                    assert portrait.format == "WEBP"
                    assert portrait.width > 0 and portrait.height > 0
                    assert not portrait.getexif()
                    assert not {"exif", "icc_profile", "xmp"} & set(portrait.info)
        first = client.get("/api/v1/asb-roster/men").json()["data"]["players"][0]
        photo_url = first["photo_url"]
        assert client.get(photo_url.replace("/men/", "/women/")).status_code == 404
        # An arbitrary remote URL in a row cannot turn this endpoint into a proxy,
        # and cannot fall back to a different cached portrait.
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE asb_roster_members SET photo_url=? WHERE id=?",
                ("https://example.org/unverified.jpg", first["id"].replace("-", "")),
            )
        assert client.get(photo_url).status_code == 404
        profile = client.get(f"/api/v1/asb-roster/men/players/{first['id']}").json()["data"]
        assert profile["player"]["photo_url"] is None
        monkeypatch.setattr(
            "predatory_beavers.modules.roster.router.current_season", lambda: "2027-2028"
        )
        assert client.get(roster["players"][0]["photo_url"]).status_code == 404


def test_new_season_never_falls_back_to_old_players_and_withdrawals_are_retained(
    roster_db: tuple[Path, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings = roster_db
    snapshots = sync.load_manifest()
    sync.apply_sync(database, snapshots)
    removed = snapshots[0].players.pop()
    update = sync.apply_sync(database, snapshots)
    assert update["members_withdrawn"] == 1
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT is_listed FROM asb_roster_members WHERE external_player_id=?",
            (removed.external_player_id,),
        ).fetchone()
        assert row and row[0] == 0
        assert connection.execute("SELECT count(*) FROM asb_roster_members").fetchone()[0] == 30
    monkeypatch.setattr(
        "predatory_beavers.modules.roster.router.current_season", lambda: "2027-2028"
    )
    with TestClient(create_app(settings)) as client:
        payload = client.get("/api/v1/asb-roster/men").json()["data"]
        assert payload["players"] == []
        assert payload["status"] == "not_published"
        assert payload["is_current_season"] is False
        latest = client.get("/api/v1/asb-roster/men?selection=latest_published").json()["data"]
        assert len(latest["players"]) == 14
        assert latest["is_current_season"] is False
        assert "2026/2027" in latest["notice"]


def test_normalization_uses_verified_members_and_raw_team_totals_without_private_data() -> None:
    person = {
        "personId": 1,
        "firstName": "Иван",
        "lastName": "Тестовый",
        "secondName": "",
        "personType": "Player",
        "accessStatus": "Access",
        "leaveTeam": False,
        "birthday": "2000-01-01",
        "contacts": "must not survive",
        "photo": None,
    }
    roster = {
        "team": {"id": 7433},
        "players": [person, {**person, "personId": 2, "leaveTeam": True}],
    }
    row = {
        "personId": 1,
        "team": {"id": 7433},
        "games": 3,
        "stats": {"points": 20, "assist": None, "rebound": 0, "second": 2250},
    }
    stats = {"players": [row, {**row, "personId": 999}]}
    source = "https://asb.sportoteka.org/api/abc/teams/stats?id=7433&season=2027"
    members = sync.normalize_members(roster, stats, season="2026-2027", source_url=source)
    assert len(members) == 1
    assert not {"birthday", "contacts"} & set(members[0].model_dump())
    member = AsbRosterMember(id=uuid4(), snapshot_id=uuid4(), **members[0].model_dump(mode="json"))
    player = player_read(member)
    assert player.points_per_game == 6.67
    assert player.assists_per_game is None
    assert player.rebounds_per_game == 0
    assert player.minutes_per_game == 12.5
    assert player.statistics_season == "2026-2027"
    assert player.statistics_source_url == source
    with pytest.raises(ValidationError):
        RosterMemberInput.model_validate({**members[0].model_dump(), "birthday": "2000-01-01"})
    with pytest.raises(ValueError, match="Duplicate"):
        sync.normalize_members(
            roster, {"players": [row, row]}, season="2026-2027", source_url=source
        )
    with pytest.raises(ValueError, match="different team"):
        sync.normalize_members(
            roster,
            {"players": [{**row, "team": {"id": 999}}]},
            season="2026-2027",
            source_url=source,
        )
    unpublished = RosterSnapshotInput(
        team="men",
        season="2026-2027",
        external_team_id="7433",
        competition_id="1415975",
        is_published=False,
        source_url="https://asb.sportoteka.org/api/abc/teams/roster",
        stats_source_url="https://asb.sportoteka.org/api/abc/teams/stats",
        checked_at=datetime.now(UTC),
        players=[],
    )
    assert unpublished.players == []
    assert current_season(date(2026, 6, 30)) == "2025-2026"
    assert current_season(date(2026, 7, 1)) == "2026-2027"


def test_latest_statistics_are_per_current_id_coherent_and_current_has_priority() -> None:
    people = [
        {
            "personId": number,
            "firstName": f"Current {number}",
            "lastName": "Roster",
            "personType": "Player",
            "accessStatus": "Access",
            "leaveTeam": False,
            "photo": None,
        }
        for number in [1, 2, 3, 4]
    ]
    current_source = "https://asb.sportoteka.org/api/abc/teams/stats?id=7433&season=2027"
    members = sync.normalize_members(
        {"team": {"id": 7433}, "players": people},
        {
            "players": [
                {
                    "personId": 1,
                    "team": {"id": 7433},
                    "games": 2,
                    "stats": {"points": 0, "assist": None, "rebound": None, "second": 120},
                },
                {"personId": 2, "team": {"id": 7433}, "games": 1, "stats": None},
                {"personId": 3, "team": {"id": 7433}, "games": 0, "stats": {"points": 0}},
            ]
        },
        season="2026-2027",
        source_url=current_source,
    )
    requested = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "GetTeamSeasons" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {"Year": 2025, "SeasonName": "Сезон 2024/2025", "CompID": 48154},
                    {"Year": 2026, "SeasonName": "Сезон 2025/2026", "CompID": 49695},
                ],
            )
        rows = (
            [
                {
                    "PersonID": 1,
                    "InGameCount": 10,
                    "Points": 999,
                    "Assist": 999,
                    "Rebound": 999,
                    "Seconds": 999,
                },
                {
                    "PersonID": 2,
                    "InGameCount": 3,
                    "GameCount": 8,
                    "Points": 20,
                    "Assist": None,
                    "Rebound": 0,
                    "Seconds": 2250,
                },
                {"PersonID": 3, "InGameCount": 0, "GameCount": 8, "Points": None, "Seconds": 0},
                {
                    "PersonID": 999,
                    "InGameCount": 10,
                    "Points": 999,
                    "Birthday": "private",
                    "FullName": "Old player",
                },
            ]
            if request.url.params["compId"] == "49695"
            else [
                {
                    "PersonID": 2,
                    "InGameCount": 10,
                    "Points": 999,
                    "Assist": 99,
                    "Rebound": 999,
                    "Seconds": 999,
                },
                {
                    "PersonID": 3,
                    "InGameCount": 2,
                    "Points": 4,
                    "Assist": 0,
                    "Rebound": None,
                    "Seconds": 600,
                },
            ]
        )
        return httpx.Response(200, json={"TeamID": 7433, "Players": rows})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        selected = sync.historical_statistics(client, members, 7433, "2026-2027")
    assert len(selected) == 4
    assert [member.full_name for member in selected] == [member.full_name for member in members]
    by_id = {member.external_player_id: member for member in selected}
    assert by_id["1"].statistics_season == "2026-2027"
    assert str(by_id["1"].statistics_source_url) == current_source
    assert by_id["1"].points_total == 0 and by_id["1"].games_played == 2
    assert by_id["1"].assists_total is None
    assert by_id["2"].statistics_season == "2025-2026"
    assert "compId=49695" in str(by_id["2"].statistics_source_url)
    assert by_id["2"].games_played == 3
    assert by_id["2"].points_total == 20 and by_id["2"].assists_total is None
    assert by_id["2"].rebounds_total == 0 and by_id["2"].seconds_total == 2250
    assert by_id["3"].statistics_season == "2024-2025"
    assert "compId=48154" in str(by_id["3"].statistics_source_url)
    assert by_id["3"].games_played == 2 and by_id["3"].assists_total == 0
    assert by_id["4"].statistics_season is None and by_id["4"].games_played is None
    assert len(requested) == 3
    # Order cannot cause older statistics to replace a usable current bundle.
    older = [{"external_player_id": "1", "games_played": 9, "points_total": 100}]
    assert sync.select_statistics(selected, older, "2025-2026", requested[-1])[0] == by_id["1"]

    # Once usable current statistics exist for everyone, historical availability
    # cannot block synchronization and no historical requests are made.
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unnecessary historical lookup: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client:
        assert sync.historical_statistics(client, [by_id["1"]], 7433, "2026-2027") == [by_id["1"]]


@pytest.mark.parametrize("error", ["season", "team", "duplicate"])
def test_historical_statistics_require_verified_season_team_and_unique_ids(error: str) -> None:
    member = RosterMemberInput(
        external_player_id="1",
        full_name="Current member",
        photo_url=None,
        games_played=None,
        points_total=None,
        assists_total=None,
        rebounds_total=None,
        seconds_total=None,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if "GetTeamSeasons" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {
                        "Year": 2026,
                        "SeasonName": "Сезон 2024/2025" if error == "season" else "Сезон 2025/2026",
                        "CompID": 49695,
                    }
                ],
            )
        row = {"PersonID": 1, "InGameCount": 1, "Points": 0}
        return httpx.Response(
            200,
            json={
                "TeamID": 999 if error == "team" else 7433,
                "Players": [row, row] if error == "duplicate" else [row],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(ValueError):
        sync.historical_statistics(client, [member], 7433, "2026-2027")


def test_unattributed_or_incoherent_statistics_are_rejected() -> None:
    base = dict(
        external_player_id="1",
        full_name="Current member",
        photo_url=None,
        games_played=2,
        points_total=0,
        assists_total=None,
        rebounds_total=None,
        seconds_total=None,
    )
    with pytest.raises(ValidationError, match="explicit season and source"):
        RosterMemberInput(**base)
    with pytest.raises(ValidationError, match="public ASB"):
        RosterMemberInput(
            **base, statistics_season="2025-2026", statistics_source_url="https://example.com/stats"
        )
