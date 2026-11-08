from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class TrashEntity(StrEnum):
    PLAYERS = "players"
    ACHIEVEMENTS = "achievements"
    CONTENT = "content"
    COMPETITIONS = "competitions"
    VENUES = "venues"
    MATCHES = "matches"
    MEDIA = "media"


class TrashItem(BaseModel):
    id: UUID
    title: str
    detail: str | None = None
    deleted_at: datetime | None
    updated_at: datetime

    @field_validator("updated_at", "deleted_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def version_has_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value


class RestoreResult(BaseModel):
    id: UUID
    entity: TrashEntity
    restored: bool
    publication_state: Literal["hidden", "private"]
    message: str
