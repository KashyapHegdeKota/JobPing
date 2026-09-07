"""Authenticated preferences, write-only BYOK and public scoped delivery callbacks."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from svix.webhooks import Webhook, WebhookVerificationError

from app.api.deps import get_db
from app.db.models import DiscoveryEvent, EmailAccount, EmailDelivery, EmailWebhookEvent, Subscriber
from app.notifications.core import lock, next_cutoff
from app.notifications.render import job_rows
from app.notifications.security import Identity, decrypt, encrypt, identity
from app.notifications.worker import delivery, match_events

router = APIRouter(tags=["notifications"])
DB = Annotated[AsyncSession, Depends(get_db)]
Who = Annotated[Identity, Depends(identity)]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alerts: bool = False
    recap: bool = False
    job_types: list[Literal["internship", "new_grad"]] = Field(
        default=["internship", "new_grad"], min_length=1, max_length=2
    )
    seasons: list[Literal[2026, 2027]] = Field(default=[2026, 2027], min_length=1, max_length=2)
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Choose a valid IANA timezone") from None
        return value


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr = Field(min_length=8, max_length=512)
    sender: EmailStr
    webhook_secret: SecretStr | None = Field(default=None, max_length=512)


async def cancel_pending(session: AsyncSession, user: Subscriber) -> None:
    await session.execute(
        update(EmailDelivery)
        .where(EmailDelivery.subscriber_id == user.id, EmailDelivery.status == "pending")
        .values(status="cancelled")
    )


async def subscriber(
    session: AsyncSession, who: Identity, initial_timezone: str = "UTC"
) -> Subscriber:
    await lock(session)
    user = await session.get(Subscriber, who.uid)
    if user is None:
        duplicate = await session.scalar(select(Subscriber.id).where(Subscriber.email == who.email))
        if duplicate:
            raise HTTPException(409, "This email already has notification settings")
        user = Subscriber(
            id=who.uid,
            email=who.email,
            verified=who.verified,
            unsubscribe_token=secrets.token_urlsafe(32),
            webhook_id=uuid.uuid4().hex,
            timezone=initial_timezone,
            connection_version=uuid.uuid4().hex,
        )
        session.add(user)
        await session.flush()
    if user.email != who.email:
        duplicate = await session.scalar(select(Subscriber.id).where(Subscriber.email == who.email))
        if duplicate:
            raise HTTPException(409, "This email already has notification settings")
        await cancel_pending(session, user)
        user.email, user.alerts, user.recap = who.email, False, False
        user.opted_at, user.window_start, user.next_recap = None, None, None
        user.suppressed = False
        user.connection_version, user.unsubscribe_token = uuid.uuid4().hex, secrets.token_urlsafe(
            32
        )
    user.verified = who.verified
    if not who.verified:
        await cancel_pending(session, user)
    return user


async def shared_capacity(session: AsyncSession, user: Subscriber) -> None:
    if user.provider != "shared":
        return
    count = (
        await session.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(
                Subscriber.id != user.id,
                Subscriber.provider == "shared",
                (Subscriber.alerts.is_(True) | Subscriber.recap.is_(True)),
                Subscriber.suppressed.is_(False),
            )
        )
        or 0
    )
    if count >= 10:
        raise HTTPException(
            409, "The ten shared email places are full. Connect your own Resend account."
        )


async def view(session: AsyncSession, user: Subscriber) -> dict:
    state = await session.get(EmailAccount, "shared" if user.provider == "shared" else user.id)
    latest = await session.scalar(
        select(EmailDelivery)
        .where(EmailDelivery.subscriber_id == user.id)
        .order_by(EmailDelivery.created_at.desc())
        .limit(1)
    )
    return {
        "alerts": user.alerts,
        "recap": user.recap,
        "job_types": user.job_types,
        "seasons": user.seasons,
        "timezone": user.timezone,
        "email": user.email,
        "verified": user.verified,
        "provider": user.provider,
        "sender": user.sender,
        "status": "suppressed" if user.suppressed else user.status,
        "next_recap": user.next_recap if user.recap else None,
        "key_configured": bool(user.key_ciphertext),
        "webhook_configured": bool(user.webhook_ciphertext),
        "webhook_path": (
            f"/api/v1/notifications/webhooks/{user.webhook_id}" if user.provider == "byok" else None
        ),
        "paused_until": state.blocked_until if state else None,
        "last_delivery_status": latest.status if latest else None,
        "last_error": latest.error_code if latest else None,
        "sending_enabled": os.environ.get("NOTIFICATIONS_SEND_ENABLED", "false").lower() == "true",
    }


@router.get("/me/notification-settings")
async def get_settings(session: DB, who: Who, timezone: str = "UTC") -> dict:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "Choose a valid IANA timezone") from None
    return await view(session, await subscriber(session, who, timezone))


@router.put("/me/notification-settings")
async def put_settings(body: Settings, session: DB, who: Who) -> dict:
    user = await subscriber(session, who)
    if (body.alerts or body.recap) and not who.verified:
        raise HTTPException(403, "Verify your email before enabling notifications")
    if (body.alerts or body.recap) and user.suppressed:
        raise HTTPException(
            409, "Sending is suppressed after a bounce or complaint. Contact support."
        )
    if body.alerts or body.recap:
        await shared_capacity(session, user)
    now = datetime.now(UTC)
    # Resolve already committed discoveries with the old preferences before changing them.
    while (
        await session.scalar(
            select(DiscoveryEvent.job_id).where(DiscoveryEvent.processed.is_(False)).limit(1)
        )
        is not None
    ):
        await match_events(session, now)
    if (body.alerts or body.recap) and not (user.alerts or user.recap):
        user.opted_at = now
    if body.recap and not user.recap:
        user.window_start, user.next_recap = now, next_cutoff(now, body.timezone)
    elif body.recap and body.timezone != user.timezone:
        user.next_recap = next_cutoff(now, body.timezone)
    if not body.alerts:
        await session.execute(
            update(EmailDelivery)
            .where(
                EmailDelivery.subscriber_id == user.id,
                EmailDelivery.kind == "alert",
                EmailDelivery.status == "pending",
            )
            .values(status="cancelled")
        )
    if not body.recap:
        await session.execute(
            update(EmailDelivery)
            .where(
                EmailDelivery.subscriber_id == user.id,
                EmailDelivery.kind == "recap",
                EmailDelivery.status == "pending",
            )
            .values(status="cancelled")
        )
        user.next_recap = None
    for name, value in body.model_dump().items():
        setattr(user, name, value)
    await session.flush()
    return await view(session, user)


@router.put("/me/email-provider")
async def put_provider(body: Provider, session: DB, who: Who) -> dict:
    user = await subscriber(session, who)
    try:
        cipher = encrypt(body.api_key.get_secret_value())
        webhook = encrypt(body.webhook_secret.get_secret_value()) if body.webhook_secret else None
    except (KeyError, ValueError):
        raise HTTPException(503, "Credential encryption is not configured") from None
    await cancel_pending(session, user)
    user.provider, user.sender, user.key_ciphertext = "byok", str(body.sender), cipher
    user.webhook_ciphertext, user.status = webhook, "needs_test"
    user.connection_version = uuid.uuid4().hex
    await session.flush()
    return await view(session, user)


@router.delete("/me/email-provider")
async def delete_provider(session: DB, who: Who) -> dict:
    user = await subscriber(session, who)
    await cancel_pending(session, user)
    user.provider, user.status = "shared", "ready"
    user.key_ciphertext, user.webhook_ciphertext, user.sender = None, None, None
    user.connection_version, user.webhook_id = uuid.uuid4().hex, uuid.uuid4().hex
    # Removal is always possible; shared enrollment requires an explicit settings save.
    user.alerts, user.recap, user.next_recap = False, False, None
    return await view(session, user)


@router.post("/me/email-provider/test", status_code=202)
async def test_provider(session: DB, who: Who) -> dict:
    user = await subscriber(session, who)
    if not user.verified or user.suppressed:
        raise HTTPException(403, "A verified, unsuppressed recipient is required")
    if user.provider != "byok" or not user.key_ciphertext:
        raise HTTPException(409, "Connect your Resend account first")
    now = datetime.now(UTC)
    recent = await session.scalar(
        select(EmailDelivery.id)
        .where(
            EmailDelivery.subscriber_id == user.id,
            EmailDelivery.kind == "test",
            EmailDelivery.created_at > now - timedelta(minutes=5),
        )
        .limit(1)
    )
    if recent:
        raise HTTPException(429, "Wait five minutes before sending another test")
    item = delivery(user, "test", f"test:{uuid.uuid4().hex}", [], now)
    session.add(item)
    return {"status": "queued", "id": item.id}


@router.get("/me/recaps/{recap_id}")
async def get_recap(recap_id: str, session: DB, who: Who) -> dict:
    user = await subscriber(session, who)
    item = await session.get(EmailDelivery, recap_id)
    if item is None or item.subscriber_id != user.id or item.kind != "recap":
        raise HTTPException(404, "Recap not found")
    return {
        "id": item.id,
        "window_start": item.window_start,
        "window_end": item.window_end,
        "jobs": await job_rows(session, item.job_ids),
    }


@router.get("/notifications/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str, session: DB) -> str:
    user = await session.scalar(select(Subscriber).where(Subscriber.unsubscribe_token == token))
    if user is None:
        raise HTTPException(404, "Link not found")
    return (
        '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width">'
        '<title>Unsubscribe · JobPing</title><body style="font-family:Arial;padding:40px;'
        'max-width:520px;margin:auto"><h1>Pause JobPing emails?</h1>'
        "<p>This turns off job alerts and daily recaps. Enable them again in your Profile.</p>"
        '<form method="post"><button style="padding:12px 20px">Unsubscribe</button></form>'
        "</body></html>"
    )


@router.post("/notifications/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(token: str, session: DB) -> str:
    await lock(session)
    user = await session.scalar(select(Subscriber).where(Subscriber.unsubscribe_token == token))
    if user is None:
        raise HTTPException(404, "Link not found")
    user.alerts, user.recap, user.next_recap = False, False, None
    await cancel_pending(session, user)
    return (
        '<!doctype html><html lang="en"><title>Unsubscribed · JobPing</title>'
        '<body style="font-family:Arial;padding:40px"><h1>You’re unsubscribed.</h1>'
        "<p>Your JobPing email notifications are now off.</p></body></html>"
    )


@router.post("/notifications/webhooks/{connection}")
async def webhook(connection: str, request: Request, session: DB) -> dict:
    await lock(session)
    user = None
    if connection == "shared":
        secret = os.environ.get("RESEND_WEBHOOK_SECRET")
        account = "shared"
    else:
        user = await session.scalar(select(Subscriber).where(Subscriber.webhook_id == connection))
        if not user or not user.webhook_ciphertext or user.provider != "byok":
            raise HTTPException(404, "Webhook not configured")
        secret, account = decrypt(user.webhook_ciphertext), user.id
    if not secret:
        raise HTTPException(503, "Webhook not configured")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 262144:
            raise HTTPException(413, "Webhook too large")
    try:
        event = Webhook(secret).verify(bytes(raw), dict(request.headers))
    except (WebhookVerificationError, ValueError):
        raise HTTPException(400, "Invalid webhook signature") from None
    event_id = f"{connection}:{request.headers['svix-id']}"
    if await session.get(EmailWebhookEvent, event_id):
        return {"status": "duplicate"}
    data = event.get("data", {})
    item = await session.scalar(
        select(EmailDelivery).where(
            EmailDelivery.account == account, EmailDelivery.provider_id == data.get("email_id")
        )
    )
    # A webhook can race the HTTP response. Ask Resend to retry instead of losing it.
    if item is None:
        pending = await session.scalar(
            select(EmailDelivery.id)
            .where(EmailDelivery.account == account, EmailDelivery.status == "sending")
            .limit(1)
        )
        if pending:
            raise HTTPException(503, "Send outcome is still being recorded")
        return {"status": "unrelated"}
    session.add(EmailWebhookEvent(id=event_id))
    kind = event.get("type")
    if kind in {"email.bounced", "email.complained"}:
        recipient = await session.get(Subscriber, item.subscriber_id)
        # Do not suppress a newly changed address based on an old delivery.
        if item.payload and item.payload.get("to") == [recipient.email]:
            recipient.suppressed = True
            await cancel_pending(session, recipient)
        item.status = "bounced" if kind == "email.bounced" else "complained"
    elif kind == "email.delivered" and item.status not in {"bounced", "complained"}:
        item.status = "delivered"
    return {"status": "ok"}
