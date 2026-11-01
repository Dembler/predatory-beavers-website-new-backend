from collections.abc import AsyncGenerator
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, Response, status
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.club.schemas import MediaAssetRead
from predatory_beavers.modules.media.errors import InvalidImageError, MediaTooLargeError
from predatory_beavers.modules.media.schemas import MediaUpdate
from predatory_beavers.modules.media.service import MediaService
from predatory_beavers.settings import Settings

content_router = APIRouter(route_class=DishkaRoute, tags=["media"])
public_router = APIRouter(route_class=DishkaRoute, tags=["media"])
admin_router = APIRouter(prefix="/admin/media", route_class=DishkaRoute, tags=["admin-media"])


@admin_router.get("", response_model=PaginatedResponse[MediaAssetRead])
async def list_admin_media(
    request: Request,
    service: FromDishka[MediaService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> PaginatedResponse[MediaAssetRead]:
    await authorizer.require_editor_session(request.cookies.get(settings.session_cookie_name))
    assets, total = await service.list(page=page, page_size=page_size)
    return PaginatedResponse[MediaAssetRead].create(
        items=[MediaAssetRead.model_validate(asset) for asset in assets],
        total=total,
        page=page,
        page_size=page_size,
    )


@admin_router.patch("/{asset_id}", response_model=ApiResponse[MediaAssetRead])
async def update_media(
    asset_id: UUID,
    payload: MediaUpdate,
    request: Request,
    service: FromDishka[MediaService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[MediaAssetRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    asset = await service.update(asset_id, payload)
    return ApiResponse(message="Media updated", data=MediaAssetRead.model_validate(asset))


@admin_router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    asset_id: UUID,
    request: Request,
    service: FromDishka[MediaService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    await service.delete(asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@content_router.get("/media/{asset_id}/content", response_class=Response)
async def get_media_content(
    asset_id: UUID,
    service: FromDishka[MediaService],
) -> Response:
    result = await service.get_content(asset_id)
    return Response(
        content=result.content,
        media_type=result.mime,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{result.checksum}"',
        },
    )


@public_router.get(
    "/media/{asset_id}/content",
    response_class=Response,
    deprecated=True,
)
async def get_legacy_media_content(
    asset_id: UUID,
    service: FromDishka[MediaService],
) -> Response:
    return await get_media_content(asset_id, service)


@admin_router.get("/{asset_id}", response_model=ApiResponse[MediaAssetRead])
async def get_admin_media(
    asset_id: UUID,
    request: Request,
    service: FromDishka[MediaService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[MediaAssetRead]:
    await authorizer.require_editor_session(
        request.cookies.get(settings.session_cookie_name),
    )
    asset = await service.get(asset_id)
    return ApiResponse(message="Media asset retrieved", data=MediaAssetRead.model_validate(asset))


@admin_router.post(
    "",
    response_model=ApiResponse[MediaAssetRead],
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "alt_text": {"type": "string", "maxLength": 500},
                        },
                    }
                }
            },
        },
    },
)
async def upload_media(
    request: Request,
    service: FromDishka[MediaService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[MediaAssetRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    # Authenticate before multipart parsing, and bound the complete stream before
    # the parser can spool file data to disk. Allow 64 KiB for multipart headers.
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "multipart/form-data"
    ):
        raise InvalidImageError("Expected multipart/form-data")
    parser = MultiPartParser(
        request.headers,
        _limited_multipart_stream(request, service.max_upload_bytes + 64 * 1024),
        max_files=1,
        max_fields=1,
        max_part_size=2000,
    )
    try:
        form = await parser.parse()
    except _MultipartBodyTooLarge:
        raise MediaTooLargeError from None
    except (MultiPartException, ValueError) as exc:
        raise InvalidImageError("Invalid multipart upload") from exc
    try:
        file = form.get("file")
        alt_text = form.get("alt_text")
        if not isinstance(file, UploadFile):
            raise InvalidImageError("A file is required")
        if alt_text is not None and not isinstance(alt_text, str):
            raise InvalidImageError("alt_text must be text")
        content = await _read_limited(file, service.max_upload_bytes)
        asset = await service.upload_image(content, alt_text)
    finally:
        await form.close()
    return ApiResponse(message="Media uploaded", data=MediaAssetRead.model_validate(asset))


class _MultipartBodyTooLarge(MultiPartException):
    pass


async def _limited_multipart_stream(request: Request, limit: int) -> AsyncGenerator[bytes, None]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            # MultiPartException makes Starlette close already spooled files.
            raise _MultipartBodyTooLarge("Upload body is too large")
        yield chunk


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise MediaTooLargeError
            chunks.append(chunk)
    finally:
        await file.close()
    return b"".join(chunks)
