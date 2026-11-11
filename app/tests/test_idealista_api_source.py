"""Tests del proveedor oficial de Idealista (sin llamadas de red reales)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.domain.exceptions import ScraperException
from app.domain.models import Portal, SearchCriteria
from app.infrastructure.scrapers.sources.http import HttpClient
from app.infrastructure.scrapers.sources.idealista_api import (
    SEARCH_URL,
    TOKEN_URL,
    IdealistaApiSource,
)
from app.tests.conftest import RecordingLogger


def item(code: str = "123") -> dict[str, Any]:
    return {
        "propertyCode": code,
        "url": f"https://www.idealista.com/inmueble/{code}/",
        "price": 500000,
        "size": 140,
        "rooms": 4,
        "bathrooms": 2,
        "propertyType": "flat",
        "propertySubtype": "flat",
        "address": "Calle Mayor",
        "municipality": "Madrid",
        "province": "Madrid",
        "description": "Piso luminoso con terraza",
        "latitude": 40.4168,
        "longitude": -3.7038,
        "thumbnail": "https://img.example/1.jpg",
        "energyCertification": "B",
        "yearBuilt": 2022,
    }


def criteria(**overrides: Any) -> SearchCriteria:
    base: dict[str, Any] = {
        "location": "Madrid",
        "portals": (Portal.IDEALISTA,),
        "latitude": 40.4168,
        "longitude": -3.7038,
        "radius_km": 5,
    }
    base.update(overrides)
    return SearchCriteria(**base)


def make_source(handler, *, max_items: int = 40, max_pages: int = 5) -> IdealistaApiSource:
    client = HttpClient(
        max_retries=0,
        rate_limit_per_minute=0,
        transport=httpx.MockTransport(handler),
        logger=RecordingLogger(),
    )
    return IdealistaApiSource(
        client=client,
        client_id="approved-client-id",
        client_secret="approved-client-secret",
        max_items=max_items,
        max_pages=max_pages,
        logger=RecordingLogger(),
    )


async def test_requests_and_caches_an_official_oauth_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == httpx.URL(TOKEN_URL).path:
            assert request.method == "POST"
            assert request.headers["authorization"].startswith("Basic ")
            assert request.content == b"grant_type=client_credentials&scope=read"
            return httpx.Response(200, json={"access_token": "api-token", "expires_in": 3600})
        assert request.url.path == httpx.URL(SEARCH_URL).path
        assert request.headers["authorization"] == "Bearer api-token"
        return httpx.Response(200, json={"elementList": [item()]})

    source = make_source(handler, max_items=1)
    first = await source.fetch(criteria())
    second = await source.fetch(criteria())

    assert len(first) == len(second) == 1
    assert sum(request.url.path == httpx.URL(TOKEN_URL).path for request in requests) == 1
    assert sum(request.url.path == httpx.URL(SEARCH_URL).path for request in requests) == 2


async def test_sends_supported_search_filters_and_paginates() -> None:
    requests: list[httpx.Request] = []
    pages = [
        {"elementList": [item("1")]},
        {"elementList": [item("2")]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == httpx.URL(TOKEN_URL).path:
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    source = make_source(handler, max_items=2, max_pages=2)
    listings = await source.fetch(
        criteria(min_price_eur=100000, max_price_eur=800000, min_size_m2=120, min_rooms=3)
    )

    assert len(listings) == 2
    assert [request.url.params["numPage"] for request in requests] == ["1", "2"]
    query = requests[0].url.params
    assert query["operation"] == "rent"
    assert query["propertyType"] == "homes"
    assert query["center"] == "40.4168,-3.7038"
    assert query["distance"] == "5"
    assert query["minPrice"] == "100000"
    assert query["maxPrice"] == "800000"
    assert query["minSize"] == "120"
    assert query["minRooms"] == "3"


async def test_maps_official_result_fields_to_the_shared_raw_schema() -> None:
    source = make_source(
        lambda request: httpx.Response(
            200,
            json=(
                {"access_token": "token", "expires_in": 3600}
                if request.url.path == httpx.URL(TOKEN_URL).path
                else {"elementList": [item()]}
            ),
        ),
        max_items=1,
    )

    raw = (await source.fetch(criteria()))[0]

    assert raw["url"] == "https://www.idealista.com/inmueble/123/"
    assert raw["title"] == "flat en Madrid"
    assert raw["address"] == "Calle Mayor, Madrid, Madrid"
    assert raw["price"] == 500000
    assert raw["size"] == 140
    assert raw["rooms"] == 4
    assert raw["bathrooms"] == 2
    assert raw["latitude"] == 40.4168
    assert raw["longitude"] == -3.7038
    assert raw["multimedia"]["images"][0]["url"] == "https://img.example/1.jpg"
    assert raw["energyCertification"] == "B"
    assert raw["yearBuilt"] == 2022


async def test_search_requires_a_geographic_origin_and_radius() -> None:
    source = make_source(lambda request: httpx.Response(500), max_items=1)

    with pytest.raises(ScraperException) as no_coordinates:
        await source.fetch(criteria(latitude=None, longitude=None))
    assert no_coordinates.value.details["reason"] == "coordinates_required"

    with pytest.raises(ScraperException) as no_radius:
        await source.fetch(criteria(radius_km=None))
    assert no_radius.value.details["reason"] == "radius_required"


async def test_invalid_oauth_response_fails_explicitly() -> None:
    source = make_source(lambda request: httpx.Response(200, json={"error": "invalid_client"}))

    with pytest.raises(ScraperException) as exc_info:
        await source.fetch(criteria())

    assert exc_info.value.details["reason"] == "missing_access_token"


async def test_api_http_error_is_not_silently_returned_as_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == httpx.URL(TOKEN_URL).path:
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(401, text="unauthorized")

    source = make_source(handler, max_items=1)

    with pytest.raises(ScraperException) as exc_info:
        await source.fetch(criteria())

    assert exc_info.value.details["status_code"] == 401
