from uuid import UUID

from sqlalchemy.exc import IntegrityError

from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.matches.repository import CompetitionRepository
from predatory_beavers.modules.standings.models import StandingsSnapshot
from predatory_beavers.modules.standings.repository import StandingsRepository
from predatory_beavers.modules.standings.schemas import StandingsPublish, StandingsSnapshotRead


class StandingsService:
    def __init__(
        self,
        repository: StandingsRepository,
        team_repository: TeamRepository,
        competition_repository: CompetitionRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService | None = None,
    ) -> None:
        self._repository = repository
        self._team_repository = team_repository
        self._competition_repository = competition_repository
        self._unit_of_work = unit_of_work
        self._audit_service = audit_service

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        team: str | None = None,
        season: str | None = None,
        is_current: bool | None = None,
    ) -> tuple[list[StandingsSnapshot], int]:
        return await self._repository.list(
            page=page,
            page_size=page_size,
            team=team,
            season=season,
            is_current=is_current,
        )

    async def get(self, snapshot_id: UUID) -> StandingsSnapshot:
        snapshot = await self._repository.get(snapshot_id)
        if snapshot is None:
            raise NotFoundError("Standings snapshot not found")
        return snapshot

    async def get_public(
        self,
        *,
        team: str,
        season: str | None = None,
        competition_id: UUID | None = None,
    ) -> StandingsSnapshot:
        snapshot = await self._repository.get_current(
            team=team,
            season=season,
            competition_id=competition_id,
        )
        if snapshot is None:
            raise NotFoundError("Standings not found")
        return snapshot

    async def publish(self, payload: StandingsPublish) -> StandingsSnapshot:
        try:
            async with self._unit_of_work:
                if await self._team_repository.get(payload.team_id) is None:
                    raise NotFoundError("Team not found")
                if await self._competition_repository.get(payload.competition_id) is None:
                    raise NotFoundError("Competition not found")

                await self._repository.archive_current(
                    team_id=payload.team_id,
                    competition_id=payload.competition_id,
                )
                snapshot = StandingsSnapshot(
                    team_id=payload.team_id,
                    competition_id=payload.competition_id,
                    rows=[
                        row.model_dump(mode="json")
                        for row in sorted(payload.rows, key=lambda item: item.position)
                    ],
                    source=payload.source,
                    source_reference=payload.source_reference,
                    fetched_at=payload.fetched_at,
                    is_current=True,
                )
                created = await self._repository.add(snapshot)
                await self._record(
                    action=AuditAction.PUBLISH,
                    snapshot=created,
                    after=StandingsSnapshotRead.model_validate(created).model_dump(mode="json"),
                )
                await self._unit_of_work.commit()
                return created
        except IntegrityError as exc:
            raise ConflictError("Standings were updated concurrently; retry publication") from exc

    async def delete(self, snapshot_id: UUID) -> None:
        async with self._unit_of_work:
            snapshot = await self.get(snapshot_id)
            before = StandingsSnapshotRead.model_validate(snapshot).model_dump(mode="json")
            await self._repository.soft_delete(snapshot)
            await self._record(
                action=AuditAction.DELETE,
                snapshot=snapshot,
                before=before,
            )
            await self._unit_of_work.commit()

    async def _record(
        self,
        *,
        action: AuditAction,
        snapshot: StandingsSnapshot,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        if self._audit_service is not None:
            await self._audit_service.record(
                action=action,
                entity_type="standings_snapshot",
                entity_id=snapshot.id,
                before=before,
                after=after,
            )
