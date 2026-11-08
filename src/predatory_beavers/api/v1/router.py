from fastapi import APIRouter

from predatory_beavers.api.health import e2e_router
from predatory_beavers.modules.achievements.router import admin_router as achievements_admin_router
from predatory_beavers.modules.achievements.router import (
    public_router as achievements_public_router,
)
from predatory_beavers.modules.audit.router import router as audit_router
from predatory_beavers.modules.auth.router import router as auth_router
from predatory_beavers.modules.calendar.router import router as calendar_router
from predatory_beavers.modules.club.router import admin_router, public_router, teams_admin_router
from predatory_beavers.modules.content.router import admin_router as content_admin_router
from predatory_beavers.modules.content.router import public_router as content_public_router
from predatory_beavers.modules.home.router import router as home_router
from predatory_beavers.modules.imports.router import router as imports_router
from predatory_beavers.modules.matches.router import admin_router as matches_admin_router
from predatory_beavers.modules.matches.router import public_router as matches_public_router
from predatory_beavers.modules.media.router import admin_router as media_admin_router
from predatory_beavers.modules.media.router import public_router as media_public_router
from predatory_beavers.modules.results.router import router as results_archive_router
from predatory_beavers.modules.results.tournaments_router import router as tournaments_router
from predatory_beavers.modules.roster.router import router as asb_roster_router
from predatory_beavers.modules.standings.router import admin_router as standings_admin_router
from predatory_beavers.modules.standings.router import public_router as standings_public_router
from predatory_beavers.modules.trash.router import router as trash_router

api_v1_router = APIRouter()
api_v1_router.include_router(e2e_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(public_router)
api_v1_router.include_router(admin_router)
api_v1_router.include_router(teams_admin_router)
api_v1_router.include_router(content_public_router)
api_v1_router.include_router(content_admin_router)
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
api_v1_router.include_router(calendar_router)
api_v1_router.include_router(trash_router)
api_v1_router.include_router(results_archive_router)
api_v1_router.include_router(tournaments_router)
api_v1_router.include_router(asb_roster_router)
