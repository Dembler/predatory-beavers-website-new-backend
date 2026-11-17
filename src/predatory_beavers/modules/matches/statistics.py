"""Shared visibility and freshness boundary for API and mascot knowledge."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.matches.models import (
    Competition,
    Match,
    MatchStatistics,
    MatchStatus,
)
from predatory_beavers.modules.matches.statistics_schemas import GameStatisticsRead, ScoreByPeriod
from predatory_beavers.modules.results.models import ArchiveResult


async def _parents(
    session: AsyncSession,
    external_id: str | None = None,
    external_ids: set[str] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    parents: dict[tuple[str, str], dict[str, Any]] = {}
    archive_query = select(
        ArchiveResult.source,
        ArchiveResult.external_id,
        ArchiveResult.discipline,
        ArchiveResult.status,
        ArchiveResult.team,
        ArchiveResult.season,
        ArchiveResult.club_score,
        ArchiveResult.opponent_score,
    ).where(ArchiveResult.source == "asb")
    if external_id is not None:
        archive_query = archive_query.where(ArchiveResult.external_id == external_id)
    if external_ids is not None:
        archive_query = archive_query.where(ArchiveResult.external_id.in_(external_ids))
    archives = await session.execute(archive_query)
    for row in archives:
        parents[(row.source, row.external_id)] = {
            "visible": row.discipline == "5x5",
            "finished": row.status == "finished",
            "team": row.team,
            "season": row.season,
            "club_score": row.club_score,
            "opponent_score": row.opponent_score,
        }
    # A published source archive must not bypass an editor's withdrawn Match.
    match_query = (
        select(
            Match.source,
            Match.external_id,
            Match.status,
            Match.home_score,
            Match.away_score,
            Match.club_side,
            Match.active.label("match_active"),
            Match.is_deleted.label("match_deleted"),
            Team.active.label("team_active"),
            Team.is_deleted.label("team_deleted"),
            Team.category,
            Competition.active.label("competition_active"),
            Competition.is_deleted.label("competition_deleted"),
            Competition.season,
        )
        .join(Team, Match.team_id == Team.id)
        .join(Competition, Match.competition_id == Competition.id)
        .where(Match.source == "asb")
    )
    if external_id is not None:
        match_query = match_query.where(Match.external_id == external_id)
    if external_ids is not None:
        match_query = match_query.where(Match.external_id.in_(external_ids))
    matches = await session.execute(match_query)
    for row in matches:
        parents[(row.source, row.external_id)] = {
            "visible": row.match_active
            and not row.match_deleted
            and row.team_active
            and not row.team_deleted
            and row.competition_active
            and not row.competition_deleted,
            "finished": row.status is MatchStatus.FINISHED,
            "team": row.category.value,
            "season": row.season,
            "home_score": row.home_score,
            "away_score": row.away_score,
            "club_side": row.club_side.value,
        }
    return parents


def matches_current_parent(data: GameStatisticsRead, parent: dict[str, Any]) -> bool:
    if not parent["visible"] or not parent["finished"]:
        return False
    if parent["team"] != data.team or parent["season"] != data.season:
        return False
    if "home_score" in parent:
        return bool(
            parent["home_score"] == data.home_score
            and parent["away_score"] == data.away_score
            and parent["club_side"] == data.club_side
        )
    club, opponent = (
        (data.home_score, data.away_score)
        if data.club_side == "home"
        else (data.away_score, data.home_score)
    )
    return bool(parent["club_score"] == club and parent["opponent_score"] == opponent)


async def public_statistics(
    session: AsyncSession,
    *,
    external_id: str | None = None,
    external_ids: set[str] | None = None,
) -> list[GameStatisticsRead]:
    """Validated current public protocols, reused by retrieval; no network access."""
    if external_ids == set():
        return []
    parents = await _parents(session, external_id, external_ids)
    result = []
    query = select(MatchStatistics).where(MatchStatistics.source == "asb")
    if external_id is not None:
        query = query.where(MatchStatistics.external_id == external_id)
    if external_ids is not None:
        query = query.where(MatchStatistics.external_id.in_(external_ids))
    for row in await session.scalars(query):
        try:
            data = GameStatisticsRead.model_validate(row.payload)
        except ValidationError:
            continue
        if (data.source, data.external_id, data.team, data.season, data.discipline) != (
            row.source,
            row.external_id,
            row.team,
            row.season,
            row.discipline,
        ):
            continue
        parent = parents.get((row.source, row.external_id))
        if parent and matches_current_parent(data, parent):
            result.append(data)
    return result


async def public_period_scores(
    session: AsyncSession,
    records: Sequence[Match | ArchiveResult],
) -> dict[UUID, list[ScoreByPeriod]]:
    """Fetch one page's valid protocols in a fixed batch, never per card.

    Archive cards may display older independent evidence even when a calendar
    copy was corrected. Check that displayed record too before attaching scores.
    """
    ids = {row.external_id for row in records if row.source == "asb" and row.external_id}
    protocols = {row.external_id: row for row in await public_statistics(session, external_ids=ids)}
    result: dict[UUID, list[ScoreByPeriod]] = {}
    for row in records:
        data = protocols.get(row.external_id or "") if row.source == "asb" else None
        if data is None:
            continue
        if isinstance(row, ArchiveResult):
            parent = {
                "visible": row.discipline == "5x5",
                "finished": row.status == "finished",
                "team": row.team,
                "season": row.season,
                "club_score": row.club_score,
                "opponent_score": row.opponent_score,
            }
            if not matches_current_parent(data, parent):
                continue
        elif (
            row.status is not MatchStatus.FINISHED
            or row.home_score != data.home_score
            or row.away_score != data.away_score
            or row.club_side.value != data.club_side
        ):
            continue
        result[row.id] = [
            ScoreByPeriod(
                number=period.number,
                label=period.label,
                club_score=period.home_score if data.club_side == "home" else period.away_score,
                opponent_score=period.away_score if data.club_side == "home" else period.home_score,
            )
            for period in data.period_scores
        ]
    return result


async def get_public_statistics(
    session: AsyncSession,
    source: str,
    external_id: str,
) -> GameStatisticsRead | None:
    parents = await _parents(session, external_id)
    parent = parents.get((source, external_id))
    if source != "asb" or not parent or not parent["visible"]:
        raise NotFoundError("Public game not found")
    # A score correction immediately fences the old protocol until recollection.
    return next(
        (
            row
            for row in await public_statistics(session, external_id=external_id)
            if row.source == source and row.external_id == external_id
        ),
        None,
    )
