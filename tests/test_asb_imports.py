from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from predatory_beavers.db.base import Base
from predatory_beavers.db.uow import SqlAlchemyUnitOfWork
from predatory_beavers.integrations.asb.client import (
    AsbImportBundle,
    HttpAsbClient,
)
from predatory_beavers.integrations.asb.errors import (
    AsbIdentifierNotAllowedError,
    AsbInvalidResponseError,
    AsbUpstreamError,
)
from predatory_beavers.integrations.asb.schemas import AsbGame, AsbStanding
from predatory_beavers.modules.audit.models import AdminAuditLog, AuditAction
from predatory_beavers.modules.audit.repository import AuditRepository
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.auth import models as auth_models  # noqa: F401
from predatory_beavers.modules.club.models import MediaAsset, Team, TeamCategory
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.imports.asb_applier import AsbImportApplier
from predatory_beavers.modules.imports.models import ImportStatus
from predatory_beavers.modules.imports.repository import ImportJobRepository
from predatory_beavers.modules.imports.schemas import AsbImportRequest
from predatory_beavers.modules.imports.service import ImportService
from predatory_beavers.modules.matches.models import Match, Venue
from predatory_beavers.modules.matches.repository import (
    CompetitionRepository,
    MatchRepository,
    VenueRepository,
)
from predatory_beavers.modules.matches.schemas import MatchRead
from predatory_beavers.modules.standings.models import StandingsSnapshot
from predatory_beavers.modules.standings.repository import StandingsRepository
from predatory_beavers.settings import Settings


def _game_payload(*, game_id: int = 865380, arena_id: int = 12371) -> dict[str, object]:
    return {
        "GameID": game_id,
        "GameDateTimeMoscow": "/Date(1731947400000)/",
        "GameStatus": 1,
        "TeamAid": 7635,
        "TeamBid": 7433,
        "TeamNameAru": "ВГПУ (Воронеж)",
        "TeamNameBru": "ВГУ (Воронеж)",
        "TeamLogoA": "https://files.infobasket.su/Logos/16494.png",
        "TeamLogoB": "https://files.infobasket.su/Logos/17024.png",
        "ScoreA": 49,
        "ScoreB": 61,
        "ArenaId": arena_id,
        "ArenaRu": "С/З ВГУ",
        "CompNameRu": "Круговой турнир",
    }


def _standing_payload(
    *, position: int, team_id: int, team_name: str, wins: int, losses: int
) -> dict[str, object]:
    return {
        "CompID": 48637,
        "TeamID": team_id,
        "Place": position,
        "CompTeamName": {"CompTeamNameRu": team_name},
        "Standings": {
            "StandingGame": wins + losses,
            "StandingWin": wins,
            "StandingDraw": None,
            "StandingLose": losses,
            "StandingPoints": wins * 2 + losses,
            "StandingGoalPlus": 150,
            "StandingGoalMinus": 120,
        },
    }


def _asb_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "cookie_secure": False,
        "log_json": False,
        "asb_enabled": True,
        "asb_allowed_competition_ids": ["48154", "48637"],
        "asb_allowed_team_ids": ["7433"],
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_asb_http_client_builds_fixed_urls_and_validates_payload() -> None:
    games = [_game_payload()]
    standings = [
        _standing_payload(
            position=1,
            team_id=7433,
            team_name="ВГУ",
            wins=2,
            losses=0,
        ),
        _standing_payload(
            position=2,
            team_id=7635,
            team_name="ВГПУ",
            wins=1,
            losses=1,
        ),
    ]
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "asb.infobasket.su"
        requested_paths.append(request.url.path)
        if request.url.path == "/Widget/TeamGames/7433":
            assert request.url.params["compId"] == "48154"
            return httpx.Response(200, json=games)
        if request.url.path == "/Widget/CompTeamResults/48637":
            return httpx.Response(200, json=standings)
        return httpx.Response(404, json={"error": "unexpected path"})

    client = HttpAsbClient(
        _asb_settings(),
        transport=httpx.MockTransport(handler),
    )
    bundle = await client.fetch_import(
        competition_id="48154",
        standings_competition_id="48637",
        external_team_id="7433",
    )

    assert len(bundle.games) == 1
    assert bundle.games[0].game_id == 865380
    assert str(bundle.games[0].home_team_logo_url) == (
        "https://files.infobasket.su/Logos/16494.png"
    )
    assert str(bundle.games[0].away_team_logo_url) == (
        "https://files.infobasket.su/Logos/17024.png"
    )
    assert [row.position for row in bundle.standings] == [1, 2]
    assert requested_paths == [
        "/Widget/TeamGames/7433",
        "/Widget/CompTeamResults/48637",
    ]

    with pytest.raises(AsbIdentifierNotAllowedError):
        await client.fetch_import(
            competition_id="48154",
            standings_competition_id="48637",
            external_team_id="999999",
        )


@pytest.mark.asyncio
async def test_asb_http_client_rejects_redirects_and_non_json() -> None:
    redirect_client = HttpAsbClient(
        _asb_settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "https://example.com"})
        ),
    )
    with pytest.raises(AsbUpstreamError, match="redirects"):
        await redirect_client.fetch_import(
            competition_id="48154",
            standings_competition_id="48637",
            external_team_id="7433",
        )

    html_client = HttpAsbClient(
        _asb_settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="<html>error</html>",
                headers={"Content-Type": "text/html"},
            )
        ),
    )
    with pytest.raises(AsbInvalidResponseError, match="non-JSON"):
        await html_client.fetch_import(
            competition_id="48154",
            standings_competition_id="48637",
            external_team_id="7433",
        )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


class FakeAsbClient:
    def __init__(self, bundle: AsbImportBundle) -> None:
        self._bundle = bundle

    async def fetch_import(
        self,
        *,
        competition_id: str,
        standings_competition_id: str,
        external_team_id: str,
    ) -> AsbImportBundle:
        assert competition_id == "48154"
        assert standings_competition_id == "48637"
        assert external_team_id == "7433"
        return self._bundle


@pytest.mark.asyncio
async def test_asb_import_is_idempotent_and_publishes_standings(
    session: AsyncSession,
) -> None:
    session.add(
        Team(
            slug="men",
            name="Хищные Бобры",
            category=TeamCategory.MEN,
            active=True,
        )
    )
    await session.commit()

    bundle = AsbImportBundle(
        games=[
            AsbGame.model_validate(_game_payload()),
            AsbGame.model_validate(_game_payload(game_id=865381)),
        ],
        standings=[
            AsbStanding.model_validate(
                _standing_payload(
                    position=1,
                    team_id=7433,
                    team_name="ВГУ",
                    wins=2,
                    losses=0,
                )
            ),
            AsbStanding.model_validate(
                _standing_payload(
                    position=2,
                    team_id=7635,
                    team_name="ВГПУ",
                    wins=1,
                    losses=1,
                )
            ),
        ],
    )
    uow = SqlAlchemyUnitOfWork(session)
    job_repository = ImportJobRepository(session)
    service = ImportService(
        job_repository,
        TeamRepository(session),
        CompetitionRepository(session),
        VenueRepository(session),
        MatchRepository(session),
        StandingsRepository(session),
        FakeAsbClient(bundle),
        AuditService(AuditRepository(session)),
        uow,
    )
    payload = AsbImportRequest(
        team="men",
        competition_id="48154",
        standings_competition_id="48637",
        external_team_id="7433",
        season="2024-2025",
    )

    first = await service.import_asb(payload)
    assert first.status is ImportStatus.COMPLETED
    assert first.result == {
        "games_received": 2,
        "standings_rows_received": 2,
        "matches_created": 2,
        "matches_updated": 0,
        "matches_unchanged": 0,
        "venues_created": 1,
        "standings_published": True,
    }

    second = await service.import_asb(payload)
    assert second.status is ImportStatus.COMPLETED
    assert second.result is not None
    assert second.result["matches_created"] == 0
    assert second.result["matches_unchanged"] == 2
    assert second.result["venues_created"] == 0
    assert second.result["standings_published"] is False

    assert await session.scalar(select(func.count(Match.id))) == 2
    match = await MatchRepository(session).get_by_external_identity("asb", "865380")
    assert match is not None
    assert match.home_logo_url == "https://files.infobasket.su/Logos/16494.png"
    assert match.away_logo_url == "https://files.infobasket.su/Logos/17024.png"
    match_read = MatchRead.model_validate(match).model_dump(mode="json")
    assert match_read["home_logo_url"] == match.home_logo_url
    assert match_read["away_logo_url"] == match.away_logo_url
    assert match_read["home_logo_asset_id"] is None
    assert match_read["away_logo_asset_id"] is None
    assert await session.scalar(select(func.count(Venue.id))) == 1
    assert await session.scalar(select(func.count(StandingsSnapshot.id))) == 1
    snapshot = await session.scalar(select(StandingsSnapshot))
    assert snapshot is not None
    assert snapshot.rows[0]["team_logo_url"] == "https://files.infobasket.su/Logos/17024.png"
    assert snapshot.rows[1]["team_logo_url"] == "https://files.infobasket.su/Logos/16494.png"
    assert snapshot.rows[0]["points_for"] == 150
    assert snapshot.rows[0]["points_against"] == 120
    assert snapshot.rows[0]["points_difference"] == 30
    assert await session.scalar(select(func.count(AdminAuditLog.id))) == 2
    actions = list(await session.scalars(select(AdminAuditLog.action)))
    assert actions == [AuditAction.IMPORT, AuditAction.IMPORT]

    jobs, total = await job_repository.list(page=1, page_size=10)
    assert total == 2
    assert all(job.status is ImportStatus.COMPLETED for job in jobs)

    uploaded_logo = MediaAsset(
        storage_key="manual-logo.png", mime="image/png", size=100, checksum="manual-logo"
    )
    session.add(uploaded_logo)
    await session.flush()
    match.home_logo_asset_id = uploaded_logo.id
    match.away_logo_asset_id = uploaded_logo.id
    await session.commit()
    bundle.games[0].home_team_logo_url = None
    bundle.games[0].away_team_logo_url = None
    changed_payload = _game_payload(game_id=865381)
    changed_payload["TeamLogoA"] = "https://files.infobasket.su/Logos/changed.png"
    bundle.games[1] = AsbGame.model_validate(changed_payload)
    third = await service.import_asb(payload)
    assert third.result is not None
    assert third.result["matches_unchanged"] == 1
    assert third.result["matches_updated"] == 1
    preserved = await MatchRepository(session).get_by_external_identity("asb", "865380")
    assert preserved is not None
    assert preserved.home_logo_url == "https://files.infobasket.su/Logos/16494.png"
    assert preserved.away_logo_url == "https://files.infobasket.su/Logos/17024.png"
    assert preserved.home_logo_asset_id == uploaded_logo.id
    assert preserved.away_logo_asset_id == uploaded_logo.id
    updated = await MatchRepository(session).get_by_external_identity("asb", "865381")
    assert updated is not None
    assert updated.home_logo_url == "https://files.infobasket.su/Logos/changed.png"


@pytest.mark.parametrize("logo", [None, "", "   "])
def test_asb_games_accept_missing_logos(logo: str | None) -> None:
    payload = _game_payload()
    payload["TeamLogoA"] = logo
    payload.pop("TeamLogoB")
    game = AsbGame.model_validate(payload)
    assert game.home_team_logo_url is None
    assert game.away_team_logo_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_source", "current_reference"),
    [("asb", "48637"), ("manual", "48637"), ("asb", "48638")],
)
async def test_asb_republication_preserves_reviewed_stage_metadata(
    session: AsyncSession,
    current_source: str,
    current_reference: str,
) -> None:
    session.add(Team(slug="men", name="ВГУ", category=TeamCategory.MEN, active=True))
    await session.commit()
    applier = AsbImportApplier(
        team_repository=TeamRepository(session),
        competition_repository=CompetitionRepository(session),
        venue_repository=VenueRepository(session),
        match_repository=MatchRepository(session),
        standings_repository=StandingsRepository(session),
    )
    payload = AsbImportRequest(
        team="men",
        competition_id="48154",
        standings_competition_id="48637",
        external_team_id="7433",
        season="2024-2025",
    )
    bundle = AsbImportBundle(
        games=[],
        standings=[
            AsbStanding.model_validate(
                _standing_payload(position=1, team_id=7433, team_name="ВГУ", wins=2, losses=0)
            )
        ],
    )
    assert (await applier.apply(payload, bundle))["standings_published"] is True
    await session.commit()
    first = await session.scalar(select(StandingsSnapshot))
    assert first is not None
    assert first.source_url is None
    assert first.last_game_at is None
    assert first.stage_order == 0
    assert first.tournament_id is None
    assert first.tournament_name is None
    assert first.stage_name is None
    assert first.stage_kind == "other"
    assert first.games == []

    metadata: dict[str, object] = {
        "source_url": "https://asb.infobasket.su/Widget/CompTeamResults/48637?format=json",
        "last_game_at": datetime(2025, 2, 19, tzinfo=UTC),
        "stage_order": 314,
        "tournament_id": "48340",
        "tournament_name": "Воронежская область",
        "stage_name": "Круговой турнир",
        "stage_kind": "group",
        "games": [
            {
                "external_id": "884990",
                "round_name": "Круговой турнир",
                "home_team_name": "ВГУ",
                "home_team_id": "7433",
                "away_team_name": "ВГПУ",
                "away_team_id": "7635",
                "home_score": 61,
                "away_score": 49,
                "status": "finished",
                "source_url": (
                    "https://asb.infobasket.su/Widget/CompTeamResults/48637?format=json"
                ),
            }
        ],
    }
    empty_metadata = {field: getattr(first, field) for field in metadata}
    for field, value in metadata.items():
        setattr(first, field, value)
    first.source = current_source
    first.source_reference = current_reference
    await session.commit()
    bundle.standings[0] = AsbStanding.model_validate(
        _standing_payload(position=1, team_id=7433, team_name="ВГУ", wins=3, losses=0)
    )

    assert (await applier.apply(payload, bundle))["standings_published"] is True
    await session.commit()
    current = await session.scalar(select(StandingsSnapshot).where(StandingsSnapshot.is_current))
    assert current is not None
    assert current.id != first.id
    assert first.is_current is False
    assert current.rows[0]["wins"] == 3
    assert current.source == "asb"
    assert current.source_reference == payload.resolved_standings_competition_id
    assert first.rows[0]["wins"] == 2
    expected_metadata = (
        metadata
        if current_reference == payload.resolved_standings_competition_id
        else empty_metadata
    )
    assert all(getattr(current, field) == value for field, value in expected_metadata.items())
    assert await session.scalar(select(func.count(StandingsSnapshot.id))) == 2
