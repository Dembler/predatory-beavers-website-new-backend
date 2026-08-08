from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from predatory_beavers.modules.club.schemas import TeamSummary
from predatory_beavers.modules.matches.schemas import CompetitionRead


class StandingRow(BaseModel):
    position: int = Field(ge=1)
    team_name: str = Field(min_length=1, max_length=200)
    external_team_id: str | None = Field(default=None, max_length=128)
    played: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    draws: int = Field(default=0, ge=0)
    table_points: int = Field(ge=0)
    points_for: int = Field(ge=0)
    points_against: int = Field(ge=0)

    @field_validator("team_name")
    @classmethod
    def strip_team_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("team_name cannot be blank")
        return stripped

    @field_validator("external_team_id")
    @classmethod
    def strip_external_team_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def results_equal_played_games(self) -> "StandingRow":
        if self.wins + self.losses + self.draws != self.played:
            raise ValueError("wins, losses and draws must add up to played")
        return self


class StandingsPublish(BaseModel):
    team_id: UUID
    competition_id: UUID
    rows: list[StandingRow] = Field(min_length=1, max_length=100)
    source: str = Field(default="manual", min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    source_reference: str | None = Field(default=None, max_length=128)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("source_reference")
    @classmethod
    def strip_source_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("fetched_at")
    @classmethod
    def fetched_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "StandingsPublish":
        positions = {row.position for row in self.rows}
        if positions != set(range(1, len(self.rows) + 1)):
            raise ValueError("positions must be unique and contiguous starting at 1")

        names = [row.team_name.casefold() for row in self.rows]
        if len(names) != len(set(names)):
            raise ValueError("team names must be unique within a snapshot")

        if self.source != "manual" and not self.source_reference:
            raise ValueError("source_reference is required for an external source")
        return self


class StandingsSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    team_id: UUID
    team: TeamSummary
    competition_id: UUID
    competition: CompetitionRead
    rows: list[StandingRow]
    source: str
    source_reference: str | None
    fetched_at: datetime
    is_current: bool
    created_at: datetime
    updated_at: datetime
