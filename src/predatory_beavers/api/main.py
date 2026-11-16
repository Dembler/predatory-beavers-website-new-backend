import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from predatory_beavers.api.dependency_injection import build_container
from predatory_beavers.api.errors import register_exception_handlers
from predatory_beavers.api.health import router as health_router
from predatory_beavers.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from predatory_beavers.api.v1.router import api_v1_router
from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.modules.achievements.provider import AchievementsProvider
from predatory_beavers.modules.audit.provider import AuditProvider
from predatory_beavers.modules.auth.provider import AuthProvider
from predatory_beavers.modules.club.provider import ClubProvider
from predatory_beavers.modules.content.provider import ContentProvider
from predatory_beavers.modules.editorial.provider import EditorialProvider
from predatory_beavers.modules.editorial.router import router as editorial_router
from predatory_beavers.modules.editorial.web import router as news_router
from predatory_beavers.modules.home.provider import HomeProvider
from predatory_beavers.modules.imports.provider import ImportsProvider
from predatory_beavers.modules.mascot.router import router as mascot_router
from predatory_beavers.modules.matches.provider import MatchesProvider
from predatory_beavers.modules.media.provider import MediaProvider
from predatory_beavers.modules.media.router import content_router as media_content_router
from predatory_beavers.modules.standings.provider import StandingsProvider
from predatory_beavers.observability import configure_logging
from predatory_beavers.settings import Settings, get_settings
from predatory_beavers.worker.cloud_state import refresh_portrait_cache
from predatory_beavers.worker.sports_sync import SportsSyncConfig, run_sports_sync

logger = logging.getLogger(__name__)


class EditorialNoStoreMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        prefix = request.app.state.settings.api_prefix
        if request.url.path.startswith(
            ("/news", prefix + "/articles", prefix + "/admin/articles", prefix + "/admin/editorial")
        ):
            response.headers["Cache-Control"] = "private, no-store"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(level=app_settings.log_level, json_logs=app_settings.log_json)
    container = build_container(
        app_settings,
        AuthProvider(),
        AuditProvider(),
        ClubProvider(),
        ContentProvider(),
        EditorialProvider(),
        MatchesProvider(),
        MediaProvider(),
        AchievementsProvider(),
        StandingsProvider(),
        ImportsProvider(),
        HomeProvider(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sync_task: asyncio.Task[None] | None = None
        cache_task: asyncio.Task[None] | None = None
        local_database = make_url(app_settings.database_url).get_backend_name() == "sqlite"
        if app_settings.sports_sync_enabled and local_database and app_settings.env != "test":
            database = Path(app_settings.database_url.removeprefix("sqlite+aiosqlite:///"))
            config = SportsSyncConfig(
                backend_root=app_settings.sports_sync_backend_root,
                database=database,
                interval_seconds=app_settings.sports_sync_interval_seconds,
                timeout_seconds=app_settings.sports_sync_timeout_seconds,
            )
            sync_task = asyncio.create_task(run_sports_sync(config), name="sports-sync")
        if (
            app_settings.sports_sync_external_enabled
            and not local_database
            and app_settings.env != "test"
        ):
            engine = await container.get(AsyncEngine)
            storage = await container.get(ObjectStorage)
            cache_task = asyncio.create_task(
                refresh_portrait_cache(
                    engine, storage, app_settings.sports_sync_cache_path / "roster-photos"
                ),
                name="cloud-portrait-cache",
            )
        logger.info("Application startup completed", extra={"environment": app_settings.env})
        try:
            yield
        finally:
            for task in (sync_task, cache_task):
                if task is None:
                    continue
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            engine = await container.get(AsyncEngine)
            await engine.dispose()
            await container.close()
            logger.info("Application shutdown completed")

    app = FastAPI(
        title=app_settings.name,
        version=app_settings.version,
        lifespan=lifespan,
        docs_url="/docs" if app_settings.env != "prod" else None,
        redoc_url="/redoc" if app_settings.env != "prod" else None,
        openapi_url="/openapi.json" if app_settings.env != "prod" else None,
    )
    app.state.settings = app_settings
    app.state.dishka_container = container
    setup_dishka(container, app)

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            app_settings.csrf_header_name,
            "X-Request-ID",
            "Idempotency-Key",
        ],
    )

    app.include_router(health_router)
    app.include_router(media_content_router)
    app.include_router(api_v1_router, prefix=app_settings.api_prefix)
    if app_settings.mascot_enabled and app_settings.env in ("dev", "test"):
        app.add_middleware(EditorialNoStoreMiddleware)
        app.include_router(mascot_router, prefix=app_settings.api_prefix)
        app.include_router(editorial_router, prefix=app_settings.api_prefix)
        app.include_router(news_router)
    register_exception_handlers(app)
    return app


app = create_app()
