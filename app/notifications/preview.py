"""Generate synthetic HTML previews without credentials or external services."""

from __future__ import annotations

from pathlib import Path

from app.notifications.render import ENV


def preview(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    jobs = [
        {
            "company": "Acme",
            "title": "Software Engineer Intern",
            "location": "New York, NY",
            "job_type": "internship",
            "season": 2027,
            "closed": False,
            "apply_url": "https://example.com/apply",
            "date_text": "Discovered Oct 4, 2026",
        },
        {
            "company": "Northstar",
            "title": "Graduate Software Engineer — Infrastructure & Developer Tools",
            "location": "Remote",
            "job_type": "new grad",
            "season": 2027,
            "closed": False,
            "apply_url": "https://example.com/apply",
            "date_text": "Posted Oct 4, 2026",
            "kind": "reposted",
        },
        {
            "company": "Orbit",
            "title": "Backend Engineer",
            "location": "",
            "job_type": "new grad",
            "season": 2027,
            "closed": True,
            "apply_url": "",
            "date_text": "Posted Oct 3, 2026",
        },
    ]
    for recap in (False, True):
        title = "2 new opportunities and 1 reposted role." if recap else "A new opportunity for you."
        html = ENV.get_template("email.html").render(
            subject=title,
            preheader="Your matching jobs from JobPing",
            heading=title,
            description=(
                "All your matches from September 5, 8 PM to September 6, 8 PM (Phoenix)."
                if recap
                else "Just discovered, based on your saved preferences."
            ),
            label="YOUR DAILY RECAP" if recap else "NEW JOB MATCH",
            recap=recap,
            jobs=jobs if recap else jobs[:1],
            new_jobs=[jobs[0], jobs[2]] if recap else [],
            reposted_jobs=[jobs[1]] if recap else [],
            new_count=2,
            reposted_count=1,
            overflow=0,
            app_url="https://example.com",
            recap_url="https://example.com/recap",
            unsubscribe_url="https://example.com/unsubscribe",
        )
        (output / ("recap.html" if recap else "alert.html")).write_text(html, encoding="utf-8")
