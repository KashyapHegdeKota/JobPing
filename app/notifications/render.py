"""Escaped, size-bounded HTML and matching text email bodies."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, EmailDelivery, JobPosting, Subscriber
from app.notifications.core import utc

ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    return value if parsed.scheme in {"https", "http"} and parsed.netloc else ""


async def job_rows(session: AsyncSession, ids: list[int]) -> list[dict]:
    rows = await session.execute(
        select(JobPosting, Company.name)
        .join(Company)
        .where(JobPosting.id.in_(ids))
        .order_by(Company.name, JobPosting.title, JobPosting.id)
    )
    result = []
    for job, company in rows:
        if job.posted_at:
            date_text = f"Posted {job.posted_at.strftime('%b %d, %Y').replace(' 0', ' ')}"
        else:
            date_text = f"Discovered {job.created_at.strftime('%b %d, %Y').replace(' 0', ' ')}"
            
        result.append(
            {
                "id": job.id,
                "company": company,
                "title": job.title,
                "location": job.location,
                "job_type": job.job_type.value.replace("_", " "),
                "season": job.season,
                "closed": job.is_closed,
                "apply_url": safe_url(job.apply_url),
                "date_text": date_text,
            }
        )
    return result


async def payload(session: AsyncSession, delivery: EmailDelivery, user: Subscriber) -> dict:
    jobs = await job_rows(session, delivery.job_ids)
    recap = delivery.kind == "recap"
    app_url = os.environ["NOTIFICATION_APP_URL"].rstrip("/")
    api_url = os.environ["NOTIFICATION_API_URL"].rstrip("/")
    unsubscribe = f"{api_url}/api/v1/notifications/unsubscribe/{user.unsubscribe_token}"
    recap_url = f"{app_url}/profile/recaps/{delivery.id}"
    if delivery.kind == "test":
        subject, heading = "Your JobPing email connection works", "You're connected."
        description = "JobPing can now send your job alerts and daily recaps using this sender."
    elif recap:
        subject = f"Your JobPing recap: {len(jobs)} new matches"
        heading = f"{len(jobs)} new opportunities."
        zone = ZoneInfo(user.timezone)
        start = utc(delivery.window_start).astimezone(zone).strftime("%b %d, %Y %I:%M %p %Z")
        end = utc(delivery.window_end).astimezone(zone).strftime("%b %d, %Y %I:%M %p %Z")
        description = f"All your matching jobs discovered from {start} to {end}."
    else:
        first = jobs[0]
        subject = f"New match: {first['title']} at {first['company']}"
        heading, description = (
            "A new opportunity for you.",
            "Just discovered, based on your saved preferences.",
        )
    context = dict(
        subject=subject,
        heading=heading,
        description=description,
        preheader=description,
        label="YOUR DAILY RECAP" if recap else "JOBPING UPDATES",
        recap=recap,
        app_url=app_url,
        recap_url=recap_url,
        unsubscribe_url=unsubscribe,
    )
    visible = jobs[:100]
    while True:
        html = ENV.get_template("email.html").render(
            **context, jobs=visible, overflow=len(jobs) - len(visible)
        )
        if len(html.encode()) < 90_000 or not visible:
            break
        visible = visible[: max(0, len(visible) - 10)]
    lines = [heading, description, ""]
    for job in visible:
        lines.extend(
            [
                f"{job['company']} — {job['title']}",
                f"{job['location']} | {job['job_type']} | {job['season']} | {job['date_text']}",
                "Applications closed" if job["closed"] else job["apply_url"],
                "",
            ]
        )
    if recap:
        lines.append(f"Complete recap ({len(jobs)} matches): {recap_url}")
    lines.extend([f"Manage preferences: {app_url}/profile", f"Unsubscribe: {unsubscribe}"])
    sender = user.sender if user.provider == "byok" else os.environ["RESEND_FROM"]
    return {
        "from": f"JobPing <{sender}>",
        "to": [user.email],
        "subject": " ".join(subject.split())[:240],
        "html": html,
        "text": "\n".join(lines),
        "headers": {
            "List-Unsubscribe": f"<{unsubscribe}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
