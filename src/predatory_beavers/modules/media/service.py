import asyncio
import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from predatory_beavers.api.errors import NotFoundError, ServiceUnavailableError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.integrations.object_storage.local import ObjectNotFoundError
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.models import MediaAsset
from predatory_beavers.modules.club.schemas import MediaAssetRead
from predatory_beavers.modules.media.errors import (
    InvalidImageError,
    MediaTooLargeError,
    UnsupportedImageError,
)
from predatory_beavers.modules.media.repository import MediaRepository
from predatory_beavers.settings import Settings

SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
OUTPUT_MIME = "image/webp"


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    content: bytes
    width: int
    height: int
    checksum: str


@dataclass(frozen=True, slots=True)
class MediaContent:
    content: bytes
    mime: str
    checksum: str


class MediaService:
    def __init__(
        self,
        repository: MediaRepository,
        storage: ObjectStorage,
        unit_of_work: UnitOfWork,
        settings: Settings,
        audit_service: AuditService | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._unit_of_work = unit_of_work
        self._settings = settings
        self._audit_service = audit_service

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.media_max_upload_bytes

    async def upload_image(self, content: bytes, alt_text: str | None) -> MediaAsset:
        if len(content) > self._settings.media_max_upload_bytes:
            raise MediaTooLargeError
        normalized_alt = alt_text.strip() if alt_text is not None else None
        if normalized_alt == "":
            normalized_alt = None
        if normalized_alt is not None and len(normalized_alt) > 500:
            raise InvalidImageError("alt_text must not exceed 500 characters")

        processed = await asyncio.to_thread(
            _process_image,
            content,
            self._settings.media_max_dimension,
            self._settings.media_webp_quality,
        )
        async with self._unit_of_work:
            existing = await self._repository.get_by_checksum(processed.checksum)
            if existing is not None:
                return existing

        storage_key = f"images/{uuid4().hex}.webp"
        await self._storage.put(
            key=storage_key,
            content=processed.content,
            content_type=OUTPUT_MIME,
        )
        asset = MediaAsset(
            storage_key=storage_key,
            mime=OUTPUT_MIME,
            size=len(processed.content),
            width=processed.width,
            height=processed.height,
            checksum=processed.checksum,
            alt_text=normalized_alt,
        )
        try:
            async with self._unit_of_work:
                created = await self._repository.add(asset)
                if self._audit_service is not None:
                    await self._audit_service.record(
                        action=AuditAction.UPLOAD,
                        entity_type="media_asset",
                        entity_id=created.id,
                        after=MediaAssetRead.model_validate(created).model_dump(mode="json"),
                    )
                await self._unit_of_work.commit()
                return created
        except BaseException:
            await self._storage.delete(key=storage_key)
            raise

    async def get(self, asset_id: UUID) -> MediaAsset:
        asset = await self._repository.get(asset_id)
        if asset is None:
            raise NotFoundError("Media asset not found")
        return asset

    async def get_content(self, asset_id: UUID) -> MediaContent:
        asset = await self.get(asset_id)
        try:
            content = await self._storage.read(key=asset.storage_key)
        except ObjectNotFoundError as exc:
            raise ServiceUnavailableError("Media content is temporarily unavailable") from exc
        return MediaContent(content=content, mime=asset.mime, checksum=asset.checksum)


def _process_image(content: bytes, max_dimension: int, quality: int) -> ProcessedImage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as probe:
                image_format = probe.format
                width, height = probe.size
                animated = bool(getattr(probe, "is_animated", False))
                probe.verify()
        if image_format not in SUPPORTED_FORMATS or animated:
            raise UnsupportedImageError
        if width <= 0 or height <= 0 or width * height > (max_dimension * max_dimension * 4):
            raise InvalidImageError("Image dimensions are not allowed")

        with Image.open(BytesIO(content)) as source:
            source.load()
            normalized = ImageOps.exif_transpose(source)
            normalized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            if "A" in normalized.getbands():
                normalized = normalized.convert("RGBA")
            else:
                normalized = normalized.convert("RGB")
            output = BytesIO()
            normalized.save(output, format="WEBP", quality=quality, method=6)
            encoded = output.getvalue()
            result_width, result_height = normalized.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InvalidImageError("Image dimensions are not allowed") from None
    except UnidentifiedImageError:
        raise UnsupportedImageError from None
    except (OSError, ValueError) as exc:
        raise InvalidImageError from exc

    return ProcessedImage(
        content=encoded,
        width=result_width,
        height=result_height,
        checksum=hashlib.sha256(encoded).hexdigest(),
    )
