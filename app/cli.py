"""Command-line entry points for local JobPing ingestion runs."""

from __future__ import annotations

import asyncio
import json
import os
import selectors
from collections.abc import Awaitable
from datetime import timedelta
from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.repository import DatabaseRepository
from app.pipelines.simplify_pipeline import PipelineResult, SimplifyPipeline
from app.scheduler import PollTarget, SchedulerDaemon, parse_intervals
from app.schemas.job import JobType
from app.scrapers.github_client import GitHubClient
from app.services.db_audit import AuditReport, DatabaseAuditService
from app.services.deduplicator import DeduplicationState, JobDeduplicator

app = typer.Typer(help="Run JobPing ingestion tools.", no_args_is_help=True)


def _asyncio_run[T](awaitable: Awaitable[T]) -> T:
    """Run async CLI work on a psycopg-compatible Windows selector loop."""
    if os.name == "nt":
        return asyncio.run(
            awaitable,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(awaitable)


@app.callback()
def cli_root() -> None:
    """Run JobPing ingestion tools."""


async def _process_commit(
    *,
    owner: str,
    repo: str,
    ref: str | None,
    target_readme: str,
    season: int,
    job_type: JobType,
    redis_url: str,
    github_token: str | None,
) -> PipelineResult:
    """Construct owned clients, process one commit, and release resources."""
    async with GitHubClient(token=github_token) as github:
        async with JobDeduplicator.from_url(redis_url) as deduplicator:
            pipeline = SimplifyPipeline(
                github,
                deduplicator,
                season=season,
                job_type=job_type,
                target_readme_paths={target_readme},
            )
            return await pipeline.process_commit(owner, repo, ref)


async def _process_commits(
    *,
    owner: str,
    repo: str,
    commit_sha: str | None,
    limit: int,
    target_readme: str,
    season: int,
    job_type: JobType,
    redis_url: str,
    github_token: str | None,
    full_sync: bool = False,
    database_url: str | None = None,
) -> tuple[PipelineResult, ...]:
    """Process one explicit commit or a bounded batch of recent commits."""
    async with GitHubClient(token=github_token) as github:
        async with JobDeduplicator.from_url(redis_url) as deduplicator:
            pipeline = SimplifyPipeline(
                github,
                deduplicator,
                season=season,
                job_type=job_type,
                target_readme_paths={target_readme},
            )
            if full_sync:
                results = (
                    await pipeline.process_full_sync(
                        owner, repo, path=target_readme, ref=commit_sha
                    ),
                )
            else:
                if commit_sha:
                    refs = (commit_sha,)
                else:
                    commits = await github.list_commits(owner, repo, per_page=limit)
                    refs = tuple(commit.sha for commit in commits)
                results = tuple([await pipeline.process_commit(owner, repo, ref) for ref in refs])
            if database_url:
                await _persist_results(results, database_url)
            return results


async def _persist_results(results: tuple[PipelineResult, ...], database_url: str) -> None:
    """Persist parsed jobs, including NO_OP rows during cache/database recovery."""
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            repository = DatabaseRepository(session)
            for result in results:
                for state in DeduplicationState:
                    for item in result.categorized(state):
                        await repository.save_job_posting(item.job)
    finally:
        await engine.dispose()


def _summary(result: PipelineResult) -> str:
    counts = " ".join(
        f"{state.value}={len(result.categorized(state))}" for state in DeduplicationState
    )
    return f"Processed commit {result.commit_sha}: {counts} rejected={len(result.rejected)}"


def _job_lines(result: PipelineResult) -> tuple[str, ...]:
    """Return concise company/title lines for parsed, non-no-op changes."""
    return tuple(
        _console_safe(f"  [{item.state.value}] {item.job.company_name} - {item.job.title}")
        for state in (
            DeduplicationState.NEW_ROLE,
            DeduplicationState.ROLE_UPDATED,
            DeduplicationState.ROLE_CLOSED,
            DeduplicationState.NO_OP,
        )
        for item in result.categorized(state)
    )


def _console_safe(value: str) -> str:
    """Make live job summaries printable on legacy Windows terminals."""
    import sys

    encoding = sys.stdout.encoding or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding)


async def _audit_database(database_url: str, stale_hours: float) -> AuditReport:
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            return await DatabaseAuditService(
                session, stale_after=timedelta(hours=stale_hours)
            ).run()
    finally:
        await engine.dispose()


@app.command("audit-db")
def audit_db(
    database_url: Annotated[
        str | None, typer.Option(envvar="DATABASE_URL", help="Async SQLAlchemy database URL.")
    ] = None,
    stale_hours: Annotated[
        float, typer.Option(min=0, help="Age threshold for stale closed statuses.")
    ] = 24
    * 30,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Audit scraper persistence integrity without modifying data."""
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        typer.echo("DATABASE_URL is required.", err=True)
        raise typer.Exit(code=2)
    try:
        report = _asyncio_run(_audit_database(url, stale_hours))
    except Exception as exc:
        typer.echo(f"Database audit failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if json_output:
        typer.echo(json.dumps(report.as_dict(), separators=(",", ":")))
    elif report.healthy:
        typer.echo("Database audit passed: no integrity violations found.")
    else:
        typer.echo("Database audit failed:")
        for finding in report.findings:
            typer.echo(f"  {finding.code}: {finding.count} ({','.join(map(str, finding.ids))})")
    if not report.healthy:
        raise typer.Exit(code=1)


@app.command("run-simplify-parser")
def run_simplify_parser(
    owner: Annotated[
        str, typer.Option(envvar="GITHUB_OWNER", help="GitHub repository owner.")
    ] = "SimplifyJobs",
    repo: Annotated[
        str, typer.Option(envvar="GITHUB_REPO", help="GitHub repository name.")
    ] = "Summer2026-Internships",
    commit_sha: Annotated[
        str | None,
        typer.Option(
            "--commit-sha",
            "--ref",
            envvar="GITHUB_REF",
            help="Specific commit SHA, tag, or branch to process.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(min=1, max=100, help="Recent commits to scan when no SHA is given."),
    ] = 1,
    full_sync: Annotated[
        bool,
        typer.Option(help="Seed from the entire README instead of commit diffs."),
    ] = False,
    target_readme: Annotated[
        str, typer.Option(envvar="TARGET_README", help="Markdown path to inspect.")
    ] = "README.md",
    season: Annotated[
        int, typer.Option(min=2026, max=2027, envvar="JOB_SEASON", help="Hiring season.")
    ] = 2026,
    job_type: Annotated[
        JobType, typer.Option(envvar="JOB_TYPE", help="Job category to assign.")
    ] = JobType.INTERNSHIP,
    redis_url: Annotated[
        str, typer.Option(envvar="REDIS_URL", help="Redis connection URL.")
    ] = "redis://localhost:6379/0",
    github_token: Annotated[
        str | None,
        typer.Option(envvar="GITHUB_TOKEN", help="Optional GitHub API token.", hidden=True),
    ] = None,
    database_url: Annotated[
        str | None,
        typer.Option(envvar="DATABASE_URL", help="Async SQLAlchemy database URL."),
    ] = None,
) -> None:
    """Fetch and classify one or more Simplify repository commits."""
    try:
        results = _asyncio_run(
            _process_commits(
                owner=owner,
                repo=repo,
                commit_sha=commit_sha,
                limit=limit,
                target_readme=target_readme,
                season=season,
                job_type=job_type,
                redis_url=redis_url,
                github_token=github_token,
                full_sync=full_sync,
                database_url=database_url or os.environ.get("DATABASE_URL"),
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        typer.echo("Simplify parser cancelled.", err=True)
        raise typer.Exit(code=130) from None
    except Exception as exc:
        typer.echo(f"Simplify parser failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for result in results:
        typer.echo(_summary(result))
        for line in _job_lines(result):
            typer.echo(line)


async def _serve_scheduler(intervals: list[str]) -> None:
    """Build the configured daemon and wait until interrupted."""
    daemon = SchedulerDaemon()
    for domain, seconds in parse_intervals(intervals).items():

        async def placeholder(target: str = domain) -> None:
            # Source-specific callbacks are registered as their production wiring lands.
            await asyncio.sleep(0)

        daemon.register(PollTarget(domain, seconds, placeholder))
    await daemon.serve()


@app.command("start-scheduler")
def start_scheduler(
    interval: Annotated[
        list[str] | None,
        typer.Option(
            "--interval",
            help="Repeatable DOMAIN=SECONDS polling interval.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Validate and print configuration without starting.",
        ),
    ] = False,
) -> None:
    """Start the UTC asynchronous polling scheduler."""
    try:
        interval = interval or [
            "github.com=60",
            "boards.greenhouse.io=120",
            "api.lever.co=120",
        ]
        parsed = parse_intervals(interval)
        if dry_run:
            for domain, seconds in parsed.items():
                typer.echo(f"{domain}={seconds:g}s")
            return
        _asyncio_run(_serve_scheduler(interval))
    except (KeyboardInterrupt, asyncio.CancelledError):
        typer.echo("Scheduler stopped.")
    except ValueError as exc:
        typer.echo(f"Scheduler configuration failed: {exc}", err=True)
        raise typer.Exit(code=2) from None


def main() -> None:
    """Invoke the Typer application when executed as a module."""
    app()


if __name__ == "__main__":
    main()
