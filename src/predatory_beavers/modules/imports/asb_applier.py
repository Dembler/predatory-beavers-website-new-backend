from datetime import UTC, datetime

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.integrations.asb.client import AsbImportBundle
from predatory_beavers.integrations.asb.schemas import AsbGame
from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.imports.asb_mapping import match_status, parse_asb_datetime
from predatory_beavers.modules.imports.schemas import AsbImportRequest
from predatory_beavers.modules.matches.models import (
    ClubSide,
    Competition,
    Match,
    MatchStatus,
    Venue,
)
from predatory_beavers.modules.matches.repository import (
    CompetitionRepository,
    MatchRepository,
    VenueRepository,
)
from predatory_beavers.modules.standings.models import StandingsSnapshot
from predatory_beavers.modules.standings.repository import StandingsRepository
from predatory_beavers.modules.standings.schemas import StandingRow, StandingsPublish


class AsbImportApplier:
    """Applies a validated ASB bundle to local repositories inside the caller's UoW."""

    def __init__(
        self,
        *,
        team_repository: TeamRepository,
        competition_repository: CompetitionRepository,
        venue_repository: VenueRepository,
        match_repository: MatchRepository,
        standings_repository: StandingsRepository,
    ) -> None:
        self._team_repository = team_repository
        self._competition_repository = competition_repository
        self._venue_repository = venue_repository
        self._match_repository = match_repository
        self._standings_repository = standings_repository

    async def apply(
        self,
        payload: AsbImportRequest,
        bundle: AsbImportBundle,
    ) -> dict[str, object]:
        team = await self._team_repository.get_by_slug(payload.team)
        if team is None:
            raise NotFoundError("Team not found")
        competition = await self._upsert_competition(payload)

        matches_created = 0
        matches_updated = 0
        matches_unchanged = 0
        venues_created = 0
        for game in bundle.games:
            venue, venue_created = await self._upsert_venue(game)
            venues_created += int(venue_created)
            outcome = await self._upsert_match(
                game=game,
                team=team,
                competition=competition,
                venue=venue,
                external_team_id=payload.external_team_id,
            )
            if outcome == "created":
                matches_created += 1
            elif outcome == "updated":
                matches_updated += 1
            else:
                matches_unchanged += 1

        standings_published = await self._publish_standings(
            payload=payload,
            bundle=bundle,
            team=team,
            competition=competition,
        )
        return {
            "games_received": len(bundle.games),
            "standings_rows_received": len(bundle.standings),
            "matches_created": matches_created,
            "matches_updated": matches_updated,
            "matches_unchanged": matches_unchanged,
            "venues_created": venues_created,
            "standings_published": standings_published,
        }

    async def _upsert_competition(self, payload: AsbImportRequest) -> Competition:
        competition = await self._competition_repository.get_by_external_identity(
            "asb",
            payload.competition_id,
        )
        if competition is None:
            return await self._competition_repository.add(
                Competition(
                    name=payload.competition_name,
                    season=payload.season,
                    source="asb",
                    external_id=payload.competition_id,
                    active=True,
                )
            )
        competition.name = payload.competition_name
        competition.season = payload.season
        competition.active = True
        competition.is_deleted = False
        competition.deleted_at = None
        return await self._competition_repository.save(competition)

    async def _upsert_venue(self, game: AsbGame) -> tuple[Venue | None, bool]:
        if not game.arena_id:
            return None, False
        external_id = str(game.arena_id)
        name = (game.arena_name or f"Площадка ASB {external_id}").strip()
        venue = await self._venue_repository.get_by_external_identity("asb", external_id)
        if venue is None:
            venue = await self._venue_repository.add(
                Venue(
                    name=name,
                    address="Адрес не указан (ASB)",
                    description="Импортировано из ASB; адрес требует уточнения",
                    source="asb",
                    external_id=external_id,
                    active=True,
                )
            )
            return venue, True
        venue.name = name
        venue.active = True
        venue.is_deleted = False
        venue.deleted_at = None
        return await self._venue_repository.save(venue), False

    async def _upsert_match(
        self,
        *,
        game: AsbGame,
        team: Team,
        competition: Competition,
        venue: Venue | None,
        external_team_id: str,
    ) -> str:
        starts_at = parse_asb_datetime(game.game_datetime_moscow)
        status = match_status(game.game_status, starts_at)
        values: dict[str, object] = {
            "team_id": team.id,
            "competition_id": competition.id,
            "venue_id": venue.id if venue is not None else None,
            "starts_at": starts_at,
            "club_side": (
                ClubSide.HOME if str(game.home_team_id) == external_team_id else ClubSide.AWAY
            ),
            "home_team_name": game.home_team_name.strip(),
            "away_team_name": game.away_team_name.strip(),
            "home_score": game.home_score if status is MatchStatus.FINISHED else None,
            "away_score": game.away_score if status is MatchStatus.FINISHED else None,
            "status": status,
            "source": "asb",
            "external_id": str(game.game_id),
            "notes": game.phase_name.strip() if game.phase_name else None,
        }
        existing = await self._match_repository.get_by_external_identity(
            "asb",
            str(game.game_id),
        )
        if existing is None:
            await self._match_repository.add(Match(**values))
            return "created"

        changed = existing.is_deleted
        for field, value in values.items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        existing.is_deleted = False
        existing.deleted_at = None
        if not changed:
            return "unchanged"
        await self._match_repository.save(existing)
        return "updated"

    async def _publish_standings(
        self,
        *,
        payload: AsbImportRequest,
        bundle: AsbImportBundle,
        team: Team,
        competition: Competition,
    ) -> bool:
        standings_payload = StandingsPublish(
            team_id=team.id,
            competition_id=competition.id,
            rows=[
                StandingRow(
                    position=row.position,
                    team_name=row.team_name.name,
                    external_team_id=str(row.team_id),
                    played=row.stats.played,
                    wins=row.stats.wins,
                    losses=row.stats.losses,
                    draws=row.stats.draws or 0,
                    table_points=row.stats.table_points,
                    points_for=row.stats.points_for,
                    points_against=row.stats.points_against,
                )
                for row in bundle.standings
            ],
            source="asb",
            source_reference=payload.resolved_standings_competition_id,
            fetched_at=datetime.now(UTC),
        )
        rows = [
            row.model_dump(mode="json")
            for row in sorted(standings_payload.rows, key=lambda item: item.position)
        ]
        current = await self._standings_repository.get_current_for_pair(
            team_id=team.id,
            competition_id=competition.id,
        )
        if (
            current is not None
            and current.rows == rows
            and current.source == "asb"
            and current.source_reference == payload.resolved_standings_competition_id
        ):
            return False
        await self._standings_repository.archive_current(
            team_id=team.id,
            competition_id=competition.id,
        )
        await self._standings_repository.add(
            StandingsSnapshot(
                team_id=team.id,
                competition_id=competition.id,
                rows=rows,
                source="asb",
                source_reference=payload.resolved_standings_competition_id,
                fetched_at=standings_payload.fetched_at,
                is_current=True,
            )
        )
        return True
