from collections.abc import AsyncIterator

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
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.club.repository import TeamRepository
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
    assert await session.scalar(select(func.count(Venue.id))) == 1
    assert await session.scalar(select(func.count(StandingsSnapshot.id))) == 1
    assert await session.scalar(select(func.count(AdminAuditLog.id))) == 2
    actions = list(await session.scalars(select(AdminAuditLog.action)))
    assert actions == [AuditAction.IMPORT, AuditAction.IMPORT]

    jobs, total = await job_repository.list(page=1, page_size=10)
    assert total == 2
    assert all(job.status is ImportStatus.COMPLETED for job in jobs)
