from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.api.responses import ApiResponse
from predatory_beavers.modules.roster.models import AsbRosterMember, AsbRosterSnapshot
from predatory_beavers.modules.roster.photos import cached_photo
from predatory_beavers.modules.roster.schemas import (
    RosterPlayerDetail,
    RosterPlayerRead,
    RosterRead,
    RosterSelection,
)

router = APIRouter(prefix="/asb-roster", route_class=DishkaRoute, tags=["asb-roster"])


def current_season(today: date | None = None) -> str:
    today = today or datetime.now(UTC).date()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{start + 1}"


def player_read(member: AsbRosterMember, team: str | None = None) -> RosterPlayerRead:
    def average(total: int | None, divisor: int = 1) -> float | None:
        if total is None or not member.games_played:
            return None
        return round(total / member.games_played / divisor, 2)

    return RosterPlayerRead(
        id=member.id,
        external_player_id=member.external_player_id,
        full_name=member.full_name,
        photo_url=(
            f"/api/v1/asb-roster/{team}/players/{member.id}/photo"
            if team and cached_photo(member.photo_url)
            else None
        ),
        games_played=member.games_played,
        points_per_game=average(member.points_total),
        assists_per_game=average(member.assists_total),
        rebounds_per_game=average(member.rebounds_total),
        minutes_per_game=average(member.seconds_total, 60),
        statistics_season=member.statistics_season,
        statistics_source_url=member.statistics_source_url,
    )


async def read_roster(session: AsyncSession, team: str, selection: RosterSelection) -> RosterRead:
    season_now = current_season()
    latest = await session.scalar(
        select(AsbRosterSnapshot)
        .where(
            AsbRosterSnapshot.team == team,
            AsbRosterSnapshot.is_published.is_(True),
            AsbRosterSnapshot.season <= season_now,
        )
        .order_by(AsbRosterSnapshot.season.desc())
        .limit(1)
    )
    snapshot = (
        latest
        if selection == "latest_published"
        else await session.scalar(
            select(AsbRosterSnapshot).where(
                AsbRosterSnapshot.team == team, AsbRosterSnapshot.season == season_now
            )
        )
    )
    published = bool(snapshot and snapshot.is_published)
    selected_season = snapshot.season if snapshot else season_now
    members = (
        (
            await session.scalars(
                select(AsbRosterMember)
                .where(
                    AsbRosterMember.snapshot_id == snapshot.id,
                    AsbRosterMember.is_listed.is_(True),
                )
                .order_by(AsbRosterMember.full_name, AsbRosterMember.id)
            )
        ).all()
        if snapshot and published
        else []
    )
    notice = None
    if not published:
        notice = f"Состав ВГУ на сезон {season_now.replace('-', '/')} ещё не опубликован АСБ."
    elif selected_season != season_now:
        notice = (
            f"Последний опубликованный состав АСБ — сезон {selected_season.replace('-', '/')}. "
            f"Состав на {season_now.replace('-', '/')} ещё не подтверждён."
        )
    elif members and any(
        member.statistics_season and member.statistics_season != season_now for member in members
    ):
        notice = (
            "Состав подтверждён на текущий сезон. Статистика каждого игрока показана за его "
            "последний опубликованный сезон в ВГУ; сезон указан отдельно. "
            "Прочерк означает, что АСБ не опубликовала показатель."
        )
    elif members and not any(member.games_played is not None for member in members):
        notice = "Заявка сезона опубликована. Индивидуальная статистика АСБ пока отсутствует."
    return RosterRead(
        team=team,
        current_season=season_now,
        season=selected_season,
        latest_published_season=latest.season if latest else None,
        is_current_season=bool(published and selected_season == season_now),
        status="published" if published else "not_published",
        selection=selection,
        notice=notice,
        source_url=(
            f"https://asbasket.ru/team/{snapshot.external_team_id}"
            f"?season={selected_season.split('-')[1]}"
            f"&league={'asb-cfo-36-m' if team == 'men' else 'asb-cfo-36-w'}"
            if snapshot
            else None
        ),
        stats_source_url=snapshot.stats_source_url if snapshot else None,
        fetched_at=snapshot.checked_at if snapshot else None,
        players=[
            player_read(member, team if selected_season == season_now else None)
            for member in members
        ],
    )


@router.get("/{team}", response_model=ApiResponse[RosterRead])
async def get_roster(
    team: Literal["men", "women"],
    session: FromDishka[AsyncSession],
    selection: RosterSelection = "current",
) -> ApiResponse[RosterRead]:
    return ApiResponse(
        message="ASB roster retrieved", data=await read_roster(session, team, selection)
    )


@router.get("/{team}/players/{player_id}", response_model=ApiResponse[RosterPlayerDetail])
async def get_roster_player(
    team: Literal["men", "women"],
    player_id: UUID,
    session: FromDishka[AsyncSession],
    selection: RosterSelection = "current",
) -> ApiResponse[RosterPlayerDetail]:
    roster = await read_roster(session, team, selection)
    player = next((item for item in roster.players if item.id == player_id), None)
    if player is None:
        raise NotFoundError("Player is not in the selected published roster")
    return ApiResponse(
        message="ASB roster player retrieved",
        data=RosterPlayerDetail(**roster.model_dump(exclude={"players"}), player=player),
    )


@router.get("/{team}/players/{player_id}/photo", response_class=FileResponse)
async def get_roster_player_photo(
    team: Literal["men", "women"],
    player_id: UUID,
    session: FromDishka[AsyncSession],
) -> FileResponse:
    member = await session.scalar(
        select(AsbRosterMember)
        .join(AsbRosterSnapshot, AsbRosterMember.snapshot_id == AsbRosterSnapshot.id)
        .where(
            AsbRosterMember.id == player_id,
            AsbRosterMember.is_listed.is_(True),
            AsbRosterSnapshot.team == team,
            AsbRosterSnapshot.season == current_season(),
            AsbRosterSnapshot.is_published.is_(True),
        )
    )
    path = cached_photo(member.photo_url) if member else None
    if path is None:
        raise NotFoundError("Photo is not available for a current roster member")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )
