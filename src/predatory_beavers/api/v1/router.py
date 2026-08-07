from fastapi import APIRouter

from predatory_beavers.modules.auth.router import router as auth_router
from predatory_beavers.modules.club.router import admin_router, public_router

api_v1_router = APIRouter()
api_v1_router.include_router(auth_router)
api_v1_router.include_router(public_router)
api_v1_router.include_router(admin_router)
