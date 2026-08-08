from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status

from predatory_beavers.api.responses import ApiResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.club.schemas import MediaAssetRead
from predatory_beavers.modules.media.errors import MediaTooLargeError
from predatory_beavers.modules.media.service import MediaService
from predatory_beavers.settings import Settings

content_router = APIRouter(route_class=DishkaRoute, tags=["media"])
public_router = APIRouter(route_class=DishkaRoute, tags=["media"])
admin_router = APIRouter(prefix="/admin/media", route_class=DishkaRoute, tags=["admin-media"])


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
)
async def upload_media(
    file: Annotated[UploadFile, File()],
    request: Request,
    service: FromDishka[MediaService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    alt_text: Annotated[str | None, Form(max_length=500)] = None,
) -> ApiResponse[MediaAssetRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    content = await _read_limited(file, service.max_upload_bytes)
    asset = await service.upload_image(content, alt_text)
    return ApiResponse(message="Media uploaded", data=MediaAssetRead.model_validate(asset))


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
