import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.cors import CORSMiddleware

from predatory_beavers.api.dependency_injection import build_container
from predatory_beavers.api.errors import register_exception_handlers
from predatory_beavers.api.health import router as health_router
from predatory_beavers.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from predatory_beavers.api.v1.router import api_v1_router
from predatory_beavers.modules.auth.provider import AuthProvider
from predatory_beavers.modules.club.provider import ClubProvider
from predatory_beavers.observability import configure_logging
from predatory_beavers.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(level=app_settings.log_level, json_logs=app_settings.log_json)
    container = build_container(app_settings, AuthProvider(), ClubProvider())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Application startup completed", extra={"environment": app_settings.env})
        try:
            yield
        finally:
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
        allow_headers=["Content-Type", app_settings.csrf_header_name, "X-Request-ID"],
    )

    app.include_router(health_router)
    app.include_router(api_v1_router, prefix=app_settings.api_prefix)
    register_exception_handlers(app)
    return app


app = create_app()
