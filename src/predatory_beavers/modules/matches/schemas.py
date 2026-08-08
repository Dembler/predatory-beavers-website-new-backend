from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from predatory_beavers.modules.club.schemas import MediaAssetRead, TeamSummary
from predatory_beavers.modules.matches.models import ClubSide, MatchStatus


class MatchesSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CompetitionRead(MatchesSchema):
    id: UUID
    name: str
    season: str
    source: str
    external_id: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class CompetitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    season: str = Field(min_length=1, max_length=32)
    source: str = Field(default="manual", min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    external_id: str | None = Field(default=None, max_length=128)
    active: bool = True

    @field_validator("name", "season")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("field cannot be blank")
        return stripped

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("external_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def external_source_requires_id(self) -> "CompetitionCreate":
        if self.source != "manual" and not self.external_id:
            raise ValueError("external_id is required for a non-manual source")
        return self


class CompetitionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    season: str | None = Field(default=None, min_length=1, max_length=32)
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9_-]+$",
    )
    external_id: str | None = Field(default=None, max_length=128)
    active: bool | None = None

    @field_validator("name", "season", "source", "active", mode="before")
    @classmethod
    def required_fields_cannot_be_cleared(cls, value: object) -> object:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("name", "season")
    @classmethod
    def strip_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("field cannot be blank")
        return stripped

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str | None) -> str | None:
        return value.strip().lower() if value is not None else None

    @field_validator("external_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class VenueRead(MatchesSchema):
    id: UUID
    name: str
    address: str
    latitude: float | None
    longitude: float | None
    description: str | None
    source: str
    external_id: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class VenueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    description: str | None = Field(default=None, max_length=2000)
    source: str = Field(default="manual", min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    external_id: str | None = Field(default=None, max_length=128)
    active: bool = True

    @field_validator("name", "address")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("field cannot be blank")
        return stripped

    @field_validator("description")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("external_id")
    @classmethod
    def strip_external_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def coordinates_are_a_pair(self) -> "VenueCreate":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        if self.source != "manual" and not self.external_id:
            raise ValueError("external_id is required for a non-manual source")
        return self


class VenueUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, min_length=1, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    description: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9_-]+$",
    )
    external_id: str | None = Field(default=None, max_length=128)
    active: bool | None = None

    @field_validator("name", "address", "source", "active", mode="before")
    @classmethod
    def required_fields_cannot_be_cleared(cls, value: object) -> object:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("name", "address")
    @classmethod
    def strip_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("field cannot be blank")
        return stripped

    @field_validator("description")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str | None) -> str | None:
        return value.strip().lower() if value is not None else None

    @field_validator("external_id")
    @classmethod
    def strip_external_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class MatchRead(MatchesSchema):
    id: UUID
    team_id: UUID
    team: TeamSummary
    competition_id: UUID
    competition: CompetitionRead
    venue_id: UUID | None
    venue: VenueRead | None
    starts_at: datetime
    club_side: ClubSide
    home_team_name: str
    away_team_name: str
    home_score: int | None
    away_score: int | None
    status: MatchStatus
    source: str
    external_id: str | None
    home_logo_asset_id: UUID | None
    away_logo_asset_id: UUID | None
    home_logo: MediaAssetRead | None
    away_logo: MediaAssetRead | None
    notes: str | None
    featured: bool
    created_at: datetime
    updated_at: datetime


class MatchCreate(BaseModel):
    team_id: UUID
    competition_id: UUID
    venue_id: UUID | None = None
    starts_at: datetime
    club_side: ClubSide
    home_team_name: str = Field(min_length=1, max_length=200)
    away_team_name: str = Field(min_length=1, max_length=200)
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    status: MatchStatus = MatchStatus.SCHEDULED
    source: str = Field(default="manual", min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    external_id: str | None = Field(default=None, max_length=128)
    home_logo_asset_id: UUID | None = None
    away_logo_asset_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)
    featured: bool = False

    @field_validator("starts_at")
    @classmethod
    def starts_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("starts_at must include a timezone")
        return value

    @field_validator("home_team_name", "away_team_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("team name cannot be blank")
        return stripped

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("external_id", "notes")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_source_and_score(self) -> "MatchCreate":
        if self.source != "manual" and not self.external_id:
            raise ValueError("external_id is required for a non-manual source")
        _validate_score(self.status, self.home_score, self.away_score)
        return self


class MatchUpdate(BaseModel):
    team_id: UUID | None = None
    competition_id: UUID | None = None
    venue_id: UUID | None = None
    starts_at: datetime | None = None
    club_side: ClubSide | None = None
    home_team_name: str | None = Field(default=None, min_length=1, max_length=200)
    away_team_name: str | None = Field(default=None, min_length=1, max_length=200)
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    status: MatchStatus | None = None
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9_-]+$",
    )
    external_id: str | None = Field(default=None, max_length=128)
    home_logo_asset_id: UUID | None = None
    away_logo_asset_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)
    featured: bool | None = None

    @field_validator(
        "team_id",
        "competition_id",
        "starts_at",
        "club_side",
        "home_team_name",
        "away_team_name",
        "status",
        "source",
        "featured",
        mode="before",
    )
    @classmethod
    def required_fields_cannot_be_cleared(cls, value: object) -> object:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("starts_at")
    @classmethod
    def starts_at_requires_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("starts_at must include a timezone")
        return value

    @field_validator("home_team_name", "away_team_name")
    @classmethod
    def strip_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("team name cannot be blank")
        return stripped

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str | None) -> str | None:
        return value.strip().lower() if value is not None else None

    @field_validator("external_id", "notes")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _validate_score(
    status: MatchStatus,
    home_score: int | None,
    away_score: int | None,
) -> None:
    if (home_score is None) != (away_score is None):
        raise ValueError("home_score and away_score must be provided together")
    if status is MatchStatus.FINISHED and home_score is None:
        raise ValueError("a finished match must have a score")
    if status in {MatchStatus.SCHEDULED, MatchStatus.POSTPONED, MatchStatus.CANCELLED}:
        if home_score is not None:
            raise ValueError(f"a {status.value} match cannot have a score")
