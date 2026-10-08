"""Real scheduler callbacks through mocked ATS, isolated SQL and in-memory Redis."""

from __future__ import annotations

import asyncio
from typing import Self

import httpx
import pytest
from app import cli
from app.db.models import Company, DiscoveryEvent, JobOccurrence, JobPosting
from app.pipelines import direct_ats_pipeline as direct
from app.scrapers.ats_sources import ATSSource, ConfiguredATSScraper
from app.services.deduplicator import DeduplicationState
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class MemoryDeduplicator:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def classify_and_update(
        self, *, base_hash: str, content_hash: str, is_closed: bool
    ) -> DeduplicationState:
        previous = self.values.get(base_hash)
        self.values[base_hash] = content_hash
        if previous is None:
            return DeduplicationState.NEW_ROLE
        if previous == content_hash:
            return DeduplicationState.NO_OP
        return DeduplicationState.ROLE_CLOSED if is_closed else DeduplicationState.ROLE_UPDATED


def board(provider: str, token: str, **updates: object) -> ATSSource:
    return ATSSource.model_validate(
        {"provider": provider, "token": token, "company": "Acme", "season": 2027} | updates
    )


async def test_scheduler_fetch_filter_persist_repeat_and_failure_isolation(
    application_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTIFICATIONS_SUPPRESS_DISCOVERY", "false")
    requests: list[httpx.Request] = []
    absent = False

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "broken" in request.url.path:
            return httpx.Response(404)
        if "greenhouse" in request.url.host:
            titles = (
                []
                if absent
                else [
                    "Software Engineer Intern 2027",
                    "New Grad Software Engineer 2027",
                    "Software Engineer Intern 2026",
                    "Software Engineer",
                    "Intern",
                    "Senior New Grad Software Engineer 2027",
                ]
            )
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": index,
                            "title": title,
                            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{index}",
                            "location": {"name": "Remote"},
                        }
                        for index, title in enumerate(titles, 1)
                    ]
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "id": identifier,
                    "text": title,
                    "applyUrl": f"https://jobs.lever.co/acme/{identifier}",
                    "categories": {"location": "Remote"},
                }
                for identifier, title in (
                    ("duplicate", "Software Engineer Intern 2027"),
                    ("analyst", "Data Analyst New Grad 2027"),
                )
            ],
        )

    sources = (board("greenhouse", "broken"), board("greenhouse", "acme"), board("lever", "acme"))
    dedupe = MemoryDeduplicator()
    runs: list[direct.DirectATSRun] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def process(**kwargs: object) -> tuple[direct.DirectATSRun, ...]:
            async with application_sessions() as session:
                result = await direct.run_direct_ats_sources(
                    kwargs["sources"], dedupe, session=session, client=client
                )
                runs.extend(result)
                return result

        monkeypatch.setattr(cli, "_process_ats_sync", process)
        targets = cli._scheduler_targets(
            ["boards.greenhouse.io=10", "api.lever.co=20"],
            redis_url="unused",
            github_token=None,
            database_url="isolated",
            ats_sources=sources,
        )
        assert [item.name for item in targets] == [
            "greenhouse/broken",
            "greenhouse/acme",
            "lever/acme",
        ]
        assert [item.interval_seconds for item in targets] == [10, 10, 20]
        with pytest.raises(RuntimeError, match="ATS polling failed"):
            await targets[0].callback()
        for target in targets[1:]:
            await target.callback()
        assert len(runs[0].result.failures) == 1
        assert runs[1].fetched == 6
        assert runs[1].filtered == 4
        assert {item.job.job_type for item in runs[1].result.outcomes} == {"internship", "new_grad"}
        assert all(item.job.company_name == "Acme" for item in runs[1].result.outcomes)
        assert runs[1].result.outcomes[0].job.identity_namespace == "greenhouse:acme"
        assert runs[2].result.outcomes[0].state is DeduplicationState.NO_OP
        for target in targets[1:]:
            await target.callback()
        assert all(
            item.state is DeduplicationState.NO_OP
            for run in runs[-2:]
            for item in run.result.outcomes
        )
        absent = True
        await targets[1].callback()
        assert runs[-1].fetched == 0
        assert not client.is_closed

    async with application_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Company)) == 1
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 3
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 3
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 3
        postings = (await session.scalars(select(JobPosting))).all()
        assert all(not item.is_closed for item in postings)
        assert all(dedupe.values[item.base_hash] == item.content_hash for item in postings)
    assert len(requests) == 6


async def test_malformed_json_failure_does_not_block_other_board(
    application_sessions: async_sessionmaker[AsyncSession],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, content=b"bad json")
            if "greenhouse" in request.url.host
            else httpx.Response(200, json=[])
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with application_sessions() as session:
            results = await direct.run_direct_ats_sources(
                (board("greenhouse", "acme"), board("lever", "acme")),
                MemoryDeduplicator(),
                session=session,
                client=client,
            )
        assert len(results[0].result.failures) == 1
        assert results[1].result.failures == []
        assert not client.is_closed


async def test_configured_adapter_reuses_injected_client_and_keeps_token_provenance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 7,
                        "title": "Engineer Intern",
                        "absolute_url": "https://job-boards.greenhouse.io/board-token/jobs/7",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with ConfiguredATSScraper(
            board("greenhouse", "board-token", allow_undated=True), client=client
        ) as scraper:
            for _ in range(2):
                rows = await scraper.run()
                assert rows[0].company == "Acme"
                assert rows[0].source_id == "greenhouse:board-token:7"
                assert rows[0].season == 2027
                assert rows[0].job_type == "internship"
        assert not client.is_closed


@pytest.mark.parametrize("error", [None, RuntimeError("SQL failed"), asyncio.CancelledError()])
async def test_owned_resources_close_on_success_failure_and_cancellation(
    monkeypatch: pytest.MonkeyPatch, error: BaseException | None
) -> None:
    closed: list[str] = []

    class Engine:
        async def dispose(self) -> None:
            closed.append("engine")

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            closed.append(self.name)

    class Deduplicator:
        @staticmethod
        def from_url(url: str) -> Resource:
            return Resource("redis")

    async def run(*args: object, **kwargs: object) -> tuple[direct.DirectATSRun, ...]:
        if error is not None:
            raise error
        return ()

    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kwargs: Resource("http"))
    monkeypatch.setattr(direct, "JobDeduplicator", Deduplicator)
    monkeypatch.setattr(direct, "create_async_engine", lambda url: Engine())
    monkeypatch.setattr(
        direct, "async_sessionmaker", lambda *args, **kwargs: lambda: Resource("session")
    )
    monkeypatch.setattr(direct, "run_direct_ats_sources", run)
    if error is None:
        await direct.process_direct_ats_sync(
            sources=(), redis_url="unused", database_url="isolated"
        )
    else:
        with pytest.raises(type(error)):
            await direct.process_direct_ats_sync(
                sources=(), redis_url="unused", database_url="isolated"
            )
    assert closed == ["session", "engine", "redis", "http"]
