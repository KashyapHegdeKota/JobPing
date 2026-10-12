"""Verified UID quota is enforced once, before opening a private SQL session."""

from typing import Annotated

import httpx
from app.api.deps import get_db
from app.notifications.security import Identity, identity
from app.request_limits.dependencies import limited_identity, private_db
from fastapi import Depends, FastAPI

from tests.unit.request_limit_fakes import limiter, redis_mock


async def test_uid_rejection_precedes_database_and_verifies_once() -> None:
    app = FastAPI()
    app.state.request_limiter = limiter()
    order: list[str] = []

    async def who() -> Identity:
        order.append("identity")
        return Identity("verified", "test@example.com", True)

    async def db() -> str:
        order.append("database")
        return "db"

    @app.get("/api/v1/me/test")
    async def endpoint(
        session: Annotated[str, Depends(private_db)],
        user: Annotated[Identity, Depends(limited_identity)],
    ) -> dict:
        return {"uid": user.uid}

    app.dependency_overrides[identity] = who
    app.dependency_overrides[get_db] = db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://test"
    ) as client:
        redis_mock(app.state.request_limiter).eval.return_value = [0, 5]
        assert (await client.get("/api/v1/me/test")).status_code == 429
        assert order == ["identity"]
        order.clear()
        redis_mock(app.state.request_limiter).eval.return_value = [1, 0]
        assert (await client.get("/api/v1/me/test")).json() == {"uid": "verified"}
        assert order == ["identity", "database"]
        assert redis_mock(app.state.request_limiter).eval.call_count == 2
