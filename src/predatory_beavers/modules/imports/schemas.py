from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from predatory_beavers.modules.imports.models import ImportStatus


class AsbImportRequest(BaseModel):
    team: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    competition_id: str = Field(min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$")
    standings_competition_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[1-9][0-9]*$",
    )
    external_team_id: str = Field(min_length=1, max_length=128, pattern=r"^[1-9][0-9]*$")
    season: str = Field(min_length=9, max_length=32, pattern=r"^[0-9]{4}-[0-9]{4}$")
    competition_name: str = Field(default="АСБ", min_length=1, max_length=200)

    @field_validator("competition_name")
    @classmethod
    def strip_competition_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("competition_name cannot be blank")
        return stripped

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_standings_competition_id(self) -> str:
        return self.standings_competition_id or self.competition_id


class ImportJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    status: ImportStatus
    team_slug: str
    competition_external_id: str
    standings_external_id: str
    external_team_id: str
    season: str
    request_data: dict[str, object]
    result: dict[str, object] | None
    error_code: str | None
    error_detail: str | None
    actor_id: UUID | None
    actor_username: str
    request_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
