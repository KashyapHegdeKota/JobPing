"""Cheap internal readiness without consuming browser quotas or issuing SQL."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/health", include_in_schema=False)
async def health(request: Request) -> dict[str, str]:
    state = request.app.state
    consumer = getattr(state, "redis_event_consumer", None)
    if not getattr(state, "db_sessionmaker", None) or not consumer or not consumer.is_running:
        raise HTTPException(503, "Application is not ready")
    return {"status": "ready"}
