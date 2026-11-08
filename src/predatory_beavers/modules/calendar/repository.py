from datetime import datetime
from typing import Any

from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression
from sqlalchemy.sql.selectable import CTE

from predatory_beavers.modules.calendar.schemas import CalendarCounts, CalendarEntry, CalendarPage
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.matches.models import Competition, Match, MatchStatus
from predatory_beavers.modules.matches.schemas import MatchRead
from predatory_beavers.modules.matches.visibility import public_match_conditions
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.results.router import ArchiveResultRead


def _season(start: SQLColumnExpression[Any]) -> ColumnElement[str]:
    return cast(start, String) + "-" + cast(start + 1, String)


def _declared_season(value: SQLColumnExpression[Any]) -> ColumnElement[Any]:
    # The source may use either separator and spaces around it. A year alone
    # deliberately remains ambiguous and must never become a fabricated date.
    normalized = func.replace(func.replace(value, " ", ""), "/", "-")
    return case(
        (
            and_(
                normalized.op("GLOB")("[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]"),
                cast(func.substr(normalized, 6, 4), Integer)
                == cast(func.substr(normalized, 1, 4), Integer) + 1,
            ),
            normalized,
        ),
        else_=None,
    )


def _date_season(value: SQLColumnExpression[Any], *, moscow: bool = False) -> ColumnElement[str]:
    modifiers = ("+3 hours",) if moscow else ()
    year = cast(func.strftime("%Y", value, *modifiers), Integer)
    month = cast(func.strftime("%m", value, *modifiers), Integer)
    return _season(case((month >= 7, year), else_=year - 1))


class CalendarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _public_conditions() -> list[ColumnElement[bool]]:
        return public_match_conditions()

    def _feed(self, *, at: datetime, team: str | None, discipline: str | None) -> CTE:
        public = self._public_conditions()
        match_season = func.coalesce(
            _declared_season(Competition.season), _date_season(Match.starts_at, moscow=True)
        )
        match_section = case(
            (Match.status == MatchStatus.LIVE, "upcoming"),
            (
                and_(Match.status == MatchStatus.SCHEDULED, Match.starts_at >= at),
                "upcoming",
            ),
            (Match.status == MatchStatus.SCHEDULED, "awaiting"),
            (Match.status == MatchStatus.FINISHED, "results"),
            else_="changed",
        )
        conditions = [*public]
        if team:
            conditions.append(Team.category == TeamCategory(team))
        if discipline and discipline != "5x5":
            conditions.append(literal(False))
        calendar = (
            select(
                Match.id.label("id"),
                literal("calendar").label("kind"),
                literal("5x5").label("discipline"),
                func.lower(cast(Match.status, String)).label("status"),
                match_section.label("section"),
                func.julianday(Match.starts_at).label("date_order"),
                match_season.label("season_low"),
                match_season.label("season_high"),
            )
            .join(Match.team)
            .join(Match.competition)
            .where(*conditions)
        )
        explicit = _declared_season(ArchiveResult.season)
        dated = case(
            (ArchiveResult.date_precision != "year", _date_season(ArchiveResult.event_date)),
            else_=None,
        )
        low = func.coalesce(explicit, dated, _season(ArchiveResult.year - 1))
        high = func.coalesce(explicit, dated, _season(ArchiveResult.year))
        # Only a publicly visible calendar copy replaces archive evidence. A
        # hidden/deleted copy must not accidentally erase the independent source.
        public_copy = exists(
            select(Match.id)
            .join(Match.team)
            .join(Match.competition)
            .where(
                *public,
                Match.source == ArchiveResult.source,
                Match.external_id == ArchiveResult.external_id,
                Match.external_id.is_not(None),
                Match.external_id != "",
            )
        ).correlate(ArchiveResult)
        archive_conditions = [
            ArchiveResult.opponent_name.is_not(None),
            ArchiveResult.opponent_name != "",
            ~public_copy,
        ]
        if team:
            archive_conditions.append(ArchiveResult.team == team)
        if discipline:
            archive_conditions.append(ArchiveResult.discipline == discipline)
        archive = select(
            ArchiveResult.id.label("id"),
            literal("archive").label("kind"),
            ArchiveResult.discipline.label("discipline"),
            ArchiveResult.status.label("status"),
            case(
                (ArchiveResult.status.in_(["finished", "participated"]), "results"),
                else_="awaiting",
            ).label("section"),
            func.julianday(
                func.coalesce(ArchiveResult.event_date, cast(ArchiveResult.year, String) + "-01-01")
            ).label("date_order"),
            low.label("season_low"),
            high.label("season_high"),
        ).where(*archive_conditions)
        return union_all(calendar, archive).cte("public_calendar")

    async def page(
        self,
        *,
        at: datetime,
        team: str | None,
        discipline: str | None,
        status: str | None,
        season: str | None,
        page: int,
        page_size: int,
    ) -> CalendarPage:
        feed = self._feed(at=at, team=team, discipline=discipline)
        # Metadata is aggregated in SQL; only the small distinct season list is
        # materialized. Complete historical records never enter application RAM.
        seasons_query = union_all(
            select(feed.c.season_low.label("season")),
            select(feed.c.season_high.label("season")),
        ).subquery()
        seasons = list(
            await self.session.scalars(
                select(seasons_query.c.season)
                .distinct()
                .where(seasons_query.c.season.is_not(None))
                .order_by(seasons_query.c.season.desc())
            )
        )
        default: str | None = None
        if discipline in (None, "5x5"):
            default = await self.session.scalar(
                select(feed.c.season_low)
                .where(feed.c.kind == "calendar", feed.c.section == "upcoming")
                .order_by(feed.c.date_order, feed.c.id)
                .limit(1)
            )
            if default is None:
                default = await self.session.scalar(
                    select(func.max(feed.c.season_low)).where(
                        feed.c.discipline == "5x5",
                        feed.c.status == "finished",
                        feed.c.season_low == feed.c.season_high,
                    )
                )
        default = default or (seasons[0] if seasons else "all")
        selected = season or default
        filters = []
        if status:
            filters.append(feed.c.status == status)
        if selected != "all":
            filters.append(or_(feed.c.season_low == selected, feed.c.season_high == selected))
        counts = CalendarCounts()
        for section, count in (
            await self.session.execute(
                select(feed.c.section, func.count()).where(*filters).group_by(feed.c.section)
            )
        ).all():
            setattr(counts, section, count)
        total = sum(counts.model_dump().values())
        total_pages = (total + page_size - 1) // page_size
        page = min(page, max(1, total_pages))
        section_order = case(
            (feed.c.section == "upcoming", 0),
            (feed.c.section == "changed", 1),
            (feed.c.section == "results", 2),
            else_=3,
        )
        upcoming_priority = case(
            (and_(feed.c.section == "upcoming", feed.c.status == "live"), 0), else_=1
        )
        date_order = case(
            (feed.c.section == "upcoming", feed.c.date_order), else_=-feed.c.date_order
        )
        rows = (
            (
                await self.session.execute(
                    select(feed)
                    .where(*filters)
                    .order_by(section_order, upcoming_priority, date_order, feed.c.kind, feed.c.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .mappings()
            .all()
        )
        calendar_ids = [row["id"] for row in rows if row["kind"] == "calendar"]
        archive_ids = [row["id"] for row in rows if row["kind"] == "archive"]
        matches = (
            {
                match.id: match
                for match in await self.session.scalars(
                    select(Match)
                    .where(Match.id.in_(calendar_ids))
                    .options(
                        selectinload(Match.team).selectinload(Team.logo),
                        selectinload(Match.competition),
                        selectinload(Match.venue),
                        selectinload(Match.home_logo),
                        selectinload(Match.away_logo),
                    )
                )
            }
            if calendar_ids
            else {}
        )
        results = (
            {
                result.id: result
                for result in await self.session.scalars(
                    select(ArchiveResult).where(ArchiveResult.id.in_(archive_ids))
                )
            }
            if archive_ids
            else {}
        )
        items = [
            CalendarEntry(
                kind=row["kind"],
                section=row["section"],
                season=(
                    row["season_low"]
                    if row["season_low"] == row["season_high"]
                    else selected
                    if selected != "all"
                    else "unknown"
                ),
                match=MatchRead.from_public(matches[row["id"]])
                if row["kind"] == "calendar"
                else None,
                result=ArchiveResultRead.model_validate(results[row["id"]])
                if row["kind"] == "archive"
                else None,
            )
            for row in rows
        ]
        return CalendarPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            counts=counts,
            seasons=seasons,
            default_season=default,
            selected_season=selected,
            at=at,
        )
