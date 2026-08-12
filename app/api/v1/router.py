"""Version 1 API router composition."""

from fastapi import APIRouter

from app.api.v1 import companies, jobs, stats

router = APIRouter(prefix="/api/v1")
router.include_router(jobs.router)
router.include_router(companies.router)
router.include_router(stats.router)

__all__ = ["router"]
