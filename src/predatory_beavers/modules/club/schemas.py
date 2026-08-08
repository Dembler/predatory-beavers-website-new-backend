from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from predatory_beavers.modules.club.models import TeamCategory


class ClubSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MediaAssetRead(ClubSchema):
    id: UUID
    mime: str
    size: int
    width: int | None
    height: int | None
    alt_text: str | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_url(self) -> str:
        return f"/media/{self.id}/content"


class TeamSummary(ClubSchema):
    id: UUID
    slug: str
    name: str
    category: TeamCategory
    active: bool


class TeamRead(TeamSummary):
    logo_asset_id: UUID | None
    logo: MediaAssetRead | None
    created_at: datetime
    updated_at: datetime


class PlayerRead(ClubSchema):
    id: UUID
    team_id: UUID
    team: TeamSummary
    full_name: str
    birth_date: date | None
    age_text: str | None
    position: str | None
    fact: str | None
    photo_asset_id: UUID | None
    photo: MediaAssetRead | None
    sort_order: int
    active: bool
    created_at: datetime
    updated_at: datetime


class PlayerCreate(BaseModel):
    team_id: UUID
    full_name: str = Field(min_length=1, max_length=200)
    birth_date: date | None = None
    age_text: str | None = Field(default=None, max_length=64)
    position: str | None = Field(default=None, max_length=100)
    fact: str | None = Field(default=None, max_length=5000)
    photo_asset_id: UUID | None = None
    sort_order: int = Field(default=0, ge=0)
    active: bool = True

    @field_validator("full_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("full_name cannot be blank")
        return stripped

    @field_validator("age_text", "position", "fact")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("birth_date")
    @classmethod
    def birth_date_cannot_be_future(cls, value: date | None) -> date | None:
        if value is not None and value > date.today():
            raise ValueError("birth_date cannot be in the future")
        return value


class PlayerUpdate(BaseModel):
    team_id: UUID | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    birth_date: date | None = None
    age_text: str | None = Field(default=None, max_length=64)
    position: str | None = Field(default=None, max_length=100)
    fact: str | None = Field(default=None, max_length=5000)
    photo_asset_id: UUID | None = None
    sort_order: int | None = Field(default=None, ge=0)
    active: bool | None = None

    @field_validator("team_id", "full_name", "sort_order", "active", mode="before")
    @classmethod
    def required_fields_cannot_be_cleared(cls, value: object) -> object:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("full_name")
    @classmethod
    def strip_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("full_name cannot be blank")
        return stripped

    @field_validator("age_text", "position", "fact")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("birth_date")
    @classmethod
    def birth_date_cannot_be_future(cls, value: date | None) -> date | None:
        if value is not None and value > date.today():
            raise ValueError("birth_date cannot be in the future")
        return value

    @model_validator(mode="after")
    def required_database_fields_cannot_be_null(self) -> "PlayerUpdate":
        for field_name in ("team_id", "full_name", "sort_order", "active"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self
