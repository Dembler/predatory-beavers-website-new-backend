from datetime import datetime
from uuid import UUID

from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.matches.models import Competition, Match, MatchStatus, Venue
from predatory_beavers.modules.matches.repository import (
    CompetitionRepository,
    MatchRepository,
    VenueRepository,
)
from predatory_beavers.modules.matches.schemas import MatchCreate, MatchRead, MatchUpdate


class MatchService:
    def __init__(
        self,
        repository: MatchRepository,
        team_repository: TeamRepository,
        competition_repository: CompetitionRepository,
        venue_repository: VenueRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService | None = None,
    ) -> None:
        self._repository = repository
        self._team_repository = team_repository
        self._competition_repository = competition_repository
        self._venue_repository = venue_repository
        self._unit_of_work = unit_of_work
        self._audit_service = audit_service

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
        for boundary in (starts_from, starts_to):
            if boundary is not None and boundary.utcoffset() is None:
                raise ConflictError("Match date filters must include a timezone")
        if starts_from is not None and starts_to is not None and starts_from > starts_to:
            raise ConflictError("starts_from must not be later than starts_to")
        return await self._repository.list(
            page=page,
            page_size=page_size,
            team=team,
            status=status,
            starts_from=starts_from,
            starts_to=starts_to,
            featured=featured,
            public_only=public_only,
        )

    async def get(self, match_id: UUID) -> Match:
        match = await self._repository.get(match_id)
        if match is None:
            raise NotFoundError("Match not found")
        return match

    async def get_public(self, match_id: UUID) -> Match:
        match = await self._repository.get(match_id, public_only=True)
        if match is None:
            raise NotFoundError("Match not found")
        return match

    async def create(self, payload: MatchCreate) -> Match:
        async with self._unit_of_work:
            await self._require_references(
                payload.team_id,
                payload.competition_id,
                payload.venue_id,
            )
            await self._ensure_external_identity_available(payload.source, payload.external_id)
            match = Match(**payload.model_dump())
            self._validate_match(match)
            created = await self._repository.add(match)
            await self._record(
                action=AuditAction.CREATE,
                match=created,
                after=MatchRead.model_validate(created).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return created

    async def update(self, match_id: UUID, payload: MatchUpdate) -> Match:
        async with self._unit_of_work:
            match = await self.get(match_id)
            before = MatchRead.model_validate(match).model_dump(mode="json")
            changes = payload.model_dump(exclude_unset=True)
            team_id = changes.get("team_id", match.team_id)
            competition_id = changes.get("competition_id", match.competition_id)
            venue_id = changes.get("venue_id", match.venue_id)
            if (
                team_id != match.team_id
                or competition_id != match.competition_id
                or venue_id != match.venue_id
            ):
                await self._require_references(team_id, competition_id, venue_id)

            source = changes.get("source", match.source)
            external_id = changes.get("external_id", match.external_id)
            await self._ensure_external_identity_available(
                source,
                external_id,
                exclude_id=match.id,
            )
            for field, value in changes.items():
                setattr(match, field, value)
            self._validate_match(match)
            updated = await self._repository.save(match)
            await self._record(
                action=AuditAction.UPDATE,
                match=updated,
                before=before,
                after=MatchRead.model_validate(updated).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return updated

    async def delete(self, match_id: UUID) -> None:
        async with self._unit_of_work:
            match = await self.get(match_id)
            before = MatchRead.model_validate(match).model_dump(mode="json")
            await self._repository.soft_delete(match)
            await self._record(action=AuditAction.DELETE, match=match, before=before)
            await self._unit_of_work.commit()

    async def _require_references(
        self,
        team_id: UUID,
        competition_id: UUID,
        venue_id: UUID | None,
    ) -> tuple[Team, Competition, Venue | None]:
        team = await self._team_repository.get(team_id)
        if team is None:
            raise NotFoundError("Team not found")
        competition = await self._competition_repository.get(competition_id)
        if competition is None:
            raise NotFoundError("Competition not found")
        venue = None
        if venue_id is not None:
            venue = await self._venue_repository.get(venue_id)
            if venue is None:
                raise NotFoundError("Venue not found")
        return team, competition, venue

    async def _ensure_external_identity_available(
        self,
        source: str,
        external_id: str | None,
        *,
        exclude_id: UUID | None = None,
    ) -> None:
        if source != "manual" and not external_id:
            raise ConflictError("External matches require an external_id")
        if external_id is None:
            return
        existing = await self._repository.get_by_external_identity(source, external_id)
        if existing is not None and existing.id != exclude_id:
            raise ConflictError("Match external identity already exists")

    @staticmethod
    def _validate_match(match: Match) -> None:
        if (match.home_score is None) != (match.away_score is None):
            raise ConflictError("home_score and away_score must be provided together")
        if match.status is MatchStatus.FINISHED and match.home_score is None:
            raise ConflictError("A finished match must have a score")
        if (
            match.status
            in {
                MatchStatus.SCHEDULED,
                MatchStatus.POSTPONED,
                MatchStatus.CANCELLED,
            }
            and match.home_score is not None
        ):
            raise ConflictError(f"A {match.status.value} match cannot have a score")

    async def _record(
        self,
        *,
        action: AuditAction,
        match: Match,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        if self._audit_service is not None:
            await self._audit_service.record(
                action=action,
                entity_type="match",
                entity_id=match.id,
                before=before,
                after=after,
            )
