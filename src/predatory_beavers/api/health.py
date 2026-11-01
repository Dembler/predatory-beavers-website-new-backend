import asyncio
from pathlib import Path
from secrets import compare_digest

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from predatory_beavers.api.errors import NotFoundError, ServiceUnavailableError
from predatory_beavers.settings import Settings

router = APIRouter(route_class=DishkaRoute, tags=["health"])
e2e_router = APIRouter(route_class=DishkaRoute, include_in_schema=False)


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/e2e", include_in_schema=False)
@e2e_router.get("/health/e2e")
async def e2e_runtime(
    request: Request,
    response: Response,
    settings: FromDishka[Settings],
) -> dict[str, str | bool]:
    token = settings.e2e_runtime_token
    supplied = request.headers.get("X-E2E-Runtime-Token", "")
    if settings.env != "test" or not token or not compare_digest(supplied, token):
        raise NotFoundError
    database = await asyncio.to_thread(Path(make_url(settings.database_url).database or "").resolve)
    if database.name != "e2e.db":
        raise NotFoundError
    media = await asyncio.to_thread(settings.media_storage_path.resolve)
    response.headers["Cache-Control"] = "no-store"
    return {
        "isolated": True,
        "databasePath": str(database),
        "mediaPath": str(media),
    }


@router.get("/health/ready")
async def ready(engine: FromDishka[AsyncEngine]) -> dict[str, str]:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise ServiceUnavailableError("Database is not ready") from exc
    return {"status": "ok"}
