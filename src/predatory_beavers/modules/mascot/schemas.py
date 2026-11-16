"""Request/response schemas for the mascot prototype (unwrapped JSON)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MascotContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: str | None = Field(default=None, max_length=64)
    team: Literal["men", "women"] | None = None


class ChatRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: UUID
    session_id: UUID
    message: str = Field(min_length=1, max_length=100_000)
    context: MascotContext | None = None

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message cannot be blank")
        return stripped


class SessionRead(BaseModel):
    session_id: UUID
    csrf_token: str
    expires_at: datetime


class ChatRunAccepted(BaseModel):
    run_id: UUID
    session_id: UUID
    status: str
    events_url: str


class CancelResult(BaseModel):
    run_id: UUID
    status: str


class RunStatusRead(BaseModel):
    run_id: UUID
    status: str
    last_seq: int
