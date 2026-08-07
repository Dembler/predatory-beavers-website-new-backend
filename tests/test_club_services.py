from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.db.base import Base
from predatory_beavers.modules.club.models import Player, Team, TeamCategory
from predatory_beavers.modules.club.repository import PlayerRepository, TeamRepository
from predatory_beavers.modules.club.schemas import PlayerCreate, PlayerUpdate
from predatory_beavers.modules.club.service import PlayerService, TeamService


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
def services(session: AsyncSession) -> tuple[TeamService, PlayerService, TeamRepository]:
    team_repository = TeamRepository(session)
    player_repository = PlayerRepository(session)
    return (
        TeamService(team_repository),
        PlayerService(player_repository, team_repository),
        team_repository,
    )


@pytest.mark.asyncio
async def test_list_players_supports_team_category_and_pagination(
    session: AsyncSession,
    services: tuple[TeamService, PlayerService, TeamRepository],
) -> None:
    team_service, player_service, _ = services
    men = Team(slug="men", name="Predatory Beavers Men", category=TeamCategory.MEN)
    women = Team(slug="women", name="Predatory Beavers Women", category=TeamCategory.WOMEN)
    session.add_all([men, women])
    await session.flush()
    session.add_all(
        [
            Player(team_id=men.id, full_name="Second", sort_order=2),
            Player(team_id=men.id, full_name="First", sort_order=1),
            Player(team_id=women.id, full_name="Women Player", sort_order=1),
            Player(team_id=men.id, full_name="Inactive", sort_order=0, active=False),
        ]
    )
    await session.commit()

    teams, team_total = await team_service.list(page=1, page_size=10, category=TeamCategory.MEN)
    players, total = await player_service.list(page=1, page_size=1, team="men")
    women_players, women_total = await player_service.list(
        page=1, page_size=10, category=TeamCategory.WOMEN
    )

    assert team_total == 1
    assert [team.slug for team in teams] == ["men"]
    assert total == 2
    assert [player.full_name for player in players] == ["First"]
    assert women_total == 1
    assert [player.full_name for player in women_players] == ["Women Player"]


@pytest.mark.asyncio
async def test_create_and_update_player(
    session: AsyncSession,
    services: tuple[TeamService, PlayerService, TeamRepository],
) -> None:
    _, player_service, _ = services
    men = Team(slug="men", name="Men", category=TeamCategory.MEN)
    women = Team(slug="women", name="Women", category=TeamCategory.WOMEN)
    session.add_all([men, women])
    await session.commit()

    created = await player_service.create(
        PlayerCreate(team_id=men.id, full_name="  New Player  ", position="Center")
    )
    updated = await player_service.update(
        created.id,
        PlayerUpdate(team_id=women.id, position="Forward", sort_order=3),
    )

    assert created.full_name == "New Player"
    assert updated.team_id == women.id
    assert updated.team.slug == "women"
    assert updated.position == "Forward"
    assert updated.sort_order == 3


@pytest.mark.asyncio
async def test_create_rejects_missing_team(
    services: tuple[TeamService, PlayerService, TeamRepository],
) -> None:
    _, player_service, _ = services

    with pytest.raises(NotFoundError, match="Team not found"):
        await player_service.create(PlayerCreate(team_id=uuid4(), full_name="No Team"))


@pytest.mark.asyncio
async def test_soft_delete_hides_player_and_second_lookup_is_not_found(
    session: AsyncSession,
    services: tuple[TeamService, PlayerService, TeamRepository],
) -> None:
    _, player_service, _ = services
    team = Team(slug="men", name="Men", category=TeamCategory.MEN)
    session.add(team)
    await session.flush()
    player = Player(team_id=team.id, full_name="Deleted Player")
    session.add(player)
    await session.commit()

    await player_service.delete(player.id)
    listed, total = await player_service.list(page=1, page_size=10, active=None)

    assert total == 0
    assert listed == []
    assert player.is_deleted is True
    assert player.deleted_at is not None
    assert player.active is False
    with pytest.raises(NotFoundError, match="Player not found"):
        await player_service.get(player.id)


@pytest.mark.asyncio
async def test_update_missing_player_is_not_found(
    services: tuple[TeamService, PlayerService, TeamRepository],
) -> None:
    _, player_service, _ = services

    with pytest.raises(NotFoundError, match="Player not found"):
        await player_service.update(uuid4(), PlayerUpdate(position="Guard"))


@pytest.mark.asyncio
async def test_public_queries_hide_inactive_players_and_teams(
    session: AsyncSession,
    services: tuple[TeamService, PlayerService, TeamRepository],
) -> None:
    _, player_service, _ = services
    active_team = Team(slug="men", name="Men", category=TeamCategory.MEN)
    inactive_team = Team(
        slug="women",
        name="Women",
        category=TeamCategory.WOMEN,
        active=False,
    )
    session.add_all([active_team, inactive_team])
    await session.flush()
    inactive_player = Player(
        team_id=active_team.id,
        full_name="Hidden Player",
        active=False,
    )
    hidden_team_player = Player(
        team_id=inactive_team.id,
        full_name="Hidden Team Player",
        active=True,
    )
    session.add_all([inactive_player, hidden_team_player])
    await session.commit()

    listed, total = await player_service.list(page=1, page_size=10, active=True)

    assert listed == []
    assert total == 0
    with pytest.raises(NotFoundError, match="Player not found"):
        await player_service.get_public(inactive_player.id)
    with pytest.raises(NotFoundError, match="Player not found"):
        await player_service.get_public(hidden_team_player.id)
