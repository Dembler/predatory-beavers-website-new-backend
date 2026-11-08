import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.matches.models import ClubSide, Competition, Match, MatchStatus
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.settings import Settings

AT = datetime(2026, 11, 7, 12, tzinfo=UTC)


def archive(external_id: str, **overrides: object) -> ArchiveResult:
    values: dict[str, object] = {
        "source": "asb",
        "external_id": external_id,
        "discipline": "5x5",
        "team": "men",
        "season": "2025-2026",
        "year": 2026,
        "tournament": "Архивный турнир",
        "event_date": date(2026, 2, 1),
        "date_precision": "day",
        "team_name": "ВГУ",
        "opponent_name": "Архивный соперник",
        "club_score": 80,
        "opponent_score": 70,
        "status": "finished",
        "source_label": "АСБ",
    }
    values.update(overrides)
    return ArchiveResult(**values)


def match(
    external_id: str,
    team: Team,
    competition: Competition,
    **overrides: object,
) -> Match:
    values: dict[str, object] = {
        "source": "asb",
        "external_id": external_id,
        "team_id": team.id,
        "competition_id": competition.id,
        "starts_at": AT - timedelta(days=10),
        "club_side": ClubSide.HOME,
        "home_team_name": "Отредактированный ВГУ",
        "away_team_name": "Соперник",
        "home_score": 90,
        "away_score": 70,
        "status": MatchStatus.FINISHED,
    }
    values.update(overrides)
    return Match(**values)


@pytest.fixture
def calendar_settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        database_url="sqlite+aiosqlite:///" + (tmp_path / "calendar.sqlite").as_posix(),
        media_storage_path=tmp_path / "media",
        log_json=False,
    )


def seed(
    settings: Settings, populate: Callable[[AsyncSession, Team, Team, Competition], None]
) -> None:
    async def run() -> None:
        engine = create_engine(settings)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with create_session_factory(engine)() as session:
            men = Team(slug="men", name="ВГУ", category=TeamCategory.MEN)
            women = Team(slug="women", name="ВГУ девушки", category=TeamCategory.WOMEN)
            competition = Competition(name="Новый сезон", season="2026 / 2027")
            session.add_all([men, women, competition])
            await session.flush()
            populate(session, men, women, competition)
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_feed_pages_deduplicates_and_preserves_calendar_edits(calendar_settings: Settings) -> None:
    def populate(session: AsyncSession, men: Team, women: Team, competition: Competition) -> None:
        session.add(match("same", men, competition))
        session.add(archive("same", tournament="Старая копия"))
        session.add(archive("tournament-only", opponent_name=None))
        session.add_all([archive(f"history-{index}") for index in range(50)])

    seed(calendar_settings, populate)
    with TestClient(create_app(calendar_settings)) as client:
        params = {"season": "all", "at": AT.isoformat()}
        loaded_archive_ids = []

        def track_loaded(row: ArchiveResult, context: object) -> None:
            loaded_archive_ids.append(row.id)

        event.listen(ArchiveResult, "load", track_loaded)
        try:
            first = client.get("/api/v1/calendar", params=params)
        finally:
            event.remove(ArchiveResult, "load", track_loaded)
        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload["total"] == 51
        assert payload["total_pages"] == 3
        assert len(payload["items"]) == 24
        assert len(loaded_archive_ids) == 23  # One calendar + 23 archive ORM rows, never all 50.
        assert payload["counts"] == {"upcoming": 0, "changed": 0, "results": 51, "awaiting": 0}
        assert payload["items"][0]["match"]["home_team_name"] == "Отредактированный ВГУ"
        all_items = payload["items"]
        for page in (2, 3):
            all_items += client.get("/api/v1/calendar", params={**params, "page": page}).json()[
                "items"
            ]
        assert len(all_items) == 51
        assert (
            len({(item["kind"], (item["match"] or item["result"])["id"]) for item in all_items})
            == 51
        )
        assert not any(
            item["result"] and item["result"]["external_id"] == "same" for item in all_items
        )
        last_page = client.get("/api/v1/calendar", params={**params, "page": 9}).json()
        assert last_page["page"] == 3 and len(last_page["items"]) == 3
        assert client.get("/api/v1/calendar?page_size=25").status_code == 422


def test_status_sections_time_boundary_and_defaults(calendar_settings: Settings) -> None:
    def populate(session: AsyncSession, men: Team, women: Team, competition: Competition) -> None:
        for external_id, status, offset in (
            ("later", MatchStatus.SCHEDULED, 2),
            ("live", MatchStatus.LIVE, -2),
            ("boundary", MatchStatus.SCHEDULED, 0),
            ("past", MatchStatus.SCHEDULED, -1),
            ("cancelled", MatchStatus.CANCELLED, -4),
        ):
            session.add(
                match(
                    external_id,
                    men,
                    competition,
                    starts_at=AT + timedelta(days=offset),
                    status=status,
                    home_score=None,
                    away_score=None,
                )
            )
        session.add(
            archive("unpublished", status="unpublished", club_score=None, opponent_score=None)
        )
        session.add(archive("women-3x3", team="women", discipline="3x3", season="2024-2025"))

    seed(calendar_settings, populate)
    with TestClient(create_app(calendar_settings)) as client:
        response = client.get("/api/v1/calendar", params={"season": "all", "at": AT.isoformat()})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["default_season"] == "2026-2027"
        assert payload["counts"] == {"upcoming": 3, "changed": 1, "results": 1, "awaiting": 2}
        assert [row["match"]["external_id"] for row in payload["items"][:3]] == [
            "live",
            "boundary",
            "later",
        ]
        scheduled = client.get(
            "/api/v1/calendar", params={"status": "scheduled", "at": AT.isoformat()}
        ).json()
        assert scheduled["total"] == 3
        assert scheduled["counts"]["awaiting"] == 1
        # The same timestamp makes equality deterministic across requests.
        later = client.get(
            "/api/v1/calendar",
            params={"season": "all", "at": (AT + timedelta(seconds=1)).isoformat()},
        ).json()
        assert later["counts"]["upcoming"] == 2
        assert later["counts"]["awaiting"] == 3
        women = client.get("/api/v1/calendar?team=women&discipline=3x3").json()
        assert women["total"] == 1
        assert women["default_season"] == "2024-2025"
        assert women["seasons"] == ["2024-2025"]
        assert client.get("/api/v1/calendar?at=2026-11-07T12:00:00").status_code == 422


def test_ambiguous_years_moscow_boundary_and_no_invented_source_dates(
    calendar_settings: Settings,
) -> None:
    def populate(session: AsyncSession, men: Team, women: Team, competition: Competition) -> None:
        competition.season = "2026"
        session.add(
            match("july", men, competition, starts_at=datetime(2026, 6, 30, 21, 0, tzinfo=UTC))
        )
        session.add(
            archive("year-only", season="2023", year=2023, event_date=None, date_precision="year")
        )
        session.add(archive("dated", season="2022", year=2022, event_date=date(2022, 3, 1)))

    seed(calendar_settings, populate)
    with TestClient(create_app(calendar_settings)) as client:
        for season in ("2022-2023", "2023-2024"):
            response = client.get("/api/v1/calendar", params={"season": season}).json()
            year_only = [
                row
                for row in response["items"]
                if row["result"] and row["result"]["external_id"] == "year-only"
            ]
            assert len(year_only) == 1
            assert year_only[0]["season"] == season
            assert year_only[0]["result"]["event_date"] is None
        all_rows = client.get("/api/v1/calendar?season=all").json()["items"]
        assert len(all_rows) == 3
        assert next(row for row in all_rows if row["match"])["season"] == "2026-2027"
        assert (
            next(
                row
                for row in all_rows
                if row["result"] and row["result"]["external_id"] == "year-only"
            )["season"]
            == "unknown"
        )
        dated = client.get("/api/v1/calendar?season=2021-2022").json()
        assert dated["total"] == 1


def test_hidden_calendar_does_not_suppress_independent_archive(calendar_settings: Settings) -> None:
    def populate(session: AsyncSession, men: Team, women: Team, competition: Competition) -> None:
        hidden = Competition(name="Не публиковать", season="2026-2027", active=False)
        session.add(hidden)
        # UUID defaults are assigned at flush. Use relationship assignment for
        # this parent so the fixture remains synchronous and database-neutral.
        row = match("hidden", men, competition)
        row.competition = hidden
        session.add(row)
        session.add(match("deleted", men, competition, is_deleted=True))
        session.add(match("draft", men, competition, active=False))
        session.add_all([archive("hidden"), archive("deleted"), archive("draft")])

    seed(calendar_settings, populate)
    with TestClient(create_app(calendar_settings)) as client:
        payload = client.get("/api/v1/calendar?season=all").json()
        assert payload["total"] == 3
        assert all(item["kind"] == "archive" for item in payload["items"])


def test_empty_feed_and_invalid_filters(calendar_settings: Settings) -> None:
    seed(calendar_settings, lambda *_: None)
    with TestClient(create_app(calendar_settings)) as client:
        response = client.get("/api/v1/calendar")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["items"] == [] and data["total"] == 0 and data["total_pages"] == 0
        assert data["default_season"] == data["selected_season"] == "all"
        for params in ({"team": "bad"}, {"status": "bad"}, {"page": 0}, {"season": "2026"}):
            assert client.get("/api/v1/calendar", params=params).status_code == 422
