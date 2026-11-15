import re
from datetime import datetime
from typing import Annotated
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    field_validator,
    model_validator,
)

from predatory_beavers.api.concurrency import VersionedUpdate
from predatory_beavers.api.idempotency import IdempotentCreate
from predatory_beavers.modules.club.schemas import MediaAssetRead
from predatory_beavers.modules.content.models import ContentSection


def validate_stored_url_length(value: HttpUrl) -> HttpUrl:
    if len(str(value)) > 2048:
        raise ValueError("The normalized URL cannot exceed 2048 characters")
    return value


def validate_contact_url(value: str) -> str:
    if "\\" in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("URL cannot contain whitespace or control characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in unquote(value)):
        raise ValueError("URL cannot contain encoded control characters")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme in {"http", "https"}:
        if parts.username is not None or parts.password is not None:
            raise ValueError("URL cannot contain credentials")
        normalized = str(TypeAdapter(HttpUrl).validate_python(value))
        if len(normalized) > 2048:
            raise ValueError("The normalized URL cannot exceed 2048 characters")
        return normalized
    if scheme == "tel" and not (parts.netloc or parts.query or parts.fragment):
        if re.fullmatch(r"\+?[0-9][0-9().-]{2,30}", parts.path):
            return value
    if scheme == "mailto" and not (parts.netloc or parts.query or parts.fragment):
        if re.fullmatch(r"[^@\s,;<>?\"]+@[^@\s,;<>?\"]+\.[^@\s,;<>?\"]+", parts.path):
            return value
    raise ValueError("Use an http, https, tel or mailto URL")


ContentLinkUrl = Annotated[str, Field(max_length=2048), AfterValidator(validate_contact_url)]


class ContentContact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=500)
    url: ContentLinkUrl | None = None

    @field_validator("url", mode="before")
    @classmethod
    def blank_url(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


class ContentCreate(IdempotentCreate):
    model_config = ConfigDict(extra="forbid")

    section: ContentSection
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=30000)
    subtitle: str | None = Field(default=None, max_length=200)
    link_url: ContentLinkUrl | None = None
    link_label: str | None = Field(default=None, max_length=100)
    photo_asset_id: UUID | None = None
    contacts: list[ContentContact] = Field(default_factory=list, max_length=20)
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


class ContentUpdate(VersionedUpdate):
    model_config = ConfigDict(extra="forbid")

    section: ContentSection | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=30000)
    subtitle: str | None = Field(default=None, max_length=200)
    link_url: ContentLinkUrl | None = None
    link_label: str | None = Field(default=None, max_length=100)
    photo_asset_id: UUID | None = None
    contacts: list[ContentContact] = Field(default_factory=list, max_length=20)
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
            if (
                field
                not in {
                    "subtitle",
                    "link_url",
                    "link_label",
                    "photo_asset_id",
                    "expected_updated_at",
                }
                and getattr(self, field) is None
            ):
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
    photo_asset_id: UUID | None = None
    photo: MediaAssetRead | None = None
    contacts: list[ContentContact] = Field(default_factory=list)
    sort_order: int
    active: bool
    created_at: datetime
    updated_at: datetime


class ContentList(BaseModel):
    items: list[ContentRead]
