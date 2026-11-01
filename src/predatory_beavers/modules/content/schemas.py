from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    UrlConstraints,
    field_validator,
    model_validator,
)

from predatory_beavers.modules.content.models import ContentSection


def validate_stored_url_length(value: HttpUrl) -> HttpUrl:
    if len(str(value)) > 2048:
        raise ValueError("The normalized URL cannot exceed 2048 characters")
    return value


ContentLinkUrl = Annotated[
    HttpUrl,
    UrlConstraints(max_length=2048),
    AfterValidator(validate_stored_url_length),
]


class ContentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: ContentSection
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=30000)
    subtitle: str | None = Field(default=None, max_length=200)
    link_url: ContentLinkUrl | None = None
    link_label: str | None = Field(default=None, max_length=100)
    sort_order: int = Field(default=0, ge=0, le=2_147_483_647)
    active: bool = True

    @field_validator("title", "body")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Text cannot be blank")
        return value

    @field_validator("subtitle", "link_url", "link_label", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


class ContentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: ContentSection | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=30000)
    subtitle: str | None = Field(default=None, max_length=200)
    link_url: ContentLinkUrl | None = None
    link_label: str | None = Field(default=None, max_length=100)
    sort_order: int | None = Field(default=None, ge=0, le=2_147_483_647)
    active: bool | None = None

    @field_validator("title", "body")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ContentCreate.strip_text(value)

    @field_validator("subtitle", "link_url", "link_label", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        return ContentCreate.strip_optional_text(value)

    @model_validator(mode="after")
    def reject_null_fields(self) -> "ContentUpdate":
        for field in self.model_fields_set:
            if field not in {"subtitle", "link_url", "link_label"} and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ContentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    section: ContentSection
    title: str
    body: str
    subtitle: str | None = None
    link_url: ContentLinkUrl | None = None
    link_label: str | None = None
    sort_order: int
    active: bool
    created_at: datetime
    updated_at: datetime


class ContentList(BaseModel):
    items: list[ContentRead]
