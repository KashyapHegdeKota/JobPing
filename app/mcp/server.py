"""JobPing MCP v2 stdio server and dependency-injected facade.

The runnable server uses the official ``mcp>=2,<3`` Python SDK. Every tool is a
high-level business operation; raw SQL, arbitrary files, shell commands, and
browser controls are intentionally absent from this transport.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.applications.resolver import AnswerResolver
from app.applications.service import ApplicationService
from app.candidate.loader import load_candidate_profile
from app.candidate.models import CandidateProfile
from app.db.repository import DatabaseRepository
from app.mcp.applications import (
    application_get_checkpoint,
    application_lookup_answer,
    application_mark_failed,
    application_mark_ready,
    application_mark_review_required,
    application_mark_submitted,
    application_mark_verification_complete,
    application_mark_verification_required,
    application_save_answer,
    application_start,
)
from app.mcp.candidate import candidate_get_profile, candidate_get_resume_path
from app.mcp.jobs import jobs_get as mcp_jobs_get
from app.mcp.jobs import jobs_get_next as mcp_jobs_get_next
from mcp.server import MCPServer

DEFAULT_PRIVATE_DIR = Path("private")


class _MCPRuntime:
    """Own database resources and sanitized candidate configuration for one server."""

    def __init__(
        self,
        database_url: str,
        *,
        candidate_path: Path | None,
        answers_path: Path | None,
        stories_path: Path | None,
        rules_path: Path | None,
    ) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.profile = load_candidate_profile(candidate_path) if candidate_path else None
        self.resolver = AnswerResolver(
            self.profile,
            answers=answers_path if answers_path and answers_path.is_file() else None,
            stories=stories_path if stories_path and stories_path.is_file() else None,
            rules=rules_path if rules_path and rules_path.is_file() else None,
        )

    @asynccontextmanager
    async def repository(self) -> AsyncIterator[DatabaseRepository]:
        async with self.sessions() as session:
            async with session.begin():
                yield DatabaseRepository(session)

    async def aclose(self) -> None:
        await self.engine.dispose()


def _private_path(path: Path | None, name: str) -> Path | None:
    """Use explicit paths strictly; optional default files may be absent."""
    if path is not None:
        return path
    default = DEFAULT_PRIVATE_DIR / name
    return default if default.is_file() else None


def create_mcp_server(
    database_url: str | None = None,
    *,
    candidate_path: Path | None = None,
    answers_path: Path | None = None,
    stories_path: Path | None = None,
    rules_path: Path | None = None,
) -> MCPServer:
    """Build a runnable MCP v2 server with owned SQLAlchemy resources."""
    resolved_url = database_url or os.environ.get("DATABASE_URL")
    if not resolved_url:
        raise ValueError("DATABASE_URL is required to start the JobPing MCP server")
    runtime = _MCPRuntime(
        resolved_url,
        candidate_path=_private_path(candidate_path, "candidate.json"),
        answers_path=_private_path(answers_path, "answers.json"),
        stories_path=_private_path(stories_path, "stories.json"),
        rules_path=_private_path(rules_path, "application_rules.json"),
    )

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await runtime.aclose()

    mcp = MCPServer(
        name="jobping",
        version="0.1.0",
        description="Deterministic JobPing facts and application checkpoints for Codex Chrome.",
        instructions=(
            "Use Codex Chrome for browser interaction. Never invent candidate facts "
            "or bypass verification."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(name="jobs_get_next")
    async def jobs_get_next_tool() -> dict[str, object] | None:
        async with runtime.repository() as repository:
            return await mcp_jobs_get_next(repository)

    @mcp.tool(name="jobs_get")
    async def jobs_get_tool(job_id: int) -> dict[str, object] | None:
        async with runtime.repository() as repository:
            return await mcp_jobs_get(repository, job_id)

    @mcp.tool(name="candidate_get_profile")
    async def candidate_get_profile_tool() -> dict[str, object]:
        if runtime.profile is None:
            raise ValueError("candidate profile is not configured")
        return candidate_get_profile(runtime.profile)

    @mcp.tool(name="candidate_get_resume_path")
    async def candidate_get_resume_path_tool() -> str:
        if runtime.profile is None:
            raise ValueError("candidate profile is not configured")
        return candidate_get_resume_path(runtime.profile)

    @mcp.tool(name="application_lookup_answer")
    async def application_lookup_answer_tool(question: str) -> dict[str, object]:
        return await application_lookup_answer(runtime.resolver, question)

    @mcp.tool(name="application_start")
    async def application_start_tool(job_id: int, current_url: str) -> dict[str, object]:
        async with runtime.repository() as repository:
            service = ApplicationService(repository)
            return (await application_start(service, job_id, current_url)).model_dump(mode="json")

    @mcp.tool(name="application_save_answer")
    async def application_save_answer_tool(
        job_id: int,
        question: str,
        answer: str,
        source: str,
        generated: bool = False,
        confidence: float | None = None,
    ) -> dict[str, object]:
        async with runtime.repository() as repository:
            return await application_save_answer(
                ApplicationService(repository),
                job_id,
                question,
                answer,
                source,
                generated=generated,
                confidence=confidence,
            )

    @mcp.tool(name="application_mark_verification_required")
    async def application_mark_verification_required_tool(
        job_id: int, verification_type: str, current_url: str, instruction: str
    ) -> dict[str, object]:
        async with runtime.repository() as repository:
            return (
                await application_mark_verification_required(
                    ApplicationService(repository),
                    job_id,
                    verification_type,
                    current_url,
                    instruction,
                )
            ).model_dump(mode="json")

    @mcp.tool(name="application_get_checkpoint")
    async def application_get_checkpoint_tool(job_id: int) -> dict[str, object]:
        async with runtime.repository() as repository:
            return await application_get_checkpoint(ApplicationService(repository), job_id)

    @mcp.tool(name="application_mark_verification_complete")
    async def application_mark_verification_complete_tool(
        job_id: int, current_url: str | None = None
    ) -> dict[str, object]:
        async with runtime.repository() as repository:
            return (
                await application_mark_verification_complete(
                    ApplicationService(repository), job_id, current_url
                )
            ).model_dump(mode="json")

    @mcp.tool(name="application_mark_review_required")
    async def application_mark_review_required_tool(
        job_id: int, current_url: str | None = None
    ) -> dict[str, object]:
        async with runtime.repository() as repository:
            return (
                await application_mark_review_required(
                    ApplicationService(repository), job_id, current_url
                )
            ).model_dump(mode="json")

    @mcp.tool(name="application_mark_ready")
    async def application_mark_ready_tool(
        job_id: int, current_url: str | None = None
    ) -> dict[str, object]:
        async with runtime.repository() as repository:
            return (
                await application_mark_ready(ApplicationService(repository), job_id, current_url)
            ).model_dump(mode="json")

    @mcp.tool(name="application_mark_submitted")
    async def application_mark_submitted_tool(
        job_id: int, confirmation_url: str | None = None
    ) -> dict[str, object]:
        async with runtime.repository() as repository:
            return (
                await application_mark_submitted(
                    ApplicationService(repository), job_id, confirmation_url
                )
            ).model_dump(mode="json")

    @mcp.tool(name="application_mark_failed")
    async def application_mark_failed_tool(
        job_id: int,
        failure_code: str,
        failure_message: str | None = None,
        current_url: str | None = None,
    ) -> dict[str, object]:
        async with runtime.repository() as repository:
            return (
                await application_mark_failed(
                    ApplicationService(repository),
                    job_id,
                    failure_code,
                    failure_message,
                    current_url,
                )
            ).model_dump(mode="json")

    return mcp


def main() -> None:
    """Run the official MCP v2 stdio transport."""

    def configured(name: str) -> Path | None:
        value = os.environ.get(name)
        return Path(value) if value else None

    mcp = create_mcp_server(
        candidate_path=configured("JOBPING_CANDIDATE_PATH"),
        answers_path=configured("JOBPING_ANSWERS_PATH"),
        stories_path=configured("JOBPING_STORIES_PATH"),
        rules_path=configured("JOBPING_RULES_PATH"),
    )
    mcp.run()


class JobPingMCPServer:
    """High-level tool collection for a single request/session context."""

    def __init__(
        self,
        repository: DatabaseRepository,
        profile: CandidateProfile | None = None,
        *,
        candidate_path: Path | None = None,
        resolver: AnswerResolver | None = None,
    ) -> None:
        if profile is None and candidate_path is not None:
            profile = load_candidate_profile(candidate_path)
        self.repository = repository
        self.profile = profile
        self.service = ApplicationService(repository)
        self.resolver = resolver or AnswerResolver(profile)

    async def jobs_get_next(self) -> dict[str, object] | None:
        return await mcp_jobs_get_next(self.repository)

    async def jobs_get(self, job_id: int) -> dict[str, object] | None:
        return await mcp_jobs_get(self.repository, job_id)

    def candidate_get_profile(self) -> dict[str, object]:
        if self.profile is None:
            raise ValueError("candidate profile is not configured")
        return candidate_get_profile(self.profile)

    def candidate_get_resume_path(self) -> str:
        if self.profile is None:
            raise ValueError("candidate profile is not configured")
        return candidate_get_resume_path(self.profile)

    async def application_lookup_answer(self, question: str) -> dict[str, object]:
        return await application_lookup_answer(self.resolver, question)

    async def application_start(self, job_id: int, current_url: str) -> dict[str, object]:
        return (await application_start(self.service, job_id, current_url)).model_dump(mode="json")

    async def application_save_answer(self, job_id: int, **kwargs: object) -> dict[str, object]:
        return await application_save_answer(self.service, job_id, **kwargs)  # type: ignore[arg-type]

    async def application_mark_verification_required(
        self, job_id: int, **kwargs: object
    ) -> dict[str, object]:
        return (
            await application_mark_verification_required(self.service, job_id, **kwargs)  # type: ignore[arg-type]
        ).model_dump(mode="json")

    async def application_get_checkpoint(self, job_id: int) -> dict[str, object]:
        return await application_get_checkpoint(self.service, job_id)

    async def application_mark_verification_complete(
        self, job_id: int, current_url: str | None = None
    ) -> dict[str, object]:
        return (
            await application_mark_verification_complete(self.service, job_id, current_url)
        ).model_dump(mode="json")

    async def application_mark_review_required(
        self, job_id: int, current_url: str | None = None
    ) -> dict[str, object]:
        return (
            await application_mark_review_required(self.service, job_id, current_url)
        ).model_dump(mode="json")

    async def application_mark_ready(
        self, job_id: int, current_url: str | None = None
    ) -> dict[str, object]:
        return (await application_mark_ready(self.service, job_id, current_url)).model_dump(
            mode="json"
        )

    async def application_mark_submitted(
        self, job_id: int, confirmation_url: str | None = None
    ) -> dict[str, object]:
        return (
            await application_mark_submitted(self.service, job_id, confirmation_url)
        ).model_dump(mode="json")

    async def application_mark_failed(self, job_id: int, **kwargs: object) -> dict[str, object]:
        return (
            await application_mark_failed(self.service, job_id, **kwargs)  # type: ignore[arg-type]
        ).model_dump(mode="json")


__all__ = ["JobPingMCPServer", "create_mcp_server", "main"]


if __name__ == "__main__":
    main()
