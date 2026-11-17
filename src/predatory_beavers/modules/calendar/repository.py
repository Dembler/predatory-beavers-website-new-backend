from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    cast,
    exists,
    extract,
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
from predatory_beavers.modules.calendar.sql import MoscowTimestamp, TimestampOrder, YearStartOrder
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.matches.models import Competition, Match, MatchStatus
from predatory_beavers.modules.matches.schemas import MatchRead
from predatory_beavers.modules.matches.statistics import public_period_scores
from predatory_beavers.modules.matches.visibility import public_match_conditions
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.results.router import ArchiveResultRead


def _season(start: SQLColumnExpression[Any]) -> ColumnElement[str]:
    return cast(start, String) + "-" + cast(start + 1, String)


def _declared_season(value: SQLColumnExpression[Any]) -> ColumnElement[Any]:
    # The source may use either separator and spaces around it. A year alone
    # deliberately remains ambiguous and must never become a fabricated date.
    normalized = func.replace(func.replace(value, " ", ""), "/", "-")
    nondigits = func.replace(normalized, "-", "")
    for digit in "0123456789":
        nondigits = func.replace(nondigits, digit, "")
    shape = and_(
        func.length(normalized) == 9,
        func.substr(normalized, 5, 1) == "-",
        func.length(func.replace(normalized, "-", "")) == 8,
        nondigits == "",
    )
    # PostgreSQL may reorder AND operands. Guard each integer cast with CASE so
    # an ambiguous source such as "202x-2027" never reaches a numeric cast.
    first = cast(case((shape, func.substr(normalized, 1, 4)), else_=None), Integer)
    last = cast(case((shape, func.substr(normalized, 6, 4)), else_=None), Integer)
    return case(
        (
            and_(shape, last == first + 1),
            normalized,
        ),
        else_=None,
    )


def _date_season(value: SQLColumnExpression[Any], *, moscow: bool = False) -> ColumnElement[str]:
    local = MoscowTimestamp(value) if moscow else value
    year = cast(extract("year", local), Integer)
    month = cast(extract("month", local), Integer)
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
                TimestampOrder(Match.starts_at).label("date_order"),
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
        # Tournament placements are calendar evidence too. They have no single
        # opponent and must retain their place instead of becoming fake games.
        archive_conditions = [~public_copy]
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
                (ArchiveResult.status.in_(["cancelled", "postponed"]), "changed"),
                (
                    and_(
                        ArchiveResult.status == "scheduled",
                        ArchiveResult.event_date
                        >= at.astimezone(timezone(timedelta(hours=3))).date(),
                    ),
                    "upcoming",
                ),
                else_="awaiting",
            ).label("section"),
            func.coalesce(
                TimestampOrder(ArchiveResult.event_date), YearStartOrder(ArchiveResult.year)
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
        # Keep the real current season visible even before a provider publishes
        # its fixtures. An older populated season must not masquerade as current.
        today = at.astimezone(timezone(timedelta(hours=3))).date()
        start = today.year if today.month >= 7 else today.year - 1
        current_season = f"{start}-{start + 1}"
        seasons = sorted(set(seasons) | {current_season}, reverse=True)
        default = current_season
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
        scores = await public_period_scores(self.session, [*matches.values(), *results.values()])
        public_matches = {
            key: MatchRead.from_public(value, score_by_periods=scores.get(key))
            for key, value in matches.items()
        }
        public_results = {
            key: ArchiveResultRead.model_validate(value) for key, value in results.items()
        }
        for key, value in public_results.items():
            value.score_by_periods = scores.get(key, [])
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
                match=public_matches[row["id"]] if row["kind"] == "calendar" else None,
                result=public_results[row["id"]] if row["kind"] == "archive" else None,
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
            current_season=current_season,
            default_season=default,
            selected_season=selected,
            at=at,
        )
