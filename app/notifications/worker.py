"""Durable matching, recap scheduling, quotas and leased Resend delivery."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    DiscoveryEvent,
    EmailAccount,
    EmailDelivery,
    JobMatch,
    JobPosting,
    Subscriber,
)
from app.notifications.core import lock, next_cutoff, utc
from app.notifications.render import payload
from app.notifications.security import current_recipient, decrypt
from app.scheduler import PollTarget, SchedulerDaemon

logger = logging.getLogger(__name__)


def delivery(user: Subscriber, kind: str, key: str, ids: list[int], now: datetime) -> EmailDelivery:
    return EmailDelivery(
        id=uuid.uuid4().hex,
        subscriber_id=user.id,
        kind=kind,
        dedupe_key=key,
        job_ids=ids,
        created_at=now,
        due_at=now,
        status="pending",
    )


async def match_events(session: AsyncSession, now: datetime) -> None:
    users = list(
        await session.scalars(
            select(Subscriber).where(
                Subscriber.verified.is_(True), Subscriber.suppressed.is_(False)
            )
        )
    )
    events = await session.execute(
        select(DiscoveryEvent, JobPosting)
        .join(JobPosting)
        .where(DiscoveryEvent.processed.is_(False))
        .order_by(DiscoveryEvent.discovered_at)
        .limit(500)
    )
    for event, job in events:
        for user in users:
            if (
                not (user.alerts or user.recap)
                or user.opted_at is None
                or utc(event.discovered_at) < utc(user.opted_at)
                or job.job_type.value not in user.job_types
                or job.season not in user.seasons
            ):
                continue
            exists = await session.scalar(
                select(JobMatch.id).where(
                    JobMatch.subscriber_id == user.id, JobMatch.job_id == job.id
                )
            )
            if exists is not None:
                continue
            session.add(
                JobMatch(subscriber_id=user.id, job_id=job.id, discovered_at=event.discovered_at)
            )
            if user.alerts and not job.is_closed:
                session.add(delivery(user, "alert", f"alert:{user.id}:{job.id}", [job.id], now))
        event.processed = True
    await session.flush()


async def make_recaps(session: AsyncSession, now: datetime) -> None:
    # Drain discovery backlog first; never advance a window past unprocessed events.
    if (
        await session.scalar(
            select(DiscoveryEvent.job_id).where(DiscoveryEvent.processed.is_(False)).limit(1)
        )
        is not None
    ):
        return
    users = await session.scalars(
        select(Subscriber).where(
            Subscriber.recap.is_(True),
            Subscriber.verified.is_(True),
            Subscriber.suppressed.is_(False),
            Subscriber.next_recap <= now,
        )
    )
    for user in users:
        start = utc(user.window_start)
        end = utc(user.next_recap)
        while next_cutoff(end, user.timezone) <= now:
            end = next_cutoff(end, user.timezone)
        ids = list(
            await session.scalars(
                select(JobMatch.job_id)
                .where(
                    JobMatch.subscriber_id == user.id,
                    JobMatch.discovered_at >= start,
                    JobMatch.discovered_at < end,
                )
                .order_by(JobMatch.discovered_at, JobMatch.id)
            )
        )
        if ids:
            item = await session.scalar(
                select(EmailDelivery)
                .where(
                    EmailDelivery.subscriber_id == user.id,
                    EmailDelivery.kind == "recap",
                    EmailDelivery.status == "pending",
                    EmailDelivery.first_attempt.is_(None),
                )
                .order_by(EmailDelivery.created_at)
                .limit(1)
            )
            if item is None:
                item = delivery(user, "recap", f"recap:{user.id}:{end.isoformat()}", ids, now)
                item.window_start = start
                session.add(item)
            else:
                # Quota-paused, never-attempted recaps become a single catch-up email.
                item.job_ids = list(dict.fromkeys([*item.job_ids, *ids]))
                item.payload = None
                item.due_at = now
            item.window_end = end
        user.window_start, user.next_recap = end, next_cutoff(end, user.timezone)
        if ids:
            # Only unsent alerts are folded into the recap. Uncertain requests retain their key.
            stale = await session.scalars(
                select(EmailDelivery).where(
                    EmailDelivery.subscriber_id == user.id,
                    EmailDelivery.kind == "alert",
                    EmailDelivery.status == "pending",
                    EmailDelivery.first_attempt.is_(None),
                )
            )
            for alert in stale:
                if alert.job_ids[0] in ids:
                    alert.status = "recap_only"


async def budget(session: AsyncSession, account: str, kind: str, now: datetime) -> bool:
    state = await session.get(EmailAccount, account)
    if state and state.blocked_until and utc(state.blocked_until) > now:
        return False
    scope = Subscriber.provider == "shared" if account == "shared" else Subscriber.id == account
    recaps = (
        await session.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(
                scope,
                Subscriber.recap.is_(True),
                Subscriber.verified.is_(True),
                Subscriber.suppressed.is_(False),
            )
        )
        or 0
    )
    counts = []
    for days in (1, 31):
        counts.append(
            await session.scalar(
                select(func.count())
                .select_from(EmailDelivery)
                .where(
                    EmailDelivery.account == account,
                    EmailDelivery.first_attempt >= now - timedelta(days=days),
                )
            )
            or 0
        )
    # A rolling 31-day window is deliberately stricter than a calendar-month quota.
    reserve_daily = recaps if kind != "recap" else 0
    reserve_month = recaps * 31 if kind != "recap" else 0
    return counts[0] < 90 - reserve_daily and counts[1] < 2900 - reserve_month


class NotificationWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        *,
        verify_recipient: Callable[[str, str], Awaitable[bool]] = current_recipient,
    ) -> None:
        self.sessions, self.client = sessions, client
        self.verify_recipient = verify_recipient

    async def prepare(self, now: datetime) -> None:
        async with self.sessions() as session, session.begin():
            await lock(session)
            await match_events(session, now)
            await make_recaps(session, now)

    async def claim(self, now: datetime) -> tuple[str, str, dict] | None:
        async with self.sessions() as session, session.begin():
            await lock(session)
            candidates = list(
                await session.scalars(
                    select(EmailDelivery)
                    .where(
                        EmailDelivery.status.in_(["pending", "sending"]),
                        EmailDelivery.due_at <= now,
                        (EmailDelivery.lease_until.is_(None) | (EmailDelivery.lease_until <= now)),
                    )
                    .order_by((EmailDelivery.kind != "recap"), EmailDelivery.created_at)
                    .limit(500)
                )
            )
            # Fairly rotate recipients by their most recent charged delivery.
            last = dict(
                (
                    await session.execute(
                        select(
                            EmailDelivery.subscriber_id, func.max(EmailDelivery.first_attempt)
                        ).group_by(EmailDelivery.subscriber_id)
                    )
                ).all()
            )
            candidates.sort(
                key=lambda d: (
                    d.kind != "recap",
                    (
                        utc(last[d.subscriber_id])
                        if last.get(d.subscriber_id)
                        else datetime.min.replace(tzinfo=UTC)
                    ),
                    utc(d.created_at),
                    d.id,
                )
            )
            for item in candidates:
                user = await session.get(Subscriber, item.subscriber_id)
                if (
                    user.suppressed
                    or not user.verified
                    or (item.kind == "alert" and not user.alerts)
                    or (item.kind == "recap" and not user.recap)
                ):
                    item.status = "cancelled"
                    continue
                if item.connection_version and item.connection_version != user.connection_version:
                    item.status = "reconcile" if item.first_attempt else "cancelled"
                    continue
                if user.status in {"sender_error", "needs_test"} and item.kind != "test":
                    continue
                if item.first_attempt and now - utc(item.first_attempt) >= timedelta(hours=23):
                    item.status, item.error_code = "reconcile", "idempotency_window_expired"
                    continue
                if item.kind == "alert" and not item.first_attempt:
                    job = await session.get(JobPosting, item.job_ids[0])
                    if job is None or job.is_closed:
                        item.status = "cancelled"
                        continue
                    if now - utc(item.created_at) >= timedelta(hours=24):
                        item.status = "expired"
                        continue
                account = "shared" if user.provider == "shared" else user.id
                if not item.first_attempt and not await budget(session, account, item.kind, now):
                    item.error_code = "quota_paused"
                    item.due_at = now + timedelta(minutes=5)
                    continue
                try:
                    valid = await self.verify_recipient(user.id, user.email)
                except Exception:
                    item.error_code = "recipient_verification_unavailable"
                    item.due_at = now + timedelta(minutes=5)
                    continue
                if not valid:
                    user.verified = False
                    item.status = "cancelled"
                    continue
                try:
                    key = (
                        decrypt(user.key_ciphertext)
                        if user.provider == "byok"
                        else os.environ["RESEND_API_KEY"]
                    )
                    if not item.payload:
                        item.payload = await payload(session, item, user)
                except (KeyError, ValueError):
                    user.status = "configuration_error"
                    continue
                item.account, item.connection_version = account, user.connection_version
                item.first_attempt = item.first_attempt or now
                item.attempts += 1
                item.status, item.lease_until = "sending", now + timedelta(minutes=2)
                return item.id, key, item.payload
        return None

    async def send_one(self, now: datetime) -> bool:
        claim = await self.claim(now)
        if not claim:
            return False
        delivery_id, key, body = claim
        code, provider_id, retry = "network_error", None, 30
        response = None
        try:
            response = await self.client.post(
                "https://api.resend.com/emails",
                json=body,
                headers={"Authorization": f"Bearer {key}", "Idempotency-Key": delivery_id},
            )
            data = response.json()
            if response.is_success and isinstance(data, dict) and data.get("id"):
                code, provider_id = "accepted", str(data["id"])
            elif isinstance(data, dict):
                raw_code = str(data.get("name", "provider_error"))
                known = {
                    "daily_quota_exceeded",
                    "monthly_quota_exceeded",
                    "rate_limit_exceeded",
                    "invalid_api_key",
                    "restricted_api_key",
                    "validation_error",
                    "invalid_from_address",
                    "invalid_access",
                    "invalid_idempotent_request",
                }
                code = raw_code if raw_code in known else "provider_error"
            try:
                retry = min(86400, max(1, int(response.headers.get("retry-after", "30"))))
            except ValueError:
                retry = 30
        except (httpx.HTTPError, ValueError):
            pass
        async with self.sessions() as session, session.begin():
            await lock(session)
            item = await session.get(EmailDelivery, delivery_id)
            user = await session.get(Subscriber, item.subscriber_id)
            item.lease_until, item.error_code = None, None if code == "accepted" else code
            if code == "accepted":
                item.provider_id = provider_id
                if item.status not in {"delivered", "bounced", "complained"}:
                    item.status = "accepted"
                if item.kind == "test" and item.connection_version == user.connection_version:
                    user.status = "ready"
            elif code in {"daily_quota_exceeded", "monthly_quota_exceeded"}:
                state = await session.get(EmailAccount, item.account)
                if state is None:
                    state = EmailAccount(id=item.account)
                    session.add(state)
                delay = timedelta(days=1 if code == "daily_quota_exceeded" else 31)
                state.blocked_until, state.error_code = now + delay, code
                item.status, item.due_at = "pending", now + delay
            elif (
                response is not None
                and 400 <= response.status_code < 500
                and response.status_code not in (429, 409)
            ):
                item.status = "failed"
                if item.connection_version == user.connection_version:
                    user.status = "sender_error"
            else:
                item.status = "pending"
                item.due_at = now + timedelta(
                    seconds=max(retry, min(3600, 15 * 2 ** min(item.attempts, 8)))
                )
            # Provider quotas cover other apps sharing the same account as well.
            if response is not None:
                for header, threshold, days in (
                    ("x-resend-daily-quota", 90, 1),
                    ("x-resend-monthly-quota", 2900, 31),
                ):
                    try:
                        used = int(response.headers.get(header, "0"))
                    except ValueError:
                        continue
                    if used >= threshold:
                        state = await session.get(EmailAccount, item.account)
                        if state is None:
                            state = EmailAccount(id=item.account)
                            session.add(state)
                        until = now + timedelta(days=days)
                        if state.blocked_until is None or utc(state.blocked_until) < until:
                            state.blocked_until, state.error_code = until, "quota_paused"
            logger.info("notification.send delivery_id=%s result=%s", delivery_id, code)
        return True

    async def tick(self) -> None:
        now = datetime.now(UTC)
        await self.prepare(now)
        if os.environ.get("NOTIFICATIONS_SEND_ENABLED", "false").lower() == "true":
            for _ in range(20):
                if not await self.send_one(datetime.now(UTC)):
                    break
                await asyncio.sleep(1)  # Conservative across provider rate-limit variants.
        async with self.sessions() as session:
            count, oldest = (
                await session.execute(
                    select(func.count(), func.min(EmailDelivery.created_at)).where(
                        EmailDelivery.status == "pending"
                    )
                )
            ).one()
            logger.info(
                "notification.heartbeat pending=%s oldest_age_seconds=%s",
                count,
                (now - utc(oldest)).total_seconds() if oldest else 0,
            )


async def serve(*, once: bool = False) -> None:
    from app.main import _as_async_database_url

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = create_async_engine(_as_async_database_url(os.environ["DATABASE_URL"]))
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            worker = NotificationWorker(async_sessionmaker(engine, expire_on_commit=False), client)
            if once:
                await worker.tick()
            else:
                scheduler = SchedulerDaemon()
                scheduler.register(PollTarget("notifications", 15, worker.tick))
                await scheduler.serve()
    finally:
        await engine.dispose()
