"""Integration coverage for the Simplify ingestion and persistence boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_asyncio
from app.db.models import Base, Company, JobPosting, StatusLog
from app.db.repository import DatabaseRepository
from app.pipelines.simplify_pipeline import PipelineResult, SimplifyPipeline
from app.schemas.job import JobType
from app.scrapers.github_client import GitHubCommitDetail, GitHubFilePatch
from app.services.deduplicator import DeduplicationState, JobDeduplicator
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class InMemoryRedis:
    """Minimal Redis script contract used by the production deduplicator."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def eval(self, script: str, number_of_keys: int, *args: object) -> int:
        assert "redis.call" in script
        assert number_of_keys == 1
        key, content_hash, closed, ttl = cast(tuple[str, str, str, int], args)
        previous = self.values.get(key)
        self.expirations[key] = ttl
        if previous is None:
            self.values[key] = content_hash
            return 1
        if previous == content_hash:
            return 4
        self.values[key] = content_hash
        return 3 if closed == "1" else 2


class CommitFeed:
    """Deterministic GitHub-compatible commit source with no network access."""

    def __init__(self, commits: dict[str, GitHubCommitDetail]) -> None:
        self.commits = commits

    async def get_commit(
        self,
        owner: str,
        repo: str,
        ref: str,
        *,
        target_markdown_paths: set[str] | None = None,
    ) -> GitHubCommitDetail:
        assert (owner, repo) == ("SimplifyJobs", "Summer2026-Internships")
        assert target_markdown_paths == {"README.md"}
        return self.commits[ref]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Provide an isolated PostgreSQL-like relational store for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def markdown_row(*, location: str = "New York, NY") -> str:
    return (
        "| Acme | Software Engineer Intern | "
        f"{location} | [Apply](https://acme.example/jobs/1) | Aug 11 |"
    )


def unified_diff(*changed_lines: str) -> str:
    return "\n".join(
        (
            "diff --git a/README.md b/README.md",
            "--- a/README.md",
            "+++ b/README.md",
            "@@ -10,1 +10,1 @@",
            *changed_lines,
        )
    )


def commit(ref: str, patch: str) -> GitHubCommitDetail:
    return GitHubCommitDetail(
        sha=ref,
        message=f"fixture {ref}",
        html_url=f"https://github.example/commit/{ref}",
        authored_at=datetime(2026, 8, 11, tzinfo=UTC),
        files=(GitHubFilePatch("README.md", "modified", 1, 1, 2, patch),),
    )


async def persist_result(
    result: PipelineResult,
    repository: DatabaseRepository,
    previous_states: dict[str, str],
) -> None:
    """Compose classification with the existing repository persistence boundary."""
    for state, items in result.items.items():
        if state is DeduplicationState.NO_OP:
            continue
        for item in items:
            posting = await repository.save_job_posting(item.job)
            previous = previous_states.get(item.job.base_hash)
            await repository.log_status_change(posting.id, previous, state.value)
            previous_states[item.job.base_hash] = state.value


@pytest.mark.asyncio
async def test_patch_lifecycle_classifies_and_persists_every_state(
    session: AsyncSession,
) -> None:
    """Exercise diff parsing, normalization, hashing, Redis state, and SQL writes."""
    initial = markdown_row()
    relocated = markdown_row(location="New York, NY / Remote")
    commits = {
        "add": commit("add", unified_diff(f"+{initial}")),
        "same": commit("same", unified_diff(f"+{initial}")),
        "update": commit("update", unified_diff(f"-{initial}", f"+{relocated}")),
        "close": commit("close", unified_diff(f"-{relocated}")),
    }
    redis = InMemoryRedis()
    deduplicator = JobDeduplicator(cast(Any, cast(Redis, redis)), ttl_seconds=3600)
    pipeline = SimplifyPipeline(
        cast(Any, CommitFeed(commits)),
        deduplicator,
        season=2026,
        job_type=JobType.INTERNSHIP,
        target_readme_paths={"README.md"},
    )
    repository = DatabaseRepository(session)
    previous_states: dict[str, str] = {}

    observed: list[DeduplicationState] = []
    for ref in ("add", "same", "update", "close"):
        result = await pipeline.process_commit("SimplifyJobs", "Summer2026-Internships", ref)
        states = [state for state, items in result.items.items() if items]
        assert len(states) == 1
        observed.extend(states)
        await persist_result(result, repository, previous_states)

    assert observed == [
        DeduplicationState.NEW_ROLE,
        DeduplicationState.NO_OP,
        DeduplicationState.ROLE_UPDATED,
        DeduplicationState.ROLE_CLOSED,
    ]
    assert await session.scalar(select(func.count()).select_from(Company)) == 1
    assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
    assert await session.scalar(select(func.count()).select_from(StatusLog)) == 3

    posting = (await session.scalars(select(JobPosting))).one()
    assert posting.location == "New York, NY / Remote"
    assert posting.is_closed is True
    logs = (await session.scalars(select(StatusLog).order_by(StatusLog.id))).all()
    assert [(log.previous_state, log.new_state) for log in logs] == [
        (None, "NEW_ROLE"),
        ("NEW_ROLE", "ROLE_UPDATED"),
        ("ROLE_UPDATED", "ROLE_CLOSED"),
    ]
    assert list(redis.expirations.values()) == [3600]
