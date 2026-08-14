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
import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.repository import DatabaseRepository
from app.pipelines.simplify_pipeline import PipelineResult, SimplifyPipeline
from app.scheduler import PollTarget, SchedulerDaemon, parse_intervals
from app.schemas.job import JobType
from app.scrapers.github_client import GitHubClient
from app.services.db_audit import AuditReport, DatabaseAuditService
from app.services.deduplicator import DeduplicationState, JobDeduplicator

app = typer.Typer(help="Run JobPing ingestion tools.", no_args_is_help=True)
SIMPLIFY_REPOSITORIES = ("Summer2027-Internships", "New-Grad-Positions")


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
                        owner, repo, path=target_readme, ref=commit_sha or "dev"
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


async def _persist_results(results: tuple[PipelineResult, ...], database_url: str) -> int:
    """Persist parsed jobs, including NO_OP rows during cache/database recovery."""
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            repository = DatabaseRepository(session)
            return await SimplifyPipeline.persist_results(repository, results)
    finally:
        await engine.dispose()


async def _process_full_sync_files(
    *,
    owner: str,
    repo: str,
    ref: str,
    target_readmes: tuple[str, ...],
    season: int,
    job_type: JobType,
    redis_url: str,
    github_token: str | None,
    database_url: str,
) -> tuple[tuple[PipelineResult, ...], int]:
    """Fetch raw Markdown targets, classify them, and bulk-persist one batch."""
    async with GitHubClient(token=github_token) as github:
        async with JobDeduplicator.from_url(redis_url) as deduplicator:
            pipeline = SimplifyPipeline(
                github,
                deduplicator,
                season=season,
                job_type=job_type,
                target_readme_paths=set(target_readmes),
            )
            results = await pipeline.process_full_sync_files(owner, repo, target_readmes, ref=ref)
            persisted = await _persist_results(results, database_url)
            return results, persisted


async def _process_full_sync_repositories(
    *,
    owner: str,
    repos: tuple[str, ...],
    ref: str,
    target_readmes: tuple[str, ...],
    season: int,
    redis_url: str,
    github_token: str | None,
    database_url: str,
) -> tuple[tuple[tuple[str, str, PipelineResult], ...], int]:
    """Fetch both current-cycle repositories and persist one combined batch."""
    async with GitHubClient(token=github_token) as github:
        async with JobDeduplicator.from_url(redis_url) as deduplicator:
            labeled_results: list[tuple[str, str, PipelineResult]] = []
            for repo in repos:
                job_type = JobType.NEW_GRAD if repo == "New-Grad-Positions" else JobType.INTERNSHIP
                pipeline = SimplifyPipeline(
                    github,
                    deduplicator,
                    season=season,
                    job_type=job_type,
                    target_readme_paths=set(target_readmes),
                )
                results = await pipeline.process_full_sync_files(
                    owner, repo, target_readmes, ref=ref
                )
                labeled_results.extend(
                    (repo, target, result)
                    for target, result in zip(target_readmes, results, strict=True)
                )
            persisted = await _persist_results(
                tuple(result for _, _, result in labeled_results), database_url
            )
            return tuple(labeled_results), persisted


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
    ] = "Summer2027-Internships",
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
    ] = 2027,
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


@app.command("run-simplify-full-sync")
def run_simplify_full_sync(
    owner: Annotated[
        str, typer.Option(envvar="GITHUB_OWNER", help="GitHub repository owner.")
    ] = "SimplifyJobs",
    repo: Annotated[
        list[str] | None,
        typer.Option(
            "--repo",
            help="Repeat for each repository; defaults to current intern and new-grad cycles.",
        ),
    ] = None,
    ref: Annotated[
        str,
        typer.Option(
            "--ref",
            envvar="SIMPLIFY_FULL_SYNC_REF",
            help="Branch containing current files.",
        ),
    ] = "dev",
    target_readme: Annotated[
        list[str] | None,
        typer.Option(
            "--target-readme",
            help="Repeat for every Markdown file to seed (for example README-Off-Season.md).",
        ),
    ] = None,
    season: Annotated[
        int, typer.Option(min=2026, max=2027, envvar="JOB_SEASON", help="Hiring season.")
    ] = 2027,
    redis_url: Annotated[
        str, typer.Option(envvar="REDIS_URL", help="Redis connection URL.")
    ] = "redis://localhost:6379/0",
    github_token: Annotated[
        str | None,
        typer.Option(envvar="GITHUB_TOKEN", help="Optional GitHub API token.", hidden=True),
    ] = None,
    database_url: Annotated[
        str | None, typer.Option(envvar="DATABASE_URL", help="Async SQLAlchemy database URL.")
    ] = None,
) -> None:
    """Seed PostgreSQL from complete current Simplify Markdown files."""
    resolved_database_url = database_url or os.environ.get("DATABASE_URL")
    if not resolved_database_url:
        typer.echo("DATABASE_URL is required for a full sync.", err=True)
        raise typer.Exit(code=2)
    env_targets = os.environ.get("TARGET_READMES") or os.environ.get("TARGET_README", "README.md")
    targets = tuple(
        target_readme or [item.strip() for item in env_targets.split(",") if item.strip()]
    )
    env_repositories = os.environ.get("SIMPLIFY_REPOSITORIES") or os.environ.get("GITHUB_REPO")
    repositories = tuple(
        repo
        or (
            [item.strip() for item in env_repositories.split(",") if item.strip()]
            if env_repositories
            else SIMPLIFY_REPOSITORIES
        )
    )
    typer.echo(
        f"Starting full sync from {owner}/{{{', '.join(repositories)}}}@{ref}: "
        f"{', '.join(_console_safe(path) for path in targets)}"
    )
    try:
        labeled_results, persisted = _asyncio_run(
            _process_full_sync_repositories(
                owner=owner,
                repos=repositories,
                ref=ref,
                target_readmes=targets,
                season=season,
                redis_url=redis_url,
                github_token=github_token,
                database_url=resolved_database_url,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        typer.echo("Simplify full sync cancelled.", err=True)
        raise typer.Exit(code=130) from None
    except Exception as exc:
        typer.echo(f"Simplify full sync failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for repository, target, result in labeled_results:
        typer.echo(f"{repository}/{_console_safe(target)}: {_summary(result)}")
    parsed = sum(
        len(result.categorized(state))
        for _, _, result in labeled_results
        for state in DeduplicationState
    )
    typer.echo(f"Bulk upsert complete: parsed={parsed} persisted={persisted}")


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


@app.command("run-server")
def run_server(
    host: Annotated[
        str,
        typer.Option(envvar="API_HOST", help="Interface on which the API server listens."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(min=1, max=65535, envvar="API_PORT", help="API server TCP port."),
    ] = 8000,
    reload: Annotated[
        bool,
        typer.Option("--reload/--no-reload", help="Reload the server when source files change."),
    ] = False,
    log_level: Annotated[
        str,
        typer.Option(
            envvar="API_LOG_LEVEL",
            help="Uvicorn log level (critical, error, warning, info, debug, or trace).",
        ),
    ] = "info",
) -> None:
    """Run the JobPing FastAPI application with Uvicorn."""
    normalized_log_level = log_level.casefold()
    allowed_log_levels = {"critical", "error", "warning", "info", "debug", "trace"}
    if normalized_log_level not in allowed_log_levels:
        typer.echo(f"Invalid log level: {log_level}", err=True)
        raise typer.Exit(code=2)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level=normalized_log_level,
    )


def main() -> None:
    """Invoke the Typer application when executed as a module."""
    app()


if __name__ == "__main__":
    main()
