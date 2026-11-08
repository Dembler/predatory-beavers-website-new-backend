from datetime import datetime
from uuid import UUID

from predatory_beavers.api.concurrency import require_version
from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.api.publication import PublicationDependencyError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.models import Player, Team, TeamCategory
from predatory_beavers.modules.club.repository import PlayerRepository, TeamRepository
from predatory_beavers.modules.club.schemas import (
    PlayerCreate,
    PlayerRead,
    PlayerUpdate,
    TeamRead,
    TeamUpdate,
)


class TeamService:
    def __init__(
        self, repository: TeamRepository, unit_of_work: UnitOfWork, audit_service: AuditService
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._audit = audit_service

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
        category: TeamCategory | None = None,
        active: bool | None = True,
    ) -> tuple[list[Team], int]:
        return await self._repository.list(
            page=page,
            page_size=page_size,
            q=q,
            category=category,
            active=active,
        )

    async def get(self, team_id: UUID) -> Team:
        team = await self._repository.get(team_id)
        if team is None:
            raise NotFoundError("Team not found")
        return team

    async def update(self, team_id: UUID, payload: TeamUpdate) -> Team:
        async with self._unit_of_work:
            team = await self.get(team_id)
            require_version(team.updated_at, payload.expected_updated_at)
            before = TeamRead.model_validate(team).model_dump(mode="json")
            if "logo_asset_id" in payload.model_fields_set:
                await self._repository.require_media(payload.logo_asset_id)
            for field, value in payload.model_dump(exclude_unset=True).items():
                setattr(team, field, value)
            team = await self._repository.save(team)
            await self._audit.record(
                action=AuditAction.UPDATE,
                entity_type="team",
                entity_id=team.id,
                before=before,
                after=TeamRead.model_validate(team).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return team


class PlayerService:
    def __init__(
        self,
        repository: PlayerRepository,
        team_repository: TeamRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService | None = None,
    ) -> None:
        self._repository = repository
        self._team_repository = team_repository
        self._unit_of_work = unit_of_work
        self._audit_service = audit_service

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
        team: str | None = None,
        category: TeamCategory | None = None,
        active: bool | None = True,
        public_only: bool = True,
    ) -> tuple[list[Player], int]:
        return await self._repository.list(
            page=page,
            page_size=page_size,
            q=q,
            team=team,
            category=category,
            active=active,
            public_only=public_only,
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
        async with self._unit_of_work:
            previous_id = await self._repository.creation_ledger.reserve("players", payload)
            if previous_id is not None:
                previous = await self.get(previous_id)
                await self._unit_of_work.commit()
                return previous
            team = await self._require_team(payload.team_id)
            if payload.active and not team.active:
                raise PublicationDependencyError(
                    "Сначала опубликуйте связанную команду; затем публикуйте игрока."
                )
            await self._repository.require_media(payload.photo_asset_id)
            player = Player(**payload.model_dump())
            created = await self._repository.add(player)
            await self._record(
                action=AuditAction.CREATE,
                player=created,
                after=PlayerRead.model_validate(created).model_dump(mode="json"),
            )
            await self._repository.creation_ledger.finish("players", payload, created.id)
            await self._unit_of_work.commit()
            return created

    async def update(self, player_id: UUID, payload: PlayerUpdate) -> Player:
        async with self._unit_of_work:
            player = await self.get(player_id)
            require_version(player.updated_at, payload.expected_updated_at)
            before = PlayerRead.model_validate(player).model_dump(mode="json")
            changes = payload.model_dump(exclude_unset=True)
            if changes.get("active", player.active):
                team = await self._require_team(changes.get("team_id", player.team_id))
                if not team.active:
                    raise PublicationDependencyError(
                        "Сначала опубликуйте связанную команду; затем публикуйте игрока."
                    )
            if "photo_asset_id" in payload.model_fields_set:
                await self._repository.require_media(payload.photo_asset_id)
            team_id = changes.get("team_id")
            if team_id is not None and team_id != player.team_id:
                await self._require_team(team_id)

            for field, value in changes.items():
                setattr(player, field, value)
            updated = await self._repository.save(player)
            await self._record(
                action=AuditAction.UPDATE,
                player=updated,
                before=before,
                after=PlayerRead.model_validate(updated).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return updated

    async def delete(self, player_id: UUID, expected_updated_at: datetime | None = None) -> None:
        async with self._unit_of_work:
            player = await self.get(player_id)
            require_version(player.updated_at, expected_updated_at)
            before = PlayerRead.model_validate(player).model_dump(mode="json")
            await self._repository.soft_delete(player)
            await self._record(
                action=AuditAction.DELETE,
                player=player,
                before=before,
            )
            await self._unit_of_work.commit()

    async def _require_team(self, team_id: UUID) -> Team:
        team = await self._team_repository.get(team_id)
        if team is None:
            raise NotFoundError("Team not found")
        return team

    async def _record(
        self,
        *,
        action: AuditAction,
        player: Player,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        if self._audit_service is not None:
            await self._audit_service.record(
                action=action,
                entity_type="player",
                entity_id=player.id,
                before=before,
                after=after,
            )
