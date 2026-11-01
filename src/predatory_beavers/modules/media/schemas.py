from pydantic import BaseModel, ConfigDict, Field, field_validator


class MediaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alt_text: str | None = Field(default=None, max_length=500)

    @field_validator("alt_text")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None
