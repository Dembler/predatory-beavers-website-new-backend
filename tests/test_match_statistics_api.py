"""Public protocol scope, archive/calendar orientation and correction fences."""

import asyncio
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.integrations.asb.statistics import normalize_game_statistics
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.matches.models import (
    ClubSide,
    Competition,
    Match,
    MatchStatistics,
    MatchStatus,
)
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.settings import Settings

ENDPOINT = "/api/v1/game-statistics/asb/1138965"


@pytest.fixture
def statistics_settings(tmp_path):
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///" + (tmp_path / "stats.db").as_posix(),
        media_storage_path=tmp_path / "media",
        log_json=False,
    )
    raw = json.loads(
        (Path(__file__).parent / "fixtures/asb-game-1138965.json").read_text(encoding="utf-8")
    )
    stamp = datetime(2026, 11, 17, tzinfo=UTC)
    data = normalize_game_statistics(
        raw,
        external_id="1138965",
        team="men",
        season="2026-2027",
        fetched_at=stamp,
    )

    async def seed():
        engine = create_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with create_session_factory(engine)() as session:
            team = Team(slug="men", name="ВГУ", category=TeamCategory.MEN)
            competition = Competition(name="АСБ", season="2026-2027")
            session.add_all([team, competition])
            await session.flush()
            session.add(
                Match(
                    source="asb",
                    external_id="1138965",
                    team_id=team.id,
                    competition_id=competition.id,
                    starts_at=stamp,
                    club_side=ClubSide.HOME,
                    home_team_name="ВГУ",
                    away_team_name="ВГПУ",
                    home_score=60,
                    away_score=51,
                    status=MatchStatus.FINISHED,
                )
            )
            session.add(
                ArchiveResult(
                    source="asb",
                    external_id="1138965",
                    discipline="5x5",
                    team="men",
                    season="2026-2027",
                    year=2026,
                    tournament="АСБ",
                    event_date=date(2026, 11, 12),
                    date_precision="day",
                    team_name="ВГУ",
                    opponent_name="ВГПУ",
                    club_score=60,
                    opponent_score=51,
                    status="finished",
                    source_label="АСБ",
                )
            )
            session.add(
                MatchStatistics(
                    source=data.source,
                    external_id=data.external_id,
                    team=data.team,
                    season=data.season,
                    discipline=data.discipline,
                    fetched_at=stamp,
                    payload=data.model_dump(mode="json"),
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())
    return settings


def edit(settings, sql, params=()):
    with sqlite3.connect(settings.database_url.removeprefix("sqlite+aiosqlite:///")) as conn:
        conn.execute(sql, params)


def test_public_calendar_and_archive_share_statistics_source_identity(statistics_settings):
    with TestClient(create_app(statistics_settings)) as client:
        result = client.get(ENDPOINT)
        assert result.status_code == 200, result.text
        data = result.json()["data"]
        assert data["club_side"] == "home"
        assert data["period_scores"][0]["home_score"] == 19
        assert len(data["box_score"]) == 24
        edit(statistics_settings, "DELETE FROM matches")
        assert client.get(ENDPOINT).json()["data"] == data
        edit(statistics_settings, "UPDATE archive_results SET club_score=61")
        assert client.get(ENDPOINT).json()["data"] is None


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE matches SET active=0",
        "UPDATE matches SET is_deleted=1",
        "UPDATE teams SET active=0",
        "UPDATE teams SET is_deleted=1",
        "UPDATE competitions SET active=0",
        "UPDATE competitions SET is_deleted=1",
    ],
)
def test_archive_cannot_bypass_withdrawn_calendar_game(statistics_settings, change):
    edit(statistics_settings, change)
    with TestClient(create_app(statistics_settings)) as client:
        assert client.get(ENDPOINT).status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE matches SET home_score=61",
        "UPDATE matches SET club_side='AWAY'",
        "UPDATE matches SET status='CANCELLED'",
        "UPDATE competitions SET season='2025-2026'",
        "UPDATE teams SET category='WOMEN'",
    ],
)
def test_parent_result_scope_corrections_invalidate_old_protocol(statistics_settings, change):
    edit(statistics_settings, change)
    with TestClient(create_app(statistics_settings)) as client:
        result = client.get(ENDPOINT)
        assert result.status_code == 200
        assert result.json()["data"] is None


def test_known_game_without_protocol_is_null_unknown_game_is_404(statistics_settings):
    edit(statistics_settings, "DELETE FROM match_statistics")
    with TestClient(create_app(statistics_settings)) as client:
        assert client.get(ENDPOINT).json()["data"] is None
        assert client.get("/api/v1/game-statistics/asb/999999").status_code == 404
        assert client.get("/api/v1/game-statistics/other/1138965").status_code == 404


@pytest.mark.parametrize("damage", ["invalid_sum", "identity", "source", "unknown_field"])
def test_invalid_stored_revision_is_never_public(statistics_settings, damage):
    database = statistics_settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM match_statistics").fetchone()[0]
        )
    if damage == "invalid_sum":
        payload["period_scores"][0]["home_score"] = 999
    elif damage == "identity":
        payload["external_id"] = "999999"
    elif damage == "source":
        payload["team"] = "women"
    else:
        payload["raw_private_profile"] = "should not be exposed"
    edit(statistics_settings, "UPDATE match_statistics SET payload=?", (json.dumps(payload),))
    with TestClient(create_app(statistics_settings)) as client:
        assert client.get(ENDPOINT).json()["data"] is None


def test_archive_away_orientation_uses_club_score_not_synthetic_card_home(statistics_settings):
    edit(statistics_settings, "DELETE FROM matches")
    database = statistics_settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM match_statistics").fetchone()[0]
        )
    payload["club_side"] = "away"
    payload["home_external_team_id"] = "7635"
    payload["away_external_team_id"] = "7433"
    edit(statistics_settings, "UPDATE match_statistics SET payload=?", (json.dumps(payload),))
    edit(statistics_settings, "UPDATE archive_results SET club_score=51,opponent_score=60")
    with TestClient(create_app(statistics_settings)) as client:
        result = client.get(ENDPOINT)
        assert result.status_code == 200
        assert result.json()["data"]["club_side"] == "away"
        assert result.json()["data"]["period_scores"][0]["home_score"] == 19


def test_single_game_get_filters_before_loading_and_category_survives_slug_change(
    statistics_settings,
):
    edit(statistics_settings, "UPDATE teams SET slug='new-public-slug'")
    database = statistics_settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO match_statistics (id,created_at,updated_at,source,external_id,team,"
            "season,discipline,payload,fetched_at) SELECT ?,created_at,updated_at,source,?,team,"
            "season,discipline,?,fetched_at FROM match_statistics LIMIT 1",
            ("f" * 32, "999999", json.dumps({"unrelated_large_payload": "x" * 500000})),
        )
    loaded = []

    def track(row, context):
        loaded.append(row.external_id)

    event.listen(MatchStatistics, "load", track)
    try:
        with TestClient(create_app(statistics_settings)) as client:
            response = client.get(ENDPOINT)
            assert response.status_code == 200 and response.json()["data"] is not None
    finally:
        event.remove(MatchStatistics, "load", track)
    assert loaded == ["1138965"]


def public_card(client, route):
    if route == "detail":
        match_id = client.get("/api/v1/matches").json()["items"][0]["id"]
        return client.get(f"/api/v1/matches/{match_id}").json()["data"]
    if route == "home":
        return client.get("/api/v1/home").json()["data"]["recent_results"][0]
    if route == "calendar":
        entry = client.get("/api/v1/calendar?season=all").json()["items"][0]
        return entry["match"] or entry["result"]
    path = "/api/v1/matches" if route == "list" else "/api/v1/results-archive"
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


@pytest.mark.parametrize("route", ["list", "detail", "calendar", "archive", "home"])
def test_cards_include_compact_validated_quarters_without_protocol(statistics_settings, route):
    edit(statistics_settings, "UPDATE matches SET starts_at='2020-01-01 00:00:00.000000'")
    with TestClient(create_app(statistics_settings)) as client:
        card = public_card(client, route)
        scores = card["score_by_periods"]
        assert [(p["club_score"], p["opponent_score"]) for p in scores] == [
            (19, 12),
            (12, 11),
            (17, 16),
            (12, 12),
        ]
        assert [p["number"] for p in scores] == [1, 2, 3, 4]
        assert set(scores[0]) == {"number", "label", "club_score", "opponent_score"}
        assert "box_score" not in card and "player_periods_status" not in card


@pytest.mark.parametrize(
    "change",
    [
        "DELETE FROM match_statistics",
        "UPDATE matches SET home_score=61",
        "UPDATE match_statistics SET payload='{}'",
    ],
)
def test_missing_invalid_or_corrected_protocol_has_empty_card_summary(statistics_settings, change):
    edit(statistics_settings, change)
    with TestClient(create_app(statistics_settings)) as client:
        for route in ("list", "detail", "calendar", "archive"):
            assert public_card(client, route)["score_by_periods"] == []


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE archive_results SET club_score=61",
        "UPDATE archive_results SET discipline='3x3'",
        "UPDATE archive_results SET season='2025-2026'",
        "UPDATE archive_results SET team='women'",
        "UPDATE archive_results SET status='cancelled'",
    ],
)
def test_archive_summary_must_match_its_displayed_scope(statistics_settings, change):
    edit(statistics_settings, change)
    with TestClient(create_app(statistics_settings)) as client:
        assert public_card(client, "archive")["score_by_periods"] == []
        assert len(public_card(client, "list")["score_by_periods"]) == 4


def test_withdrawn_game_keeps_independent_archive_but_never_its_quarters(statistics_settings):
    edit(statistics_settings, "UPDATE matches SET active=0")
    with TestClient(create_app(statistics_settings)) as client:
        assert client.get("/api/v1/matches").json()["items"] == []
        assert public_card(client, "archive")["score_by_periods"] == []
        assert public_card(client, "calendar")["score_by_periods"] == []


@pytest.mark.parametrize("archive_only", [False, True])
def test_away_summary_uses_club_orientation_on_calendar_and_archive(
    statistics_settings, archive_only
):
    database = statistics_settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM match_statistics").fetchone()[0]
        )
    payload["club_side"] = "away"
    payload["home_external_team_id"] = "7635"
    payload["away_external_team_id"] = "7433"
    edit(statistics_settings, "UPDATE match_statistics SET payload=?", (json.dumps(payload),))
    edit(statistics_settings, "UPDATE archive_results SET club_score=51,opponent_score=60")
    edit(statistics_settings, "UPDATE matches SET club_side='AWAY'")
    if archive_only:
        edit(statistics_settings, "DELETE FROM matches")
    with TestClient(create_app(statistics_settings)) as client:
        for route in ("calendar", "archive") if archive_only else ("list", "calendar", "archive"):
            scores = public_card(client, route)["score_by_periods"]
            assert [(p["club_score"], p["opponent_score"]) for p in scores] == [
                (12, 19),
                (11, 12),
                (16, 17),
                (12, 12),
            ]


def test_card_summary_preserves_overtime_as_separate_period(statistics_settings):
    database = statistics_settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM match_statistics").fetchone()[0]
        )
    payload["period_scores"][3]["home_score"] -= 5
    payload["period_scores"][3]["away_score"] -= 4
    payload["period_scores"].append(
        {"number": 5, "kind": "overtime", "label": "ОТ 1", "home_score": 5, "away_score": 4}
    )
    for row in payload["box_score"]:
        row["period_statistics"] = []
    payload["player_periods_status"] = "unavailable"
    edit(statistics_settings, "UPDATE match_statistics SET payload=?", (json.dumps(payload),))
    with TestClient(create_app(statistics_settings)) as client:
        scores = public_card(client, "list")["score_by_periods"]
        assert scores[-1] == {"number": 5, "label": "ОТ 1", "club_score": 5, "opponent_score": 4}
        assert sum(row["club_score"] for row in scores) == 60
        assert sum(row["opponent_score"] for row in scores) == 51


def test_twenty_four_cards_load_protocols_in_one_filtered_batch(statistics_settings):
    database = statistics_settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(matches)")]
        match = connection.execute("SELECT * FROM matches").fetchone()
        protocol = json.loads(
            connection.execute("SELECT payload FROM match_statistics").fetchone()[0]
        )
        for number in range(23):
            external_id = str(2000000 + number)
            values = dict(zip(columns, match, strict=True))
            values.update(id=uuid4().hex, external_id=external_id)
            placeholders = ",".join("?" * len(columns))
            connection.execute(
                f"INSERT INTO matches ({','.join(columns)}) VALUES ({placeholders})",
                list(values.values()),
            )
            payload = {**protocol, "external_id": external_id}
            payload["source_url"] = protocol["source_url"].replace("1138965", external_id)
            connection.execute(
                "INSERT INTO match_statistics (id,created_at,updated_at,source,external_id,team,"
                "season,discipline,payload,fetched_at) SELECT ?,created_at,updated_at,"
                "source,?,team,"
                "season,discipline,?,fetched_at FROM match_statistics LIMIT 1",
                (uuid4().hex, external_id, json.dumps(payload)),
            )
        connection.execute(
            "INSERT INTO match_statistics (id,created_at,updated_at,source,external_id,team,"
            "season,discipline,payload,fetched_at) SELECT ?,created_at,updated_at,source,?,team,"
            "season,discipline,?,fetched_at FROM match_statistics LIMIT 1",
            (uuid4().hex, "999999", json.dumps({"unrelated_large_payload": "x" * 500000})),
        )
    statements = []
    loaded = []

    def query(conn, cursor, statement, parameters, context, executemany):
        if "FROM match_statistics" in statement:
            statements.append(statement)

    def load(row, context):
        loaded.append(row.external_id)

    event.listen(Engine, "before_cursor_execute", query)
    event.listen(MatchStatistics, "load", load)
    try:
        with TestClient(create_app(statistics_settings)) as client:
            response = client.get("/api/v1/matches?page_size=24")
            assert response.status_code == 200, response.text
            cards = response.json()["items"]
            assert len(cards) == 24
            assert all(len(card["score_by_periods"]) == 4 for card in cards)
    finally:
        event.remove(Engine, "before_cursor_execute", query)
        event.remove(MatchStatistics, "load", load)
    assert len(statements) == 1 and " IN " in statements[0]
    assert len(loaded) == 24 and "999999" not in loaded
