"""Network-isolated tests of BYOK planning, polling, persistence and CLI behavior."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from app import cli
from app.trackers import service
from app.trackers.cli import run_checks
from app.trackers.models import Check, Plan, Provider, Tracker
from app.trackers.service import TrackerError, ask_model, check_tracker, create_tracker, fetch_page
from app.trackers.store import TrackerStore
from pydantic import ValidationError
from pytest import MonkeyPatch
from typer.testing import CliRunner

URL = "https://example.com/jobs"
JOB = {
    "url": "https://example.com/jobs/1",
    "title": "Intern",
    "company": "Acme",
    "status": "open",
    "facts": {"location": "Remote"},
}
PAGE = '<h1>Acme jobs</h1><a href="/jobs/1">Intern Remote</a><a href="/jobs/2">Intern NY</a>'
REAL_VALIDATE_PUBLIC_URL = service.validate_public_url


@pytest.fixture(autouse=True)
def environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("JOBPING_TRACKER_API_KEY", "test-private-key")

    async def public(url: str) -> None:
        assert url.startswith("https://example.com")

    monkeypatch.setattr(service, "validate_public_url", public)


def tracker() -> Tracker:
    return Tracker(
        url=URL,
        scope="company",
        request="Watch remote internships",
        provider=Provider(model="test-model"),
        plan=Plan(name="Remote interns", instructions="Only remote", fields=["location"]),
    )


def completion(value: object, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": finish, "message": {"content": json.dumps(value)}}]},
    )


def transport(values: list[object], requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        assert request.headers["authorization"] == "Bearer test-private-key"
        return completion(values.pop(0))

    return httpx.MockTransport(handler)


async def test_plan_baseline_updates_missing_and_reappearance(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    changed = {**JOB, "facts": {"location": "NY"}}
    same_title_other_role = {**JOB, "url": "https://example.com/jobs/2"}
    values = [tracker().plan.model_dump()] + [
        {"complete": True, "jobs": jobs}
        for jobs in [[JOB], [JOB], [changed, same_title_other_role], [], [JOB]]
    ]
    async with httpx.AsyncClient(transport=transport(values, requests)) as client:
        state = await create_tracker(
            client,
            url=URL,
            scope="company",
            request="Remote internships",
            provider=Provider(model="test-model"),
            interval_seconds=60,
        )
        repository = TrackerStore(tmp_path)
        for expected in [[], [], ["updated", "added"], ["missing", "missing"], ["added"]]:
            result = await check_tracker(client, state)
            assert [change.kind for change in result.changes] == expected
            assert result.baseline == (not state.history)
            state.history.append(result)
            with repository.locked(state.id):
                repository.save(state)
            state = repository.load(state.id)
        assert not client.is_closed
    assert "test-private-key" not in repository.path(state.id).read_text()
    payload = json.loads(requests[1].content)
    assert payload["model"] == "test-model"
    assert "Remote internships" in payload["messages"][1]["content"]
    assert "max_completion_tokens" in payload


@pytest.mark.parametrize(
    "observation",
    [
        {"complete": False, "jobs": []},
        {"complete": True, "jobs": [JOB, JOB]},
        {"complete": True, "jobs": [{**JOB, "url": "https://invented.com/job"}]},
        {"complete": True, "jobs": [{**JOB, "facts": {"salary": "100"}}]},
        {"complete": True, "jobs": [{**JOB, "status": "probably closed"}]},
        {"complete": True, "jobs": [JOB], "execute": "malicious code"},
    ],
)
async def test_invalid_extraction_does_not_mutate_state(observation: object) -> None:
    state = tracker()
    original = state.model_dump_json()
    async with httpx.AsyncClient(transport=transport([observation], [])) as client:
        with pytest.raises(TrackerError):
            await check_tracker(client, state)
    assert state.model_dump_json() == original


async def test_posting_scope_rejects_multiple_jobs() -> None:
    state = tracker()
    state.scope = "posting"
    observation = {"complete": True, "jobs": [JOB, {**JOB, "url": URL}]}
    async with httpx.AsyncClient(transport=transport([observation], [])) as client:
        with pytest.raises(TrackerError, match="more than one"):
            await check_tracker(client, state)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
async def test_provider_errors_are_sanitized(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="test-private-key", headers={"location": URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TrackerError) as error:
            await ask_model(client, tracker().provider, Plan, "plan", {})
    assert str(status) in str(error.value)
    assert "test-private-key" not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not JSON test-private-key"),
        httpx.Response(200, json={"choices": []}),
        completion({}, "length"),
        completion({"name": "Bad", "instructions": "Bad", "fields": ["x", "x"]}),
    ],
)
async def test_invalid_provider_response(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(TrackerError) as error:
            await ask_model(client, tracker().provider, Plan, "plan", {})
    assert "test-private-key" not in str(error.value)


async def test_missing_key_fails_before_network(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("JOBPING_TRACKER_API_KEY")
    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=transport([], requests)) as client:
        with pytest.raises(TrackerError, match="environment variable"):
            await check_tracker(client, tracker())
    assert not requests


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(429),
        httpx.Response(302, headers={"location": "https://[invalid"}),
        httpx.Response(200, text="{}", headers={"content-type": "application/json"}),
        httpx.Response(200, text="<script>hidden</script>", headers={"content-type": "text/html"}),
        httpx.Response(
            200, text="x" * (service.MAX_TEXT + 1), headers={"content-type": "text/html"}
        ),
        httpx.Response(
            200, text="x" * (service.MAX_BYTES + 1), headers={"content-type": "text/html"}
        ),
    ],
)
async def test_source_failures(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(TrackerError):
            await fetch_page(client, URL)


async def test_redirect_is_validated_and_scripts_excluded(monkeypatch: MonkeyPatch) -> None:
    checked = []

    async def validate(url: str) -> None:
        checked.append(url)

    monkeypatch.setattr(service, "validate_public_url", validate)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs":
            return httpx.Response(302, headers={"location": "/careers"})
        return httpx.Response(
            200,
            text='<script>evil</script><a href="/1">Job</a>',
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text, links = await fetch_page(client, URL)
    assert checked == [URL, "https://example.com/careers"]
    assert "evil" not in text and "Job" in text
    assert "https://example.com/1" in links


def test_store_lock_path_traversal_and_failed_atomic_write(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store = TrackerStore(tmp_path)
    state = tracker()
    store.save(state)
    original = store.path(state.id).read_bytes()
    with store.locked(state.id):
        with pytest.raises(TrackerError, match="busy"):
            with store.locked(state.id):
                pass
    assert not list(tmp_path.glob("*.lock"))
    with pytest.raises(TrackerError):
        store.load("../secrets")

    def fail_replace(self: Path, target: Path) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        store.save(state)
    assert store.path(state.id).read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


async def test_watch_retries_preserves_snapshot_and_stops(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from app.trackers import cli as tracker_cli

    state = tracker()
    store = TrackerStore(tmp_path)
    store.save(state)
    count = 0
    sleeps = []

    async def check(client: httpx.AsyncClient, tracker: Tracker) -> Check:
        nonlocal count
        count += 1
        if count == 1:
            raise TrackerError("incomplete")
        return Check(checked_at="test", baseline=True, jobs=[], changes=[])

    async def sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(tracker_cli, "check_tracker", check)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await run_checks(state.id, tmp_path, True, 2)
    assert count == 2 and sleeps == [3600]
    assert len(store.load(state.id).history) == 1


async def test_cancellation_releases_lock_and_client(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from app.trackers import cli as tracker_cli

    state = tracker()
    store = TrackerStore(tmp_path)
    store.save(state)
    clients = []

    async def cancel(client: httpx.AsyncClient, tracker: Tracker) -> Check:
        clients.append(client)
        raise asyncio.CancelledError

    monkeypatch.setattr(tracker_cli, "check_tracker", cancel)
    with pytest.raises(asyncio.CancelledError):
        await run_checks(state.id, tmp_path, True, 0)
    assert clients[0].is_closed
    assert not list(tmp_path.glob("*.lock"))
    assert not store.load(state.id).history


def test_cli_validation_list_and_show(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.delenv("JOBPING_TRACKER_API_KEY")
    args = ["trackers", "create", URL, "--request", "Remote", "--model", "test"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1 and "JOBPING_TRACKER_API_KEY" in result.output
    assert runner.invoke(cli.app, args + ["--interval", "1"]).exit_code == 2
    assert runner.invoke(cli.app, args + ["--scope", "invalid"]).exit_code == 2
    store = TrackerStore(tmp_path)
    state = tracker()
    store.save(state)
    listed = runner.invoke(cli.app, ["trackers", "list", "--store", str(tmp_path)])
    assert listed.exit_code == 0 and state.id in listed.output
    shown = runner.invoke(cli.app, ["trackers", "show", state.id, "--store", str(tmp_path)])
    assert shown.exit_code == 0 and json.loads(shown.output)["plan"]["fields"] == ["location"]


@pytest.mark.parametrize(
    "url", ["http://provider.com/v1", "https://key@provider.com", "https://provider.com?key=x"]
)
def test_provider_rejects_insecure_settings(url: str) -> None:
    with pytest.raises(ValidationError):
        Provider(base_url=url, model="test")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
async def test_source_rejects_private_addresses(address: str, monkeypatch: MonkeyPatch) -> None:
    async def resolve(*args: object, **kwargs: object) -> list[tuple]:
        return [(2, 1, 6, "", (address, 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(TrackerError, match="public"):
        await REAL_VALIDATE_PUBLIC_URL(URL)


async def test_provider_timeout_is_safe() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test-private-key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(TrackerError, match="AI request failed"):
            await ask_model(client, tracker().provider, Plan, "plan", {})


async def test_custom_byok_endpoint_and_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PROVIDER_KEY", "another-private-key")
    provider = Provider(
        base_url="https://provider.example/api/v1", model="custom-model", key_env="MY_PROVIDER_KEY"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://provider.example/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer another-private-key"
        assert json.loads(request.content)["model"] == "custom-model"
        return completion(tracker().plan.model_dump())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plan = await ask_model(client, provider, Plan, "plan", {})
    assert plan == tracker().plan


async def test_provider_key_echo_is_discarded() -> None:
    value = {**tracker().plan.model_dump(), "name": "test-private-key"}
    async with httpx.AsyncClient(transport=transport([value], [])) as client:
        with pytest.raises(TrackerError, match="credential material"):
            await ask_model(client, tracker().provider, Plan, "plan", {})


def test_malformed_page_links_do_not_break_extraction() -> None:
    parser = service.PageText(URL)
    parser.feed('<a href="https://[invalid">Bad link</a><p>Internship available</p>')
    assert "Internship available" in parser.parts
    assert parser.links == {URL}


def test_cli_create_run_and_persist_end_to_end(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    values = [
        tracker().plan.model_dump(),
        {"complete": True, "jobs": [JOB]},
        {"complete": True, "jobs": [{**JOB, "status": "closed"}]},
    ]
    original_client = httpx.AsyncClient
    mock_transport = transport(values, requests)
    clients = []

    def client(**kwargs: object) -> httpx.AsyncClient:
        instance = original_client(transport=mock_transport, **kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", client)
    runner = CliRunner()
    created = runner.invoke(
        cli.app,
        [
            "trackers",
            "create",
            URL,
            "--request",
            "Watch remote",
            "--scope",
            "company",
            "--model",
            "test-model",
            "--store",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0, created.output
    state = json.loads(created.output)
    checked = runner.invoke(cli.app, ["trackers", "run", state["id"], "--store", str(tmp_path)])
    assert checked.exit_code == 0, checked.output
    change = json.loads(checked.output)["changes"][0]
    assert change["kind"] == "updated" and change["after"]["status"] == "closed"
    assert len(TrackerStore(tmp_path).load(state["id"]).history) == 2
    assert all(instance.is_closed for instance in clients)
    assert "test-private-key" not in created.output + checked.output


def test_failed_creation_leaves_no_tracker(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    original_client = httpx.AsyncClient
    values = [tracker().plan.model_dump(), {"complete": False, "jobs": []}]

    def client(**kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=transport(values, []), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = CliRunner().invoke(
        cli.app,
        [
            "trackers",
            "create",
            URL,
            "--request",
            "Watch",
            "--model",
            "test",
            "--store",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1 and "incomplete" in result.output
    assert not list(tmp_path.glob("*.json"))
