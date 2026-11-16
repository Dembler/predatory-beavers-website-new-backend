from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from predatory_beavers.api.idempotency import IdempotentCreate
from predatory_beavers.modules.editorial.generation import ColumnDraft


class GenerateRequest(IdempotentCreate):
    week_start: date
    team_scope: Literal["men", "women", "both"] = "both"
    discipline: Literal["5x5", "3x3", "phygital", "all"] = "all"
    brief: str = Field(default="", max_length=1500)

    @field_validator("week_start")
    @classmethod
    def monday(cls, value: date) -> date:
        if value.weekday() != 0:
            raise ValueError("week_start must be a Monday")
        if value > date.max - timedelta(days=6):
            raise ValueError("week_start must allow a complete calendar week")
        return value


class RevisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    revision_id: UUID


class EditRequest(RevisionAction):
    content: ColumnDraft
