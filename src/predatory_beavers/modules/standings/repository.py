from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.matches.models import Competition
from predatory_beavers.modules.standings.models import StandingsSnapshot


class StandingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _with_details(
        statement: Select[tuple[StandingsSnapshot]],
    ) -> Select[tuple[StandingsSnapshot]]:
        return statement.options(
            selectinload(StandingsSnapshot.team),
            selectinload(StandingsSnapshot.competition),
        )

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        team: str | None = None,
        season: str | None = None,
        is_current: bool | None = None,
    ) -> tuple[list[StandingsSnapshot], int]:
        filters: list[ColumnElement[bool]] = [StandingsSnapshot.is_deleted.is_(False)]
        if team is not None:
            filters.append(Team.slug == team)
        if season is not None:
            filters.append(Competition.season == season)
        if is_current is not None:
            filters.append(StandingsSnapshot.is_current.is_(is_current))

        base = (
            select(StandingsSnapshot)
            .join(StandingsSnapshot.team)
            .join(StandingsSnapshot.competition)
            .where(*filters)
        )
        total = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        statement = self._with_details(
            base.order_by(
                StandingsSnapshot.fetched_at.desc(),
                StandingsSnapshot.created_at.desc(),
                StandingsSnapshot.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(await self._session.scalars(statement)), int(total or 0)

    async def get(self, snapshot_id: UUID) -> StandingsSnapshot | None:
        statement = self._with_details(
            select(StandingsSnapshot).where(
                StandingsSnapshot.id == snapshot_id,
                StandingsSnapshot.is_deleted.is_(False),
            )
        )
        return cast(StandingsSnapshot | None, await self._session.scalar(statement))

    async def get_current(
        self,
        *,
        team: str,
        season: str | None = None,
        competition_id: UUID | None = None,
    ) -> StandingsSnapshot | None:
        filters: list[ColumnElement[bool]] = [
            StandingsSnapshot.is_current.is_(True),
            StandingsSnapshot.is_deleted.is_(False),
            Team.slug == team,
            Team.active.is_(True),
            Team.is_deleted.is_(False),
            Competition.active.is_(True),
            Competition.is_deleted.is_(False),
        ]
        if season is not None:
            filters.append(Competition.season == season)
        if competition_id is not None:
            filters.append(StandingsSnapshot.competition_id == competition_id)

        statement = self._with_details(
            select(StandingsSnapshot)
            .join(StandingsSnapshot.team)
            .join(StandingsSnapshot.competition)
            .where(*filters)
            .order_by(
                Competition.season.desc(),
                StandingsSnapshot.fetched_at.desc(),
                StandingsSnapshot.created_at.desc(),
            )
            .limit(1)
        )
        return cast(StandingsSnapshot | None, await self._session.scalar(statement))

    async def get_current_for_pair(
        self,
        *,
        team_id: UUID,
        competition_id: UUID,
    ) -> StandingsSnapshot | None:
        statement = self._with_details(
            select(StandingsSnapshot).where(
                StandingsSnapshot.team_id == team_id,
                StandingsSnapshot.competition_id == competition_id,
                StandingsSnapshot.is_current.is_(True),
                StandingsSnapshot.is_deleted.is_(False),
            )
        )
        return cast(StandingsSnapshot | None, await self._session.scalar(statement))

    async def archive_current(self, *, team_id: UUID, competition_id: UUID) -> None:
        await self._session.execute(
            update(StandingsSnapshot)
            .where(
                StandingsSnapshot.team_id == team_id,
                StandingsSnapshot.competition_id == competition_id,
                StandingsSnapshot.is_current.is_(True),
                StandingsSnapshot.is_deleted.is_(False),
            )
            .values(is_current=False, updated_at=datetime.now(UTC))
        )

    async def add(self, snapshot: StandingsSnapshot) -> StandingsSnapshot:
        self._session.add(snapshot)
        await self._session.flush()
        return await self._load_after_write(snapshot.id)

    async def soft_delete(self, snapshot: StandingsSnapshot) -> None:
        snapshot.is_deleted = True
        snapshot.deleted_at = datetime.now(UTC)
        snapshot.is_current = False
        await self._session.flush()

    async def _load_after_write(self, snapshot_id: UUID) -> StandingsSnapshot:
        statement = self._with_details(
            select(StandingsSnapshot).where(StandingsSnapshot.id == snapshot_id)
        ).execution_options(populate_existing=True)
        snapshot = await self._session.scalar(statement)
        if snapshot is None:
            raise RuntimeError("Persisted standings snapshot could not be reloaded")
        return snapshot
