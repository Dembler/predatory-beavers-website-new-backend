from fastapi import APIRouter

from predatory_beavers.modules.achievements.router import admin_router as achievements_admin_router
from predatory_beavers.modules.achievements.router import (
    public_router as achievements_public_router,
)
from predatory_beavers.modules.audit.router import router as audit_router
from predatory_beavers.modules.auth.router import router as auth_router
from predatory_beavers.modules.club.router import admin_router, public_router
from predatory_beavers.modules.home.router import router as home_router
from predatory_beavers.modules.imports.router import router as imports_router
from predatory_beavers.modules.matches.router import admin_router as matches_admin_router
from predatory_beavers.modules.matches.router import public_router as matches_public_router
from predatory_beavers.modules.media.router import admin_router as media_admin_router
from predatory_beavers.modules.media.router import public_router as media_public_router
from predatory_beavers.modules.standings.router import admin_router as standings_admin_router
from predatory_beavers.modules.standings.router import public_router as standings_public_router

api_v1_router = APIRouter()
api_v1_router.include_router(auth_router)
api_v1_router.include_router(public_router)
api_v1_router.include_router(admin_router)
api_v1_router.include_router(matches_public_router)
api_v1_router.include_router(matches_admin_router)
api_v1_router.include_router(media_public_router)
api_v1_router.include_router(media_admin_router)
api_v1_router.include_router(achievements_public_router)
api_v1_router.include_router(achievements_admin_router)
api_v1_router.include_router(standings_public_router)
api_v1_router.include_router(standings_admin_router)
api_v1_router.include_router(audit_router)
api_v1_router.include_router(imports_router)
api_v1_router.include_router(home_router)
