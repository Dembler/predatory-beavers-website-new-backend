from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.modules.club.models import Player, Team, TeamCategory


class TeamRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        category: TeamCategory | None = None,
        active: bool | None = True,
    ) -> tuple[list[Team], int]:
        filters: list[ColumnElement[bool]] = [Team.is_deleted.is_(False)]
        if category is not None:
            filters.append(Team.category == category)
        if active is not None:
            filters.append(Team.active.is_(active))

        total = await self._session.scalar(select(func.count(Team.id)).where(*filters))
        statement = (
            select(Team)
            .where(*filters)
            .options(selectinload(Team.logo))
            .order_by(Team.category, Team.name, Team.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self._session.scalars(statement)
        return list(result), int(total or 0)

    async def get(self, team_id: UUID) -> Team | None:
        statement = select(Team).where(Team.id == team_id, Team.is_deleted.is_(False))
        return cast(Team | None, await self._session.scalar(statement))

    async def get_by_slug(self, slug: str) -> Team | None:
        statement = select(Team).where(
            Team.slug == slug,
            Team.is_deleted.is_(False),
        )
        return cast(Team | None, await self._session.scalar(statement))


class PlayerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _with_details(statement: Select[tuple[Player]]) -> Select[tuple[Player]]:
        return statement.options(selectinload(Player.team), selectinload(Player.photo))

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        team: str | None = None,
        category: TeamCategory | None = None,
        active: bool | None = True,
        public_only: bool = True,
    ) -> tuple[list[Player], int]:
        filters: list[ColumnElement[bool]] = [
            Player.is_deleted.is_(False),
            Team.is_deleted.is_(False),
        ]
        if public_only:
            filters.append(Team.active.is_(True))
        if team is not None:
            filters.append(Team.slug == team)
        if category is not None:
            filters.append(Team.category == category)
        if active is not None:
            filters.append(Player.active.is_(active))

        count_statement = (
            select(func.count(Player.id)).select_from(Player).join(Player.team).where(*filters)
        )
        total = await self._session.scalar(count_statement)
        statement = (
            select(Player)
            .join(Player.team)
            .where(*filters)
            .options(selectinload(Player.team), selectinload(Player.photo))
            .order_by(Player.sort_order, Player.full_name, Player.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self._session.scalars(statement)
        return list(result), int(total or 0)

    async def get(self, player_id: UUID, *, public_only: bool = False) -> Player | None:
        filters: list[ColumnElement[bool]] = [
            Player.id == player_id,
            Player.is_deleted.is_(False),
        ]
        statement = select(Player)
        if public_only:
            filters.extend(
                [
                    Player.active.is_(True),
                    Team.active.is_(True),
                    Team.is_deleted.is_(False),
                ]
            )
            statement = statement.join(Player.team)
        statement = self._with_details(statement.where(*filters))
        return cast(Player | None, await self._session.scalar(statement))

    async def add(self, player: Player) -> Player:
        self._session.add(player)
        await self._session.flush()
        return await self._load_after_write(player.id)

    async def save(self, player: Player) -> Player:
        await self._session.flush()
        return await self._load_after_write(player.id)

    async def soft_delete(self, player: Player) -> None:
        player.is_deleted = True
        player.deleted_at = datetime.now(UTC)
        player.active = False
        await self._session.flush()

    async def _load_after_write(self, player_id: UUID) -> Player:
        # expire-on-commit may differ, so always reload a complete response graph.
        statement = self._with_details(
            select(Player).where(Player.id == player_id)
        ).execution_options(populate_existing=True)
        player = await self._session.scalar(statement)
        if player is None:  # defensive: the row was just persisted in this transaction
            raise RuntimeError("Persisted player could not be reloaded")
        return player
