from typing import Annotated

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Header, Request, Response

from predatory_beavers.api.responses import ApiResponse
from predatory_beavers.modules.auth.errors import InvalidCsrfTokenError, InvalidSessionError
from predatory_beavers.modules.auth.schemas import (
    LoginData,
    LoginRequest,
    LogoutData,
    UserRead,
)
from predatory_beavers.modules.auth.service import AuthService
from predatory_beavers.settings import Settings

router = APIRouter(prefix="/auth", tags=["auth"], route_class=DishkaRoute)


def _session_token(request: Request, settings: Settings) -> str:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise InvalidSessionError
    return token


@router.post("/login", response_model=ApiResponse[LoginData])
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: FromDishka[AuthService],
    settings: FromDishka[Settings],
) -> ApiResponse[LoginData]:
    client_key = request.client.host if request.client is not None else None
    result = await service.login(payload.username, payload.password, client_key=client_key)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=result.session_token,
        max_age=settings.session_ttl_seconds,
        expires=result.expires_at,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return ApiResponse(
        message="Login successful",
        data=LoginData(
            user=UserRead.model_validate(result.user),
            csrf_token=result.csrf_token,
            expires_at=result.expires_at,
        ),
    )


@router.post("/logout", response_model=ApiResponse[LogoutData])
async def logout(
    request: Request,
    response: Response,
    service: FromDishka[AuthService],
    settings: FromDishka[Settings],
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> ApiResponse[LogoutData]:
    session_token = _session_token(request, settings)
    csrf_token = request.headers.get(settings.csrf_header_name)
    if not csrf_token:
        # Keeps the configured header authoritative while documenting the default in OpenAPI.
        csrf_token = csrf_header
    if not csrf_token:
        raise InvalidCsrfTokenError

    await service.logout(session_token, csrf_token)
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return ApiResponse(message="Logout successful", data=LogoutData())


@router.get("/me", response_model=ApiResponse[UserRead])
async def me(
    request: Request,
    service: FromDishka[AuthService],
    settings: FromDishka[Settings],
) -> ApiResponse[UserRead]:
    user = await service.me(_session_token(request, settings))
    return ApiResponse(
        message="Current user retrieved successfully",
        data=UserRead.model_validate(user),
    )
