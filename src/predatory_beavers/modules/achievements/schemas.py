from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from predatory_beavers.modules.club.schemas import MediaAssetRead, TeamSummary


class AchievementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    team_id: UUID
    team: TeamSummary
    title: str
    media_asset_id: UUID
    media: MediaAssetRead
    achieved_at: date | None
    sort_order: int
    active: bool
    created_at: datetime
    updated_at: datetime


class AchievementCreate(BaseModel):
    team_id: UUID
    title: str = Field(min_length=1, max_length=300)
    media_asset_id: UUID
    achieved_at: date | None = None
    sort_order: int = Field(default=0, ge=0)
    active: bool = True

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title cannot be blank")
        return stripped


class AchievementUpdate(BaseModel):
    team_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    media_asset_id: UUID | None = None
    achieved_at: date | None = None
    sort_order: int | None = Field(default=None, ge=0)
    active: bool | None = None

    @field_validator(
        "team_id",
        "title",
        "media_asset_id",
        "sort_order",
        "active",
        mode="before",
    )
    @classmethod
    def required_fields_cannot_be_cleared(cls, value: object) -> object:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("title cannot be blank")
        return stripped
