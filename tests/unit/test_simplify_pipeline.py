"""Focused tests for Simplify ingestion orchestration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.pipelines.simplify_pipeline import SimplifyPipeline
from app.schemas.job import JobType
from app.scrapers.github_client import GitHubCommitDetail, GitHubFilePatch
from app.services.deduplicator import DeduplicationState


def row(*, company: str = "Acme", title: str = "Engineer Intern", closed: bool = False) -> str:
    lock = " 🔒" if closed else ""
    return f"| {company}{lock} | {title} | Remote | [Apply](https://acme.test/apply) | Today |"


def detail(patch: str, filename: str = "README.md") -> GitHubCommitDetail:
    return GitHubCommitDetail(
        "abc123",
        "roles",
        "https://github.test/commit/abc123",
        datetime(2026, 8, 11, tzinfo=UTC),
        (GitHubFilePatch(filename, "modified", 1, 1, 2, patch),),
    )


class FakeGitHub:
    def __init__(
        self, commit: GitHubCommitDetail | None = None, error: Exception | None = None
    ) -> None:
        self.commit = commit
        self.error = error

    async def get_commit(self, *args: object, **kwargs: object) -> GitHubCommitDetail:
        if self.error:
            raise self.error
        assert self.commit is not None
        return self.commit


class FakeDeduplicator:
    def __init__(self, states: list[DeduplicationState] | None = None) -> None:
        self.states = states or [DeduplicationState.NEW_ROLE]
        self.calls: list[dict[str, object]] = []

    async def classify_and_update(self, **kwargs: object) -> DeduplicationState:
        self.calls.append(kwargs)
        return self.states[len(self.calls) - 1]


def pipeline(commit: GitHubCommitDetail, dedupe: FakeDeduplicator) -> SimplifyPipeline:
    return SimplifyPipeline(  # type: ignore[arg-type]
        FakeGitHub(commit), dedupe, season=2026, job_type=JobType.INTERNSHIP
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "closed"),
    [
        (DeduplicationState.NEW_ROLE, False),
        (DeduplicationState.ROLE_UPDATED, False),
        (DeduplicationState.ROLE_CLOSED, True),
        (DeduplicationState.NO_OP, False),
    ],
)
async def test_categorizes_states_and_retains_provenance(
    state: DeduplicationState, closed: bool
) -> None:
    dedupe = FakeDeduplicator([state])
    result = await pipeline(detail("+" + row(closed=closed)), dedupe).process_detail(
        detail("+" + row(closed=closed))
    )

    item = result.categorized(state)[0]
    assert item.job.company_name == "Acme"
    assert item.job.is_closed is closed
    assert item.commit_sha == "abc123"
    assert item.raw.source_id == "abc123:README.md"
    assert dedupe.calls[0]["content_hash"] == item.job.content_hash


@pytest.mark.asyncio
async def test_replaced_row_processes_addition_without_false_close() -> None:
    changed = "-" + row() + "\n+" + row()
    dedupe = FakeDeduplicator()

    result = await pipeline(detail(changed), dedupe).process_detail(detail(changed))

    assert len(dedupe.calls) == 1
    assert dedupe.calls[0]["is_closed"] is False
    assert len(result.categorized(DeduplicationState.NEW_ROLE)) == 1


@pytest.mark.asyncio
async def test_removed_active_row_becomes_closed_when_not_replaced() -> None:
    dedupe = FakeDeduplicator([DeduplicationState.ROLE_CLOSED])
    result = await pipeline(detail("-" + row()), dedupe).process_detail(detail("-" + row()))

    assert dedupe.calls[0]["is_closed"] is True
    assert result.categorized(DeduplicationState.ROLE_CLOSED)[0].job.is_closed is True


@pytest.mark.asyncio
async def test_malformed_non_target_and_duplicate_rows_are_isolated() -> None:
    patch = "+| broken | row | only |\n+" + row() + "\n+" + row()
    commit = detail(patch)
    dedupe = FakeDeduplicator()
    result = await pipeline(commit, dedupe).process_detail(commit)

    assert len(dedupe.calls) == 1
    assert len(result.rejected) == 1
    ignored = detail("+" + row(), "jobs.md")
    assert not (await pipeline(ignored, FakeDeduplicator()).process_detail(ignored)).items[
        DeduplicationState.NEW_ROLE
    ]


@pytest.mark.asyncio
async def test_dependency_errors_propagate() -> None:
    failure = RuntimeError("github unavailable")
    pipe = SimplifyPipeline(  # type: ignore[arg-type]
        FakeGitHub(error=failure),
        FakeDeduplicator(),
        season=2026,
        job_type=JobType.INTERNSHIP,
    )
    with pytest.raises(RuntimeError, match="github unavailable"):
        await pipe.process_commit("SimplifyJobs", "Summer2026-Internships", "main")

    class BrokenDeduplicator(FakeDeduplicator):
        async def classify_and_update(self, **kwargs: object) -> DeduplicationState:
            raise RuntimeError("redis unavailable")

    commit = detail("+" + row())
    with pytest.raises(RuntimeError, match="redis unavailable"):
        await pipeline(commit, BrokenDeduplicator()).process_detail(commit)
