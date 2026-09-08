"""Backfill job creation timestamps from the local Simplify Git history.

Run from the repository root with::

    poetry run python scripts/backfill_simplify_dates.py --dry-run
    poetry run python scripts/backfill_simplify_dates.py
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import selectors
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.models import JobPosting  # noqa: E402

LOGGER = logging.getLogger("jobping.backfill_simplify_dates")
DEFAULT_REPOSITORY_URLS = (
    "https://github.com/SimplifyJobs/Summer2027-Internships.git",
    "https://github.com/SimplifyJobs/New-Grad-Positions.git",
)


def _run_git(arguments: Sequence[str], *, cwd: Path | None = None) -> str:
    """Run Git without invoking a shell and return its standard output."""
    command = ["git", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or is not available on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or "unknown Git error"
        raise RuntimeError(f"Git command failed: {detail}") from exc
    return completed.stdout


def _clone_repository(repository_url: str, destination: Path) -> None:
    LOGGER.info("Cloning %s into a temporary directory", repository_url)
    _run_git(
        (
            "clone",
            "--quiet",
            "--no-tags",
            "--branch",
            "dev",
            "--single-branch",
            repository_url,
            str(destination),
        )
    )


def _parse_git_timestamp(value: str) -> datetime | None:
    """Parse Git's strict ISO-8601 timestamp output."""
    timestamp = value.strip().splitlines()[0] if value.strip() else ""
    if not timestamp:
        return None
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _url_candidates(added_line: str) -> tuple[str, ...]:
    """Extract HTTP URL candidates from one added Markdown line."""
    candidates: list[str] = []
    for token in added_line.replace("(", " ").split():
        start = min(
            (
                position
                for scheme in ("https://", "http://")
                if (position := token.find(scheme)) >= 0
            ),
            default=-1,
        )
        if start < 0:
            continue
        candidate = html.unescape(token[start:]).split('"', 1)[0].split("'", 1)[0].split("|", 1)[0]
        candidates.append(candidate)
        while candidate and candidate[-1] in ")]},;.>":
            candidate = candidate[:-1]
            candidates.append(candidate)
    return tuple(candidates)


def parse_patch_history(patch_history: str, target_urls: set[str]) -> dict[str, datetime]:
    """Map target URLs to their first chronological addition in a Git patch stream."""
    unresolved = set(target_urls)
    timestamps: dict[str, datetime] = {}
    current_date: datetime | None = None
    for line in patch_history.splitlines():
        if line.startswith("Date:"):
            current_date = _parse_git_timestamp(line.removeprefix("Date:"))
            continue
        if current_date is None or not line.startswith("+") or line.startswith("+++"):
            continue
        for candidate in _url_candidates(line[1:]):
            if candidate in unresolved:
                timestamps[candidate] = current_date
                unresolved.remove(candidate)
        if not unresolved:
            break
    return timestamps


def collect_earliest_url_timestamps(repository: Path, target_urls: set[str]) -> dict[str, datetime]:
    """Scan one repository history once and return first-added target URL dates."""
    output = _run_git(
        (
            "log",
            "--reverse",
            "-p",
            "-U0",
            "--date=iso-strict",
            "--format=Date: %cI",
            "--",
            "README*.md",
        ),
        cwd=repository,
    )
    return parse_patch_history(output, target_urls)


async def _backfill(
    *,
    database_url: str,
    repository_urls: Sequence[str],
    dry_run: bool,
) -> tuple[int, int, int]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(JobPosting.id, JobPosting.apply_url, JobPosting.posted_at).order_by(
                        JobPosting.id
                    )
                )
            ).all()
            LOGGER.info("Loaded %d jobs from the database", len(rows))

            with tempfile.TemporaryDirectory(prefix="jobping-simplify-") as temp_dir:
                repositories = tuple(
                    Path(temp_dir) / f"simplify-{index}" for index in range(len(repository_urls))
                )
                await asyncio.gather(
                    *(
                        asyncio.to_thread(_clone_repository, repository_url, repository)
                        for repository_url, repository in zip(
                            repository_urls, repositories, strict=True
                        )
                    )
                )

                target_urls = {apply_url for _, apply_url, _ in rows}
                history_maps = await asyncio.gather(
                    *(
                        asyncio.to_thread(collect_earliest_url_timestamps, repository, target_urls)
                        for repository in repositories
                    )
                )
                timestamps: dict[str, datetime] = {}
                for history_map in history_maps:
                    for apply_url, timestamp in history_map.items():
                        previous = timestamps.get(apply_url)
                        if previous is None or timestamp < previous:
                            timestamps[apply_url] = timestamp

                populated = 0
                moved_earlier = 0
                unchanged = 0
                unmatched = 0
                updates = []

                for job_id, apply_url, current_posted_at in rows:
                    if apply_url not in timestamps:
                        unmatched += 1
                        continue

                    new_posted_at = timestamps[apply_url]
                    if current_posted_at is None:
                        populated += 1
                        updates.append({"_job_id": job_id, "_posted_at": new_posted_at})
                    elif new_posted_at < current_posted_at:
                        moved_earlier += 1
                        updates.append({"_job_id": job_id, "_posted_at": new_posted_at})
                    else:
                        unchanged += 1

                LOGGER.info(
                    "Single-pass history scan: populated=%d moved_earlier=%d "
                    "unchanged=%d unmatched=%d",
                    populated,
                    moved_earlier,
                    unchanged,
                    unmatched,
                )

            if updates and not dry_run:
                await session.execute(
                    update(JobPosting.__table__)
                    .where(JobPosting.id == bindparam("_job_id"))
                    .where(
                        (JobPosting.posted_at.is_(None))
                        | (JobPosting.posted_at > bindparam("_posted_at"))
                    )
                    .values(posted_at=bindparam("_posted_at")),
                    updates,
                )
                await session.commit()
                LOGGER.info("Committed %d historical timestamps", len(updates))
            elif dry_run:
                LOGGER.info("Dry run: no database rows were changed")

            return len(rows), populated, moved_earlier, unchanged, unmatched
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill JobPing created_at values from Simplify's Git history."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Async SQLAlchemy URL; defaults to DATABASE_URL.",
    )
    parser.add_argument(
        "--repository-url",
        action="append",
        dest="repository_urls",
        help=("Simplify Git repository URL. Repeat to override the two current-cycle defaults."),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve timestamps without updating the database.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL or --database-url is required.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    coroutine = _backfill(
        database_url=args.database_url,
        repository_urls=tuple(args.repository_urls or DEFAULT_REPOSITORY_URLS),
        dry_run=args.dry_run,
    )
    if os.name == "nt":
        scanned, matched, missing = asyncio.run(
            coroutine,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        scanned, matched, missing = asyncio.run(coroutine)
    LOGGER.info("Backfill complete: scanned=%d matched=%d missing=%d", scanned, matched, missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
