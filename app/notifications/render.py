"""Escaped, size-bounded HTML and matching text email bodies."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, EmailDelivery, JobOccurrence, JobPosting, Subscriber
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


async def occurrence_rows(session: AsyncSession, ids: list[int]) -> list[dict]:
    """Render each occurrence with its immutable URL/date lifecycle snapshot."""
    if not ids:
        return []
    rows = await session.execute(
        select(JobOccurrence, JobPosting, Company.name)
        .join(JobPosting, JobPosting.id == JobOccurrence.job_id)
        .join(Company, Company.id == JobPosting.company_id)
        .where(JobOccurrence.id.in_(ids))
        .order_by(
            Company.name,
            JobPosting.title,
            JobPosting.id,
            JobOccurrence.observed_at,
            JobOccurrence.id,
        )
    )
    items = []
    for occurrence, job, company in rows:
        date = occurrence.posted_at or occurrence.observed_at
        date_text = f"Posted {date.strftime('%b %d, %Y').replace(' 0', ' ')}"
        items.append(
            {
                "id": job.id,
                "occurrence_id": occurrence.id,
                "kind": occurrence.kind,
                "company": company,
                "title": job.title,
                "location": job.location,
                "job_type": job.job_type.value.replace("_", " "),
                "season": job.season,
                "closed": occurrence.closed_at is not None,
                "apply_url": safe_url(occurrence.apply_url),
                "date_text": date_text,
            }
        )
    return items


async def payload(session: AsyncSession, delivery: EmailDelivery, user: Subscriber) -> dict:
    jobs = (
        await occurrence_rows(session, delivery.occurrence_ids)
        if delivery.occurrence_ids
        else await job_rows(session, delivery.job_ids)
    )
    recap = delivery.kind == "recap"
    reposted_alert = bool(not recap and jobs and jobs[0].get("kind") == "reposted")
    new_jobs = [job for job in jobs if job.get("kind") != "reposted"]
    reposted_jobs = [job for job in jobs if job.get("kind") == "reposted"]
    new_count = delivery.new_count if recap and delivery.total_matches else len(new_jobs)
    reposted_count = (
        delivery.reposted_count if recap and delivery.total_matches else len(reposted_jobs)
    )
    total_matches = delivery.total_matches if recap and delivery.total_matches else len(jobs)
    app_url = os.environ["NOTIFICATION_APP_URL"].rstrip("/")
    api_url = os.environ["NOTIFICATION_API_URL"].rstrip("/")
    unsubscribe = f"{api_url}/api/v1/notifications/unsubscribe/{user.unsubscribe_token}"
    recap_url = f"{app_url}/profile/recaps/{delivery.id}"
    if delivery.kind == "test":
        subject, heading = "Your JobPing email connection works", "You're connected."
        description = "JobPing can now send your job alerts and daily recaps using this sender."
    elif recap:
        if reposted_count:
            subject = f"Your JobPing recap: {new_count} new, {reposted_count} reposted"
        else:
            subject = f"Your JobPing recap: {new_count} new matches"
        if new_count and reposted_count:
            heading = f"{new_count} new opportunities and {reposted_count} reposted roles."
        elif reposted_count:
            heading = f"{reposted_count} roles have been reposted."
        else:
            heading = f"{new_count} new opportunities."
        zone = ZoneInfo(user.timezone)
        start = utc(delivery.window_start).astimezone(zone).strftime("%b %d, %Y %I:%M %p %Z")
        end = utc(delivery.window_end).astimezone(zone).strftime("%b %d, %Y %I:%M %p %Z")
        description = f"All your matching jobs discovered from {start} to {end}."
    else:
        first = jobs[0]
        if reposted_alert:
            subject = f"Reposted: {first['title']} at {first['company']}"
            heading = "This role has been reposted."
            description = "A previously closed opportunity is available again."
        else:
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
        label=(
            "YOUR DAILY RECAP"
            if recap
            else "REPOSTED OPPORTUNITY" if reposted_alert else "JOBPING UPDATES"
        ),
        recap=recap,
        app_url=app_url,
        recap_url=recap_url,
        unsubscribe_url=unsubscribe,
    )
    visible = jobs[:100]
    visible_new = new_jobs[:100]
    visible_reposted = reposted_jobs[:100]
    while True:
        html = ENV.get_template("email.html").render(
            **context,
            jobs=visible,
            new_jobs=visible_new,
            reposted_jobs=visible_reposted,
            new_count=new_count,
            reposted_count=reposted_count,
            overflow=(
                (len(new_jobs) - len(visible_new)) + (len(reposted_jobs) - len(visible_reposted))
                if recap
                else len(jobs) - len(visible)
            ),
        )
        has_visible = bool(visible_new or visible_reposted) if recap else bool(visible)
        if len(html.encode()) < 90_000 or not has_visible:
            break
        if recap:
            if visible_reposted:
                visible_reposted = visible_reposted[: max(0, len(visible_reposted) - 10)]
            else:
                visible_new = visible_new[: max(0, len(visible_new) - 10)]
        else:
            visible = visible[: max(0, len(visible) - 10)]
    lines = [heading, description, ""]
    if recap:
        lines = [heading, description, ""]
        if visible_new:
            lines.extend([f"NEW JOBS · {len(new_jobs)}", ""])
        for job in visible_new:
            lines.extend(_text_job_lines(job))
        if visible_reposted:
            lines.extend([f"REPOSTED JOBS · {len(reposted_jobs)}", ""])
        for job in visible_reposted:
            lines.extend(["REPOSTED", *_text_job_lines(job)])
    elif reposted_alert:
        lines.append("REPOSTED OPPORTUNITY")
        lines.append("")
    for job in ([] if recap else visible):
        lines.extend(_text_job_lines(job))
    if recap:
        lines.append(f"Complete recap ({total_matches} matches): {recap_url}")
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


def _text_job_lines(job: dict[str, object]) -> list[str]:
    return [
        f"{job['company']} — {job['title']}",
        f"{job['location']} | {job['job_type']} | {job['season']} | {job['date_text']}",
        "Applications closed" if job["closed"] else job["apply_url"],
        "",
    ]
