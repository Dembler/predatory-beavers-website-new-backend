from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    computed_field,
    field_validator,
    model_validator,
)

from predatory_beavers.modules.club.schemas import TeamSummary
from predatory_beavers.modules.matches.schemas import CompetitionRead


class StandingRow(BaseModel):
    position: int = Field(ge=1)
    team_name: str = Field(min_length=1, max_length=200)
    external_team_id: str | None = Field(default=None, max_length=128)
    team_logo_url: str | None = None
    played: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    draws: int = Field(default=0, ge=0)
    table_points: int = Field(ge=0)
    points_for: int = Field(ge=0)
    points_against: int = Field(ge=0)

    @field_validator("team_logo_url", mode="before")
    @classmethod
    def validate_logo_url(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("/api/v1/standings/logos/"):
            return value
        return str(TypeAdapter(HttpUrl).validate_python(value))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def points_difference(self) -> int:
        return self.points_for - self.points_against

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


class StandingsGame(BaseModel):
    external_id: str
    round_name: str
    round: int | None = None
    tree: int | None = None
    sort_order: int = 0
    starts_at: datetime | None = None
    date_precision: Literal["day", "time"] = "day"
    home_team_name: str
    home_team_id: str | None = None
    home_team_logo_url: str | None = None
    away_team_name: str
    away_team_id: str | None = None
    away_team_logo_url: str | None = None
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    status: Literal["finished", "scheduled", "unpublished"]
    source_url: HttpUrl

    @model_validator(mode="after")
    def validate_score(self) -> "StandingsGame":
        if (self.home_score is None) != (self.away_score is None):
            raise ValueError("Both scores must be present or absent")
        if self.status == "finished" and self.home_score is None:
            raise ValueError("Finished playoff games require confirmed scores")
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
    source_url: str | None
    last_game_at: datetime | None
    stage_order: int
    tournament_id: str | None = None
    tournament_name: str | None = None
    stage_name: str | None = None
    stage_kind: str = "other"
    games: list[StandingsGame] = Field(default_factory=list)
    fetched_at: datetime
    is_current: bool
    created_at: datetime
    updated_at: datetime


class StandingsCompetitionOption(BaseModel):
    id: UUID
    name: str
    season: str
    snapshot_id: UUID
    row_count: int
    source: str
    source_reference: str | None
    source_url: str | None
    tournament_id: str | None = None
    tournament_name: str | None = None
    stage_name: str | None = None
    stage_kind: str = "other"
    game_count: int = 0


class StandingsOptions(BaseModel):
    team: str
    seasons: list[str]
    competitions: list[StandingsCompetitionOption]
    default_competition_id: UUID | None
