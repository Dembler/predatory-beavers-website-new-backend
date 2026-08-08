import builtins
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.matches.models import Competition, Match, MatchStatus, Venue


class CompetitionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        season: str | None = None,
        active: bool | None = True,
    ) -> tuple[list[Competition], int]:
        filters: list[ColumnElement[bool]] = [Competition.is_deleted.is_(False)]
        if season is not None:
            filters.append(Competition.season == season)
        if active is not None:
            filters.append(Competition.active.is_(active))
        total = await self._session.scalar(select(func.count(Competition.id)).where(*filters))
        statement = (
            select(Competition)
            .where(*filters)
            .order_by(Competition.season.desc(), Competition.name, Competition.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(await self._session.scalars(statement)), int(total or 0)

    async def get(self, competition_id: UUID) -> Competition | None:
        statement = select(Competition).where(
            Competition.id == competition_id,
            Competition.is_deleted.is_(False),
        )
        return cast(Competition | None, await self._session.scalar(statement))

    async def get_by_external_identity(
        self,
        source: str,
        external_id: str,
    ) -> Competition | None:
        statement = select(Competition).where(
            Competition.source == source,
            Competition.external_id == external_id,
        )
        return cast(Competition | None, await self._session.scalar(statement))

    async def add(self, competition: Competition) -> Competition:
        self._session.add(competition)
        await self._session.flush()
        return competition

    async def save(self, competition: Competition) -> Competition:
        await self._session.flush()
        return competition

    async def soft_delete(self, competition: Competition) -> None:
        competition.is_deleted = True
        competition.deleted_at = datetime.now(UTC)
        competition.active = False
        await self._session.flush()


class VenueRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        active: bool | None = True,
    ) -> tuple[list[Venue], int]:
        filters: list[ColumnElement[bool]] = [Venue.is_deleted.is_(False)]
        if active is not None:
            filters.append(Venue.active.is_(active))
        total = await self._session.scalar(select(func.count(Venue.id)).where(*filters))
        statement = (
            select(Venue)
            .where(*filters)
            .order_by(Venue.name, Venue.address, Venue.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(await self._session.scalars(statement)), int(total or 0)

    async def get(self, venue_id: UUID) -> Venue | None:
        statement = select(Venue).where(Venue.id == venue_id, Venue.is_deleted.is_(False))
        return cast(Venue | None, await self._session.scalar(statement))

    async def get_by_external_identity(self, source: str, external_id: str) -> Venue | None:
        statement = select(Venue).where(
            Venue.source == source,
            Venue.external_id == external_id,
            Venue.is_deleted.is_(False),
        )
        return cast(Venue | None, await self._session.scalar(statement))

    async def add(self, venue: Venue) -> Venue:
        self._session.add(venue)
        await self._session.flush()
        return venue

    async def save(self, venue: Venue) -> Venue:
        await self._session.flush()
        return venue

    async def soft_delete(self, venue: Venue) -> None:
        venue.is_deleted = True
        venue.deleted_at = datetime.now(UTC)
        venue.active = False
        await self._session.flush()


class MatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _with_details(statement: Select[tuple[Match]]) -> Select[tuple[Match]]:
        return statement.options(
            selectinload(Match.team),
            selectinload(Match.competition),
            selectinload(Match.venue),
            selectinload(Match.home_logo),
            selectinload(Match.away_logo),
        )

    @staticmethod
    def _public_filters() -> list[ColumnElement[bool]]:
        return [
            Match.is_deleted.is_(False),
            Team.active.is_(True),
            Team.is_deleted.is_(False),
            Competition.active.is_(True),
            Competition.is_deleted.is_(False),
        ]

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        team: str | None = None,
        status: MatchStatus | None = None,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        featured: bool | None = None,
        public_only: bool = True,
    ) -> tuple[list[Match], int]:
        filters: list[ColumnElement[bool]] = [Match.is_deleted.is_(False)]
        if public_only:
            filters = self._public_filters()
        if team is not None:
            filters.append(Team.slug == team)
        if status is not None:
            filters.append(Match.status == status)
        if starts_from is not None:
            filters.append(Match.starts_at >= starts_from)
        if starts_to is not None:
            filters.append(Match.starts_at <= starts_to)
        if featured is not None:
            filters.append(Match.featured.is_(featured))

        base = select(Match).join(Match.team).join(Match.competition).where(*filters)
        count_statement = select(func.count()).select_from(base.subquery())
        total = await self._session.scalar(count_statement)
        statement = self._with_details(
            base.order_by(Match.starts_at, Match.id).offset((page - 1) * page_size).limit(page_size)
        )
        return list(await self._session.scalars(statement)), int(total or 0)

    async def next_public_match(self, now: datetime) -> Match | None:
        filters = self._public_filters()
        filters.append(
            or_(
                Match.status == MatchStatus.LIVE,
                and_(Match.status == MatchStatus.SCHEDULED, Match.starts_at >= now),
            )
        )
        statement = self._with_details(
            select(Match)
            .join(Match.team)
            .join(Match.competition)
            .where(*filters)
            .order_by(
                case((Match.status == MatchStatus.LIVE, 0), else_=1),
                Match.starts_at,
                Match.id,
            )
            .limit(1)
        )
        return cast(Match | None, await self._session.scalar(statement))

    async def recent_public_results(self, now: datetime, *, limit: int = 3) -> builtins.list[Match]:
        filters = self._public_filters()
        filters.extend([Match.status == MatchStatus.FINISHED, Match.starts_at <= now])
        statement = self._with_details(
            select(Match)
            .join(Match.team)
            .join(Match.competition)
            .where(*filters)
            .order_by(Match.starts_at.desc(), Match.id.desc())
            .limit(limit)
        )
        return list(await self._session.scalars(statement))

    async def get(self, match_id: UUID, *, public_only: bool = False) -> Match | None:
        filters: list[ColumnElement[bool]] = [
            Match.id == match_id,
            Match.is_deleted.is_(False),
        ]
        statement = select(Match)
        if public_only:
            filters = [Match.id == match_id, *self._public_filters()]
            statement = statement.join(Match.team).join(Match.competition)
        statement = self._with_details(statement.where(*filters))
        return cast(Match | None, await self._session.scalar(statement))

    async def get_by_external_identity(self, source: str, external_id: str) -> Match | None:
        statement = self._with_details(
            select(Match).where(
                Match.source == source,
                Match.external_id == external_id,
            )
        )
        return cast(Match | None, await self._session.scalar(statement))

    async def add(self, match: Match) -> Match:
        self._session.add(match)
        await self._session.flush()
        return await self._load_after_write(match.id)

    async def save(self, match: Match) -> Match:
        await self._session.flush()
        return await self._load_after_write(match.id)

    async def soft_delete(self, match: Match) -> None:
        match.is_deleted = True
        match.deleted_at = datetime.now(UTC)
        match.featured = False
        await self._session.flush()

    async def _load_after_write(self, match_id: UUID) -> Match:
        statement = self._with_details(select(Match).where(Match.id == match_id)).execution_options(
            populate_existing=True
        )
        match = await self._session.scalar(statement)
        if match is None:
            raise RuntimeError("Persisted match could not be reloaded")
        return match
