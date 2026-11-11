"""Tests de la fuente HTTP de Fotocasa y del núcleo de scraping (sin red)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.domain.exceptions import ScraperException
from app.domain.models import Listing, Portal, SearchCriteria
from app.infrastructure.scrapers.listing_scraper import ListingScraper
from app.infrastructure.scrapers.sources.fotocasa import (
    FotocasaHttpSource,
    location_segment,
)
from app.infrastructure.scrapers.sources.http import HttpClient
from app.tests.conftest import RecordingLogger

FIXTURE = Path(__file__).parent / "fixtures" / "fotocasa" / "search.html"


def criteria(**overrides: Any) -> SearchCriteria:
    base: dict[str, Any] = {"location": "Madrid", "portals": (Portal.FOTOCASA,)}
    base.update(overrides)
    return SearchCriteria(**base)


def make_client(handler, *, max_retries: int = 0) -> HttpClient:
    """Cliente con transporte falso: ni red ni esperas reales."""
    return HttpClient(
        max_retries=max_retries,
        backoff_seconds=0.0,
        rate_limit_per_minute=0,
        transport=httpx.MockTransport(handler),
        logger=RecordingLogger(),
    )


def page_with(items: list[dict[str, Any]]) -> str:
    payload = {"initialSearch": {"result": {"count": len(items), "realEstates": items}}}
    return (
        '<html><body><script type="application/json" id="__initial_props__">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def fixture_items() -> list[dict[str, Any]]:
    html = FIXTURE.read_text()
    start = html.index('id="__initial_props__"')
    body = html[html.index(">", start) + 1 : html.index("</script>", start)]
    return json.loads(body)["initialSearch"]["result"]["realEstates"]


def test_location_segment_maps_cities_and_uses_the_last_level() -> None:
    assert location_segment("Madrid") == "madrid-capital"
    assert location_segment("Ventas, Ciudad Lineal, Madrid") == "madrid-capital"
    assert location_segment("Málaga") == "malaga-capital"
    assert location_segment("Tarragona") == "tarragona"
    assert location_segment("Madrid", override="Alcobendas") == "alcobendas"


async def test_builds_the_search_url_with_the_portal_filters() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, text=FIXTURE.read_text())

    source = FotocasaHttpSource(client=make_client(handler), operation="sale", max_items=1)
    await source.fetch(
        criteria(
            location="Ventas, Ciudad Lineal, Madrid",
            max_price_eur=900000,
            min_size_m2=140,
            min_rooms=4,
        )
    )

    assert "/es/comprar/viviendas/madrid-capital/todas-las-zonas/l" in seen["url"]
    assert "maxPrice=900000" in seen["url"]
    assert "minSurface=140" in seen["url"]
    assert "minRooms=4" in seen["url"]
    assert seen["headers"]["user-agent"].startswith("Mozilla/5.0")


async def test_rent_uses_the_alquiler_path() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text=FIXTURE.read_text())

    source = FotocasaHttpSource(client=make_client(handler), operation="rent")
    await source.fetch(criteria())

    assert "/es/alquiler/viviendas/madrid-capital/todas-las-zonas/l" in seen["url"]


async def test_maps_a_real_item_to_the_raw_schema() -> None:
    source = FotocasaHttpSource(client=make_client(lambda request: httpx.Response(200, text=FIXTURE.read_text())))

    items = await source.fetch(criteria())

    assert len(items) == 3
    first = items[0]
    assert first["url"].startswith("https://www.fotocasa.es/es/comprar/vivienda/")
    assert first["price"] == 7490000
    assert first["transaction"]["price"] == 7490000
    assert first["surface"] == 413
    assert first["rooms"] == 3
    assert first["baths"] == 4
    assert first["antiquity"] == 9
    assert first["latitude"] == pytest.approx(40.43078083786225)
    assert first["longitude"] == pytest.approx(-3.6866740825020803)
    assert first["multimedia"]
    assert first["location"]["level7Name"] == "Barrio de Salamanca"


async def test_the_core_normalizes_fotocasa_items_into_domain_listings() -> None:
    source = FotocasaHttpSource(client=make_client(lambda request: httpx.Response(200, text=FIXTURE.read_text())))
    scraper = ListingScraper(sources={Portal.FOTOCASA: source}, logger=RecordingLogger())

    listings = await scraper.fetch_listings(criteria())

    assert len(listings) == 3
    listing = listings[0]
    assert listing.portal is Portal.FOTOCASA
    assert listing.price_eur == 7490000.0
    assert listing.size_m2 == 413.0
    assert listing.rooms == 3
    assert listing.bathrooms == 4
    assert listing.location.startswith("Castellana")
    assert listing.title
    assert listing.photos and listing.photos[0].startswith("https://")
    assert listing.year_built == date.today().year - 9
    assert listing.home_type == "flat"
    assert listing.price_per_m2 is not None


async def test_passes_explicit_new_development_status_through_to_the_listing() -> None:
    item = fixture_items()[0]
    item["newDevelopment"] = True
    source = FotocasaHttpSource(
        client=make_client(lambda request: httpx.Response(200, text=page_with([item])))
    )
    scraper = ListingScraper(sources={Portal.FOTOCASA: source}, logger=RecordingLogger())

    listings = await scraper.fetch_listings(criteria())

    assert len(listings) == 1
    assert listings[0].listing_condition == "new"


async def test_paginates_until_max_items() -> None:
    first_page = fixture_items()
    second_page = []
    for item in first_page:
        clone = json.loads(json.dumps(item))
        clone["id"] = clone["id"] + 1000
        second_page.append(clone)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        requests.append(page or "")
        return httpx.Response(200, text=page_with(first_page if page == "1" else second_page))

    source = FotocasaHttpSource(client=make_client(handler), max_items=4)
    items = await source.fetch(criteria())

    assert len(items) == 4
    assert requests == ["1", "2"]


async def test_stops_when_a_page_repeats_the_same_listings() -> None:
    html = FIXTURE.read_text()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=html)

    source = FotocasaHttpSource(client=make_client(handler), max_items=40, max_pages=5)
    items = await source.fetch(criteria())

    assert len(items) == 3
    assert len(calls) == 2


async def test_retries_a_temporary_block_and_succeeds() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, text=FIXTURE.read_text())

    source = FotocasaHttpSource(client=make_client(handler, max_retries=1), max_items=3)

    items = await source.fetch(criteria())

    assert len(attempts) == 2
    assert len(items) == 3


async def test_a_hard_block_is_reported_as_scraper_exception() -> None:
    source = FotocasaHttpSource(client=make_client(lambda request: httpx.Response(403, text="blocked")))

    with pytest.raises(ScraperException) as exc_info:
        await source.fetch(criteria())

    assert exc_info.value.details["status_code"] == 403


async def test_a_page_without_the_search_payload_is_reported() -> None:
    source = FotocasaHttpSource(client=make_client(lambda request: httpx.Response(200, text="<html></html>")))

    with pytest.raises(ScraperException) as exc_info:
        await source.fetch(criteria())

    assert exc_info.value.details["reason"] == "missing_initial_props"


class _FakeScraper:
    """``ScraperPort`` mínimo para probar el respaldo."""

    def __init__(self, listings: list[Listing] | None = None, error: Exception | None = None) -> None:
        self.listings = list(listings or [])
        self.error = error
        self.calls: list[SearchCriteria] = []

    async def fetch_listings(self, criteria: SearchCriteria) -> list[Listing]:
        self.calls.append(criteria)
        if self.error is not None:
            raise self.error
        return list(self.listings)


async def test_falls_back_to_the_delegate_when_the_local_source_fails() -> None:
    logger = RecordingLogger()
    fallback = _FakeScraper(
        [
            Listing(
                portal=Portal.IDEALISTA,
                url="https://www.idealista.com/inmueble/1/",
                title="Chalet",
                price_eur=500000.0,
                location="Madrid",
            )
        ]
    )
    broken = FotocasaHttpSource(client=make_client(lambda request: httpx.Response(403, text="blocked")))
    scraper = ListingScraper(
        sources={Portal.FOTOCASA: broken},
        delegates={Portal.FOTOCASA: fallback},
        logger=logger,
    )

    listings = await scraper.fetch_listings(criteria())

    assert [listing.url for listing in listings] == ["https://www.idealista.com/inmueble/1/"]
    assert fallback.calls and fallback.calls[0].portals == (Portal.FOTOCASA,)
    event = logger.find("scraper_fallback_used")
    assert event is not None and event["reason"] == "error"


async def test_empty_local_results_fall_back_only_when_enabled() -> None:
    empty_html = page_with([])
    fallback = _FakeScraper(
        [
            Listing(
                portal=Portal.FOTOCASA,
                url="https://www.fotocasa.es/es/comprar/vivienda/madrid/1/d",
                title="Piso",
                price_eur=300000.0,
                location="Madrid",
            )
        ]
    )
    source = FotocasaHttpSource(client=make_client(lambda request: httpx.Response(200, text=empty_html)))

    without_fallback = ListingScraper(sources={Portal.FOTOCASA: source}, logger=RecordingLogger())
    assert await without_fallback.fetch_listings(criteria()) == []

    with_fallback = ListingScraper(
        sources={Portal.FOTOCASA: source},
        delegates={Portal.FOTOCASA: fallback},
        fallback_on_empty=True,
        logger=RecordingLogger(),
    )
    assert len(await with_fallback.fetch_listings(criteria())) == 1


async def test_delegated_portals_do_not_run_local_filters_twice() -> None:
    """Idealista delegado se añade tal cual (ya viene filtrado y con detalles)."""
    delegate = _FakeScraper(
        [
            Listing(
                portal=Portal.IDEALISTA,
                url="https://www.idealista.com/inmueble/9/",
                title="Chalet",
                price_eur=800000.0,
                location="Madrid",
            )
        ]
    )
    logger = RecordingLogger()
    scraper = ListingScraper(delegates={Portal.IDEALISTA: delegate}, logger=logger)

    listings = await scraper.fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,), max_price_eur=1.0)
    )

    assert len(listings) == 1
    assert delegate.calls[0].portals == (Portal.IDEALISTA,)
    assert logger.find("scraper_fallback_used") is not None
