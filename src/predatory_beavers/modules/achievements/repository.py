from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.modules.achievements.models import Achievement
from predatory_beavers.modules.club.models import Team, TeamCategory


class AchievementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _with_details(statement: Select[tuple[Achievement]]) -> Select[tuple[Achievement]]:
        return statement.options(
            selectinload(Achievement.team),
            selectinload(Achievement.media),
        )

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        team: str | None = None,
        category: TeamCategory | None = None,
        active: bool | None = True,
        public_only: bool = True,
    ) -> tuple[list[Achievement], int]:
        filters: list[ColumnElement[bool]] = [Achievement.is_deleted.is_(False)]
        if public_only:
            filters.extend([Team.active.is_(True), Team.is_deleted.is_(False)])
        if team is not None:
            filters.append(Team.slug == team)
        if category is not None:
            filters.append(Team.category == category)
        if active is not None:
            filters.append(Achievement.active.is_(active))

        base = select(Achievement).join(Achievement.team).where(*filters)
        total = await self._session.scalar(select(func.count()).select_from(base.subquery()))
        statement = self._with_details(
            base.order_by(Achievement.sort_order, Achievement.title, Achievement.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(await self._session.scalars(statement)), int(total or 0)

    async def get(self, achievement_id: UUID, *, public_only: bool = False) -> Achievement | None:
        filters: list[ColumnElement[bool]] = [
            Achievement.id == achievement_id,
            Achievement.is_deleted.is_(False),
        ]
        statement = select(Achievement)
        if public_only:
            filters.extend(
                [
                    Achievement.active.is_(True),
                    Team.active.is_(True),
                    Team.is_deleted.is_(False),
                ]
            )
            statement = statement.join(Achievement.team)
        statement = self._with_details(statement.where(*filters))
        return cast(Achievement | None, await self._session.scalar(statement))

    async def add(self, achievement: Achievement) -> Achievement:
        self._session.add(achievement)
        await self._session.flush()
        return await self._load_after_write(achievement.id)

    async def save(self, achievement: Achievement) -> Achievement:
        await self._session.flush()
        return await self._load_after_write(achievement.id)

    async def soft_delete(self, achievement: Achievement) -> None:
        achievement.is_deleted = True
        achievement.deleted_at = datetime.now(UTC)
        achievement.active = False
        await self._session.flush()

    async def _load_after_write(self, achievement_id: UUID) -> Achievement:
        statement = self._with_details(
            select(Achievement).where(Achievement.id == achievement_id)
        ).execution_options(populate_existing=True)
        achievement = await self._session.scalar(statement)
        if achievement is None:
            raise RuntimeError("Persisted achievement could not be reloaded")
        return achievement
