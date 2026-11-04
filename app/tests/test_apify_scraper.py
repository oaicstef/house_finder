"""Tests del adaptador de scraping Apify (cliente falso, sin red)."""

from __future__ import annotations

from typing import Any, Iterable

import pytest

from app.domain.exceptions import ScraperException
from app.domain.models import Portal, SearchCriteria
from app.infrastructure.scrapers.apify_scraper import ApifyListingScraper, _to_float
from app.tests.conftest import RecordingLogger


class _FakeDataset:
    def __init__(self, items: Iterable[dict[str, Any]]) -> None:
        self._items = list(items)

    def iterate_items(self) -> Iterable[dict[str, Any]]:
        return iter(self._items)


class _FakeActorHandle:
    def __init__(self, client: "_FakeClient", actor_id: str) -> None:
        self._client = client
        self._actor_id = actor_id

    def call(self, run_input: dict[str, Any] | None = None, timeout_secs: int | None = None) -> Any:
        self._client.calls.append(
            {"actor_id": self._actor_id, "run_input": run_input, "timeout_secs": timeout_secs}
        )
        if self._client.error is not None:
            raise self._client.error
        return self._client.run_result


class _FakeClient:
    def __init__(
        self,
        items: Iterable[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        run_result: Any = None,
    ) -> None:
        self.items = list(items or [])
        self.error = error
        self.run_result = run_result if run_result is not None else {"defaultDatasetId": "ds-1"}
        self.calls: list[dict[str, Any]] = []

    def actor(self, actor_id: str) -> _FakeActorHandle:
        return _FakeActorHandle(self, actor_id)

    def dataset(self, dataset_id: str) -> _FakeDataset:
        return _FakeDataset(self.items)


RAW_ITEM = {
    "url": "https://www.idealista.com/inmueble/123/",
    "title": "Piso en Chamberí",
    "price": "1.250 €",
    "size": "80 m2",
    "rooms": "2",
    "bathrooms": 1,
    "location": "Chamberí, Madrid",
    "description": "Exterior con ascensor",
}


def build_scraper(client: _FakeClient, **overrides: Any) -> ApifyListingScraper:
    params: dict[str, Any] = {
        "token": "test-token",
        "actor_ids": {Portal.IDEALISTA: "actor-idealista"},
        "max_items": 40,
        "timeout_seconds": 120,
        "operation": "rent",
        "client_factory": lambda token: client,
        "logger": RecordingLogger(),
    }
    params.update(overrides)
    return ApifyListingScraper(**params)


def idealista_criteria(**overrides: Any) -> SearchCriteria:
    base: dict[str, Any] = {"location": "Madrid", "portals": (Portal.IDEALISTA,)}
    base.update(overrides)
    return SearchCriteria(**base)


async def test_normalizes_raw_item_to_domain_listing() -> None:
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client)

    listings = await scraper.fetch_listings(idealista_criteria())

    assert len(listings) == 1
    listing = listings[0]
    assert listing.portal is Portal.IDEALISTA
    assert listing.url == RAW_ITEM["url"]
    assert listing.price_eur == 1250.0
    assert listing.size_m2 == 80.0
    assert listing.rooms == 2
    assert listing.price_per_m2 == 15.63


async def test_builds_run_input_from_criteria() -> None:
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client)

    await scraper.fetch_listings(idealista_criteria(max_price_eur=1400.0, min_rooms=2))

    run_input = client.calls[0]["run_input"]
    assert run_input["location"] == "Madrid"
    assert run_input["operation"] == "rent"
    assert run_input["maxPrice"] == 1400.0
    assert run_input["minRooms"] == 2
    assert client.calls[0]["timeout_secs"] == 120


async def test_filters_items_that_do_not_meet_hard_criteria() -> None:
    client = _FakeClient([RAW_ITEM])

    listings = await build_scraper(client).fetch_listings(idealista_criteria(max_price_eur=1000.0))

    assert listings == []


async def test_skips_items_without_url_or_price() -> None:
    client = _FakeClient([{"title": "sin url", "price": 900}, {"url": "https://x.test/1"}])

    assert await build_scraper(client).fetch_listings(idealista_criteria()) == []


async def test_deduplicates_urls_across_items() -> None:
    client = _FakeClient([RAW_ITEM, dict(RAW_ITEM)])

    listings = await build_scraper(client).fetch_listings(idealista_criteria())

    assert len(listings) == 1


async def test_raises_scraper_exception_when_actor_fails() -> None:
    client = _FakeClient([], error=RuntimeError("boom"))
    scraper = build_scraper(client)

    with pytest.raises(ScraperException) as exc_info:
        await scraper.fetch_listings(idealista_criteria())

    assert exc_info.value.code == "SCRAPER_FAILURE"
    assert exc_info.value.details["portal"] == "idealista"


async def test_raises_scraper_exception_when_actor_is_not_configured() -> None:
    scraper = build_scraper(_FakeClient([RAW_ITEM]), actor_ids={})

    with pytest.raises(ScraperException):
        await scraper.fetch_listings(idealista_criteria())


async def test_raises_scraper_exception_when_run_has_no_dataset() -> None:
    scraper = build_scraper(_FakeClient([RAW_ITEM], run_result={}))

    with pytest.raises(ScraperException):
        await scraper.fetch_listings(idealista_criteria())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.250,50 €", 1250.5),
        ("1,250.50", 1250.5),
        ("1,250", 1250.0),
        ("85,5 m²", 85.5),
        ("1200", 1200.0),
        (1200, 1200.0),
        ("", None),
        (None, None),
        ("sin precio", None),
    ],
)
def test_to_float_handles_spanish_and_english_formats(raw: Any, expected: float | None) -> None:
    assert _to_float(raw) == expected
