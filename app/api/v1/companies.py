"""Company discovery endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.models import Company
from app.schemas.api import CompanyResponse

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyResponse])
async def list_companies(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> list[Company]:
    """List all known companies in stable, case-insensitive name order."""
    result = await session.scalars(
        select(Company).order_by(func.lower(Company.name), Company.name, Company.id)
    )
    return list(result.all())


__all__ = ["router"]
