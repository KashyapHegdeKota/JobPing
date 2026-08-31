from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.applications.service import ApplicationService
from app.schemas.application import ApplicationForm


class FakePage:
    url = "https://boards.greenhouse.io/acme/jobs/1"

    def __init__(self) -> None:
        self.visited: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def goto(self, url: str, **kwargs: object) -> None:
        self.visited.append((url, kwargs))

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.page = FakePage()
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeBrowser:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.exited = True

    async def new_page(self) -> FakePage:
        return self.page


class Repo:
    def __init__(self, posting: SimpleNamespace | None) -> None:
        self.posting = posting

    async def get_job_by_id(self, job_id: int) -> SimpleNamespace | None:
        assert job_id == 1
        return self.posting


def job(
    url: str = "https://boards.greenhouse.io/acme/jobs/1", *, closed: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        title="Intern",
        apply_url=url,
        is_closed=closed,
        company=SimpleNamespace(name="Acme"),
    )


@pytest.mark.asyncio
async def test_service_success_navigates_and_returns_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser()

    async def inspect(self: object, page: FakePage, *, job_id: int) -> ApplicationForm:
        del self
        return ApplicationForm(ats="greenhouse", job_id=job_id, url=page.url, fields=[])

    monkeypatch.setattr("app.applications.service.GreenhouseApplicant.inspect", inspect)
    result = await ApplicationService(Repo(job()), browser).inspect_job(1)  # type: ignore[arg-type]

    assert result.company == "Acme"
    assert result.role == "Intern"
    assert browser.entered is True
    assert browser.exited is True
    assert browser.page.closed is True
    assert browser.page.visited == [
        (
            "https://boards.greenhouse.io/acme/jobs/1",
            {"wait_until": "domcontentloaded"},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("posting", "message"),
    [
        (None, "not found"),
        (job(closed=True), "closed"),
        (job(url="ftp://bad"), "valid application URL"),
        (job(url="https://jobs.lever.co/acme"), "Unsupported ATS"),
    ],
)
async def test_service_error_paths(posting: SimpleNamespace | None, message: str) -> None:
    browser = FakeBrowser()
    with pytest.raises(ValueError, match=message):
        await ApplicationService(Repo(posting), browser).inspect_job(1)  # type: ignore[arg-type]
    assert browser.entered is False
