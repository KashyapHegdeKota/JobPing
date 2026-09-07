"""Version 1 API router composition."""

from fastapi import APIRouter

from app.api.v1 import companies, jobs, sse, stats, ws
from app.notifications.api import router as notifications_router

router = APIRouter(prefix="/api/v1")
router.include_router(jobs.router)
router.include_router(companies.router)
router.include_router(stats.router)
router.include_router(ws.router)
router.include_router(sse.router)
router.include_router(notifications_router)

__all__ = ["router"]
