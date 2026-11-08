from pydantic import BaseModel, ConfigDict, Field, field_validator

from predatory_beavers.api.concurrency import VersionedUpdate
from predatory_beavers.modules.club.schemas import MediaAssetRead


class MediaUpdate(VersionedUpdate):
    model_config = ConfigDict(extra="forbid")

    alt_text: str | None = Field(default=None, max_length=500)

    @field_validator("alt_text")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class MediaUploadRead(MediaAssetRead):
    reused: bool = False


class MediaUsage(BaseModel):
    entity_type: str
    entity_id: str
    title: str
    field: str
    admin_url: str
    public_url: str | None
