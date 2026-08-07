from uuid import UUID

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.modules.club.models import Player, Team, TeamCategory
from predatory_beavers.modules.club.repository import PlayerRepository, TeamRepository
from predatory_beavers.modules.club.schemas import PlayerCreate, PlayerUpdate


class TeamService:
    def __init__(self, repository: TeamRepository) -> None:
        self._repository = repository

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        category: TeamCategory | None = None,
        active: bool | None = True,
    ) -> tuple[list[Team], int]:
        return await self._repository.list(
            page=page,
            page_size=page_size,
            category=category,
            active=active,
        )


class PlayerService:
    def __init__(
        self,
        repository: PlayerRepository,
        team_repository: TeamRepository,
    ) -> None:
        self._repository = repository
        self._team_repository = team_repository

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        team: str | None = None,
        category: TeamCategory | None = None,
        active: bool | None = True,
    ) -> tuple[list[Player], int]:
        return await self._repository.list(
            page=page,
            page_size=page_size,
            team=team,
            category=category,
            active=active,
        )

    async def get(self, player_id: UUID) -> Player:
        player = await self._repository.get(player_id)
        if player is None:
            raise NotFoundError("Player not found")
        return player

    async def get_public(self, player_id: UUID) -> Player:
        player = await self._repository.get(player_id, public_only=True)
        if player is None:
            raise NotFoundError("Player not found")
        return player

    async def create(self, payload: PlayerCreate) -> Player:
        await self._require_team(payload.team_id)
        player = Player(**payload.model_dump())
        return await self._repository.add(player)

    async def update(self, player_id: UUID, payload: PlayerUpdate) -> Player:
        player = await self.get(player_id)
        changes = payload.model_dump(exclude_unset=True)
        team_id = changes.get("team_id")
        if team_id is not None and team_id != player.team_id:
            await self._require_team(team_id)

        for field, value in changes.items():
            setattr(player, field, value)
        return await self._repository.save(player)

    async def delete(self, player_id: UUID) -> None:
        player = await self.get(player_id)
        await self._repository.soft_delete(player)

    async def _require_team(self, team_id: UUID) -> Team:
        team = await self._team_repository.get(team_id)
        if team is None:
            raise NotFoundError("Team not found")
        return team
