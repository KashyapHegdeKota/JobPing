"""Optional public pay reads are scoped to the requested ATS requisition."""

import asyncio

import httpx
import pytest
from app.scrapers.greenhouse import GreenhouseScraper


@pytest.mark.parametrize(
    "response",
    [
        {
            "id": 1,
            "pay_input_ranges": [{"min_cents": 4000, "currency_type": "USD"}],
            "first_published": "2026-09-10T16:09:47-04:00",
        },
        {
            "id": 2,
            "pay_input_ranges": [{"min_cents": 999900, "currency_type": "USD"}],
            "first_published": "2026-09-10T16:09:47-04:00",
        },
        None,
    ],
)
async def test_optional_pay_is_scoped_and_failure_preserves_job(response: dict | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/acme/jobs/1")
        assert request.url.params["pay_transparency"] == "true"
        return httpx.Response(200, json=response) if response else httpx.Response(503)

    row = GreenhouseScraper._map_job(
        "acme",
        {
            "id": 1,
            "title": "Intern 2027",
            "absolute_url": "https://example.com/jobs/1",
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scraper = GreenhouseScraper(company="acme", client=client)
        enriched = await scraper.enrich_pay(row)
        if response and response["id"] == 1:
            assert enriched.payload["pay_input_ranges"][0]["min_cents"] == 4000
            assert enriched.payload["posted"] == "2026-09-10T16:09:47-04:00"
        else:
            assert enriched is row
        await scraper.aclose()
        assert not client.is_closed


def test_greenhouse_board_retains_publication_date_without_using_update_date() -> None:
    item = {
        "id": 1,
        "title": "Intern 2027",
        "absolute_url": "https://example.com/jobs/1",
        "updated_at": "2026-10-09T16:00:00Z",
    }
    assert "posted" not in GreenhouseScraper._map_job("acme", item).payload
    item["first_published"] = "2026-09-10T16:09:47-04:00"
    assert GreenhouseScraper._map_job("acme", item).payload["posted"] == item["first_published"]


async def test_optional_pay_cancellation_propagates() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    row = GreenhouseScraper._map_job(
        "acme",
        {
            "id": 1,
            "title": "Intern 2027",
            "absolute_url": "https://example.com/jobs/1",
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await GreenhouseScraper(company="acme", client=client).enrich_pay(row)
