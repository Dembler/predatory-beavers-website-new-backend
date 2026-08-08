from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.db.base import Base
from predatory_beavers.db.uow import SqlAlchemyUnitOfWork
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.matches.models import ClubSide, MatchStatus
from predatory_beavers.modules.matches.repository import (
    CompetitionRepository,
    MatchRepository,
    VenueRepository,
)
from predatory_beavers.modules.matches.schemas import (
    CompetitionCreate,
    MatchCreate,
    MatchUpdate,
    VenueCreate,
)
from predatory_beavers.modules.matches.service import (
    CompetitionService,
    MatchService,
    VenueService,
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


@pytest.fixture
def services(
    session: AsyncSession,
) -> tuple[CompetitionService, VenueService, MatchService]:
    unit_of_work = SqlAlchemyUnitOfWork(session)
    competition_repository = CompetitionRepository(session)
    venue_repository = VenueRepository(session)
    return (
        CompetitionService(competition_repository, unit_of_work),
        VenueService(venue_repository, unit_of_work),
        MatchService(
            MatchRepository(session),
            TeamRepository(session),
            competition_repository,
            venue_repository,
            unit_of_work,
        ),
    )


@pytest.mark.asyncio
async def test_match_lifecycle_and_public_filters(
    session: AsyncSession,
    services: tuple[CompetitionService, VenueService, MatchService],
) -> None:
    competition_service, venue_service, match_service = services
    team = Team(slug="men", name="Хищные Бобры", category=TeamCategory.MEN)
    session.add(team)
    await session.commit()
    competition = await competition_service.create(
        CompetitionCreate(name="Московская баскетбольная лига", season="2026-2027")
    )
    venue = await venue_service.create(
        VenueCreate(
            name="Спортивный зал",
            address="Москва, Спортивная улица, 1",
            latitude=55.75,
            longitude=37.61,
        )
    )
    match = await match_service.create(
        MatchCreate(
            team_id=team.id,
            competition_id=competition.id,
            venue_id=venue.id,
            starts_at=datetime(2026, 9, 15, 15, 30, tzinfo=UTC),
            club_side=ClubSide.HOME,
            home_team_name="Хищные Бобры",
            away_team_name="Соперники",
        )
    )

    listed, total = await match_service.list(
        page=1,
        page_size=10,
        team="men",
        status=MatchStatus.SCHEDULED,
    )
    assert total == 1
    assert listed[0].id == match.id
    assert listed[0].starts_at.tzinfo is not None
    assert listed[0].competition.name == "Московская баскетбольная лига"
    assert listed[0].venue is not None

    finished = await match_service.update(
        match.id,
        MatchUpdate(status=MatchStatus.FINISHED, home_score=82, away_score=77),
    )
    assert finished.status is MatchStatus.FINISHED
    assert finished.home_score == 82
    assert finished.away_score == 77

    await match_service.delete(match.id)
    listed_after_delete, total_after_delete = await match_service.list(
        page=1,
        page_size=10,
    )
    assert listed_after_delete == []
    assert total_after_delete == 0
    with pytest.raises(NotFoundError, match="Match not found"):
        await match_service.get(match.id)


@pytest.mark.asyncio
async def test_external_identities_are_unique(
    services: tuple[CompetitionService, VenueService, MatchService],
) -> None:
    competition_service, _, _ = services
    payload = CompetitionCreate(
        name="АСБ",
        season="2026-2027",
        source="asb",
        external_id="48154",
    )
    await competition_service.create(payload)

    with pytest.raises(ConflictError, match="already exists"):
        await competition_service.create(payload)


def test_match_and_venue_input_invariants() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        MatchCreate(
            team_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # type: ignore[arg-type]
            competition_id="6ba7b810-9dad-11d1-80b4-00c04fd430c9",  # type: ignore[arg-type]
            starts_at=datetime(2026, 9, 15, 18, 30),
            club_side=ClubSide.HOME,
            home_team_name="Хищные Бобры",
            away_team_name="Соперники",
        )
    with pytest.raises(ValidationError, match="provided together"):
        VenueCreate(
            name="Зал",
            address="Москва",
            latitude=55.75,
        )
