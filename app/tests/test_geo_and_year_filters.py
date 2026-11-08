"""Tests de los filtros geográficos y de año de construcción."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from app.api.schemas import SearchCreateRequest
from app.application.services.search_service import SearchService
from app.config import Settings
from app.domain.exceptions import (
    ConfigurationException,
    GeoLocationException,
    ValidationError,
)
from app.domain.models import (
    Coordinates,
    Portal,
    ScoredListing,
    SearchCriteria,
    SearchResult,
    Trigger,
)
from app.infrastructure.classifiers.deepseek_classifier import DeepSeekListingClassifier
from app.infrastructure.geocoding.carto_ciudad_locator import CartoCiudadPostalCodeLocator
from app.infrastructure.repositories.json_file_repository import JsonFileListingRepository
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.infrastructure.scrapers.apify_scraper import ApifyListingScraper
from app.main import create_app
from app.tests.conftest import FakeClassifier, FakeScraper, RecordingLogger, make_listing

CP_28031 = Coordinates(latitude=40.37876642147255, longitude=-3.602295005978209)


class FakePostalCodeLocator:
    """Doble del port ``PostalCodeLocatorPort``."""

    def __init__(self, coordinates: Coordinates | None) -> None:
        self.coordinates = coordinates
        self.calls: list[str] = []

    async def locate(self, postal_code: str) -> Coordinates | None:
        self.calls.append(postal_code)
        return self.coordinates


class _FakeDataset:
    def __init__(self, items: Iterable[dict[str, Any]]) -> None:
        self._items = list(items)

    def iterate_items(self) -> Iterable[dict[str, Any]]:
        return iter(self._items)


class _FakeRun:
    default_dataset_id = "ds-1"
    status = "SUCCEEDED"


class _FakeActorHandle:
    def __init__(self, client: "_FakeClient", actor_id: str) -> None:
        self._client = client
        self._actor_id = actor_id

    def call(self, **kwargs: Any) -> Any:
        self._client.calls.append({"actor_id": self._actor_id, **kwargs})
        return _FakeRun()


class _FakeClient:
    def __init__(self, items: Iterable[dict[str, Any]]) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []

    def actor(self, actor_id: str) -> _FakeActorHandle:
        return _FakeActorHandle(self, actor_id)

    def dataset(self, dataset_id: str) -> _FakeDataset:
        return _FakeDataset(self.items)


def build_scraper(
    client: _FakeClient,
    *,
    actor_ids: dict[Portal, str] | None = None,
    **overrides: Any,
) -> ApifyListingScraper:
    params: dict[str, Any] = {
        "token": "test-token",
        "actor_ids": actor_ids or {Portal.IDEALISTA: "actor-idealista"},
        "max_items": 40,
        "timeout_seconds": 120,
        "operation": "sale",
        "client_factory": lambda token: client,
        "logger": RecordingLogger(),
    }
    params.update(overrides)
    return ApifyListingScraper(**params)


def raw_item(url: str = "https://www.idealista.com/inmueble/1/", **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "url": url,
        "title": "Chalet en Madrid",
        "price": "900000",
        "size": "200",
        "rooms": "4",
        "location": "Madrid",
    }
    item.update(extra)
    return item


def build_service(*, locator: Any = None, scraper: Any = None, logger: Any = None) -> SearchService:
    return SearchService(
        scraper or FakeScraper([make_listing()]),
        FakeClassifier(),
        InMemoryListingRepository(logger=RecordingLogger()),
        postal_code_locator=locator,
        logger=logger or RecordingLogger(),
        today=lambda: date(2026, 11, 7),
    )


def test_schema_accepts_geo_and_year_fields() -> None:
    request = SearchCreateRequest.model_validate(
        {
            "location": "Madrid",
            "portals": ["idealista", "fotocasa"],
            "max_price_eur": 900000,
            "min_size_m2": 150,
            "min_rooms": 4,
            "max_results": 50,
            "preferences": "Casa o chalet, piscina comunitaria, pádel comunitario",
            "must_have": ["padel"],
            "postal_code": "28031",
            "radius_km": 5,
            "min_year_built": 2020,
        }
    )

    criteria = request.to_domain()
    assert criteria.postal_code == "28031"
    assert criteria.radius_km == 5
    assert criteria.min_year_built == 2020


@pytest.mark.parametrize(
    "payload",
    [
        {"location": "Madrid", "postal_code": "2803", "radius_km": 5},
        {"location": "Madrid", "radius_km": 5},
        {"location": "Madrid", "postal_code": "28031"},
        {"location": "Madrid", "latitude": 40.4, "radius_km": 5},
        {
            "location": "Madrid",
            "postal_code": "28031",
            "latitude": 40.4,
            "longitude": -3.6,
            "radius_km": 5,
        },
        {"location": "Madrid", "postal_code": "28031", "radius_km": 0},
        {"location": "Madrid", "postal_code": "28031", "radius_km": 51},
        {"location": "Madrid", "postal_code": "28031", "radius_km": 5, "min_year_built": 3000},
    ],
)
def test_schema_rejects_invalid_geo_combinations(payload: dict[str, Any]) -> None:
    with pytest.raises(PydanticValidationError):
        SearchCreateRequest.model_validate(payload)


def test_api_accepts_postal_code_radius_and_year(make_container: Any) -> None:
    scraper = FakeScraper([make_listing(year_built=2021)])
    container = make_container(
        scraper=scraper,
        postal_code_locator=FakePostalCodeLocator(CP_28031),
    )

    with TestClient(create_app(container=container)) as client:
        response = client.post(
            "/api/v1/searches",
            json={
                "location": "Madrid",
                "postal_code": "28031",
                "radius_km": 5,
                "min_year_built": 2020,
            },
        )

    assert response.status_code == 201
    assert response.json()["listings"][0]["year_built"] == 2021
    assert scraper.calls[0].latitude == pytest.approx(CP_28031.latitude)
    assert scraper.calls[0].longitude == pytest.approx(CP_28031.longitude)


def test_api_returns_422_for_invalid_geo(make_container: Any) -> None:
    with TestClient(create_app(container=make_container())) as client:
        response = client.post(
            "/api/v1/searches",
            json={"location": "Madrid", "radius_km": 5},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_api_returns_422_when_postal_code_is_not_found(make_container: Any) -> None:
    container = make_container(postal_code_locator=FakePostalCodeLocator(None))

    with TestClient(create_app(container=container)) as client:
        response = client.post(
            "/api/v1/searches",
            json={"location": "Madrid", "postal_code": "99999", "radius_km": 5},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "POSTAL_CODE_NOT_FOUND"


async def test_service_resolves_postal_code_for_the_scraper_only() -> None:
    locator = FakePostalCodeLocator(CP_28031)
    scraper = FakeScraper([make_listing()])
    logger = RecordingLogger()
    service = build_service(locator=locator, scraper=scraper, logger=logger)
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.IDEALISTA,),
        postal_code="28031",
        radius_km=5,
    )

    result = await service.run_search(criteria)

    passed = scraper.calls[0]
    assert passed.postal_code == "28031"
    assert passed.radius_km == 5
    assert passed.latitude == pytest.approx(CP_28031.latitude)
    assert passed.longitude == pytest.approx(CP_28031.longitude)
    assert result.criteria.postal_code == "28031"
    assert result.criteria.latitude is None
    assert locator.calls == ["28031"]
    assert "postal_code_resolved" in logger.event_names()


async def test_service_raises_when_postal_code_is_not_found() -> None:
    scraper = FakeScraper([make_listing()])
    logger = RecordingLogger()
    service = build_service(
        locator=FakePostalCodeLocator(None),
        scraper=scraper,
        logger=logger,
    )
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.IDEALISTA,),
        postal_code="99999",
        radius_km=5,
    )

    with pytest.raises(ValidationError) as exc_info:
        await service.run_search(criteria)

    assert exc_info.value.code == "POSTAL_CODE_NOT_FOUND"
    assert scraper.calls == []
    assert "postal_code_not_found" in logger.event_names()


async def test_service_requires_locator_for_postal_code() -> None:
    service = build_service()
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.IDEALISTA,),
        postal_code="28031",
        radius_km=5,
    )

    with pytest.raises(ConfigurationException):
        await service.run_search(criteria)


async def test_service_accepts_explicit_coordinates_without_locator() -> None:
    scraper = FakeScraper([make_listing()])
    service = build_service(scraper=scraper)
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.IDEALISTA,),
        latitude=40.378766,
        longitude=-3.602295,
        radius_km=5,
    )

    await service.run_search(criteria)

    assert scraper.calls[0].latitude == 40.378766
    assert scraper.calls[0].longitude == -3.602295


@pytest.mark.parametrize(
    "overrides",
    [
        {"radius_km": 5},
        {"latitude": 40.4, "radius_km": 5},
        {"postal_code": "28031", "latitude": 40.4, "longitude": -3.6, "radius_km": 5},
        {"postal_code": "2803", "radius_km": 5},
    ],
)
async def test_service_rejects_invalid_geo_combinations(overrides: dict[str, Any]) -> None:
    service = build_service(locator=FakePostalCodeLocator(CP_28031))
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.IDEALISTA,),
        **overrides,
    )

    with pytest.raises(ValidationError):
        await service.run_search(criteria)


@pytest.mark.parametrize(
    ("portal", "expected_property_type"),
    [(Portal.IDEALISTA, "homes"), (Portal.FOTOCASA, "home")],
)
async def test_run_input_uses_coordinates_and_radius(
    portal: Portal,
    expected_property_type: str,
) -> None:
    client = _FakeClient([raw_item()])
    scraper = build_scraper(
        client,
        actor_ids={portal: f"actor-{portal.value}"},
    )
    criteria = SearchCriteria(
        location="Madrid",
        portals=(portal,),
        latitude=CP_28031.latitude,
        longitude=CP_28031.longitude,
        radius_km=5,
    )

    await scraper.fetch_listings(criteria)

    run_input = client.calls[0]["run_input"]
    assert run_input["propertyType"] == expected_property_type
    assert run_input["latitude"] == pytest.approx(CP_28031.latitude)
    assert run_input["longitude"] == pytest.approx(CP_28031.longitude)
    assert run_input["distanceKm"] == 5
    assert "location" not in run_input


async def test_scraper_drops_listings_outside_the_radius() -> None:
    items = [
        raw_item("https://www.idealista.com/inmueble/inside/", latitude=40.38, longitude=-3.60),
        raw_item("https://www.idealista.com/inmueble/outside/", latitude=40.50, longitude=-3.60),
        raw_item("https://www.idealista.com/inmueble/unknown/"),
    ]
    client = _FakeClient(items)
    scraper = build_scraper(client)
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.IDEALISTA,),
        latitude=CP_28031.latitude,
        longitude=CP_28031.longitude,
        radius_km=5,
    )

    listings = await scraper.fetch_listings(criteria)

    assert [listing.url for listing in listings] == [
        "https://www.idealista.com/inmueble/inside/",
        "https://www.idealista.com/inmueble/unknown/",
    ]


async def test_scraper_drops_known_years_older_than_the_minimum() -> None:
    items = [
        raw_item("https://www.idealista.com/inmueble/old/", yearBuilt=2015),
        raw_item("https://www.idealista.com/inmueble/new/", yearBuilt=2021),
        raw_item("https://www.idealista.com/inmueble/unknown/"),
        raw_item(
            "https://www.idealista.com/inmueble/regex-old/",
            description="Chalet construido en 2018",
        ),
        raw_item(
            "https://www.idealista.com/inmueble/regex-new/",
            description="Vivienda de obra nueva construida en 2022",
        ),
    ]
    client = _FakeClient(items)
    scraper = build_scraper(client)
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.IDEALISTA,),
        min_year_built=2020,
    )

    listings = await scraper.fetch_listings(criteria)

    urls = {listing.url for listing in listings}
    assert "https://www.idealista.com/inmueble/old/" not in urls
    assert "https://www.idealista.com/inmueble/regex-old/" not in urls
    assert "https://www.idealista.com/inmueble/new/" in urls
    assert "https://www.idealista.com/inmueble/unknown/" in urls
    assert "https://www.idealista.com/inmueble/regex-new/" in urls


async def test_normalizes_idealista_coordinates_and_year() -> None:
    client = _FakeClient(
        [raw_item(latitude=40.4, longitude=-3.7, yearBuilt=2021)]
    )
    criteria = SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,))

    listings = await build_scraper(client).fetch_listings(criteria)

    assert listings[0].latitude == 40.4
    assert listings[0].longitude == -3.7
    assert listings[0].year_built == 2021


async def test_normalizes_fotocasa_nested_shape() -> None:
    item = {
        "propertyId": "187123129",
        "url": "https://www.fotocasa.es/es/comprar/vivienda/madrid/187123129/1",
        "transaction": {"type": "SALE", "price": 120000},
        "surface": 57,
        "rooms": 3,
        "baths": 1,
        "street": "Calle de Benimamet",
        "number": "51",
        "antiquity": 5,
        "location": {
            "latitude": "40.3443439",
            "longitude": "-3.6871201",
            "level5Name": "Madrid",
            "level7Name": "Barrio de Salamanca",
            "level8Name": "Guindalera",
        },
        "description": "Piso exterior con ascensor",
    }
    client = _FakeClient([item])
    criteria = SearchCriteria(location="Madrid", portals=(Portal.FOTOCASA,))

    listings = await build_scraper(
        client,
        actor_ids={Portal.FOTOCASA: "actor-fotocasa"},
    ).fetch_listings(criteria)

    listing = listings[0]
    assert listing.price_eur == 120000.0
    assert listing.size_m2 == 57.0
    assert listing.rooms == 3
    assert listing.bathrooms == 1
    # La calle y el número viven en el título; ``location`` es la zona legible.
    assert listing.title == (
        "Calle de Benimamet 51, Guindalera, Barrio de Salamanca, Madrid"
    )
    assert listing.location == "Guindalera, Barrio de Salamanca, Madrid"
    assert listing.latitude == pytest.approx(40.3443439)
    assert listing.longitude == pytest.approx(-3.6871201)
    assert listing.year_built == date.today().year - 5


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class _FakeLLMClient:
    def __init__(self, content: str) -> None:
        self.completions = _FakeCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)


async def test_classifier_receives_geo_and_year_criteria() -> None:
    listing = make_listing(year_built=2021)
    client = _FakeLLMClient(
        json.dumps({"assessments": [{"listing_url": listing.url, "relevance": 0.9}]})
    )
    classifier = DeepSeekListingClassifier(
        api_key="test-key",
        client=client,
        logger=RecordingLogger(),
    )
    criteria = SearchCriteria(
        location="Madrid",
        postal_code="28031",
        radius_km=5,
        min_year_built=2020,
    )

    await classifier.assess_listings(criteria, [listing])

    sent = json.loads(client.completions.requests[0]["messages"][1]["content"])
    assert sent["criteria"]["postal_code"] == "28031"
    assert sent["criteria"]["radius_km"] == 5
    assert sent["criteria"]["min_year_built"] == 2020
    assert sent["listings"][0]["year_built"] == 2021


async def test_carto_ciudad_locator_parses_and_caches() -> None:
    calls: list[str] = []

    def http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"lat": CP_28031.latitude, "lng": CP_28031.longitude}

    locator = CartoCiudadPostalCodeLocator(
        http_get=http_get,
        logger=RecordingLogger(),
    )

    first = await locator.locate("28031")
    second = await locator.locate("28031")

    assert first == CP_28031
    assert second == CP_28031
    assert len(calls) == 1
    assert "q=28031" in calls[0]
    assert "type=postalcode" in calls[0]


async def test_carto_ciudad_locator_returns_none_when_not_found() -> None:
    calls: list[str] = []

    def http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"state": 1, "stateMsg": "not found"}

    locator = CartoCiudadPostalCodeLocator(
        http_get=http_get,
        logger=RecordingLogger(),
    )

    assert await locator.locate("99999") is None
    assert await locator.locate("99999") is None
    assert len(calls) == 1


async def test_carto_ciudad_locator_wraps_http_errors() -> None:
    def http_get(url: str) -> Any:
        raise TimeoutError("boom")

    locator = CartoCiudadPostalCodeLocator(
        http_get=http_get,
        logger=RecordingLogger(),
    )

    with pytest.raises(GeoLocationException) as exc_info:
        await locator.locate("28031")

    assert exc_info.value.code == "GEO_LOCATION_FAILURE"


def build_settings(**kwargs: Any) -> Settings:
    return Settings(
        environment="test",
        enable_scheduler=False,
        search_profile_json="",
        **kwargs,
    )


def test_daily_criteria_includes_geo_and_year() -> None:
    criteria = build_settings(
        daily_postal_code="28031",
        daily_radius_km=5,
        daily_min_year_built=2020,
    ).build_daily_criteria()

    assert criteria.postal_code == "28031"
    assert criteria.radius_km == 5
    assert criteria.min_year_built == 2020


def test_daily_blank_geo_and_year_are_none() -> None:
    criteria = build_settings(
        daily_postal_code="",
        daily_radius_km="",
        daily_min_year_built="",
    ).build_daily_criteria()

    assert criteria.postal_code is None
    assert criteria.radius_km is None
    assert criteria.min_year_built is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_postal_code": "28031"},
        {"daily_radius_km": 5},
        {"daily_postal_code": "2803", "daily_radius_km": 5},
        {"daily_postal_code": "28031", "daily_radius_km": 5, "daily_min_year_built": 2100},
    ],
)
def test_daily_invalid_geo_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationException):
        build_settings(**kwargs).build_daily_criteria()


def test_fingerprint_changes_with_geo_and_year() -> None:
    base = SearchCriteria(location="Madrid")

    assert base.fingerprint() != replace(
        base, postal_code="28031", radius_km=5
    ).fingerprint()
    assert base.fingerprint() != replace(base, min_year_built=2020).fingerprint()


def test_coordinates_distance_is_symmetric() -> None:
    near = Coordinates(latitude=40.38, longitude=-3.60)

    assert CP_28031.distance_km_to(near) < 1
    assert CP_28031.distance_km_to(near) == pytest.approx(near.distance_km_to(CP_28031))


def build_result_with_geo() -> SearchResult:
    result = SearchResult(
        search_id=uuid4(),
        criteria=SearchCriteria(
            location="Madrid",
            portals=(Portal.IDEALISTA,),
            postal_code="28031",
            radius_km=5,
            min_year_built=2020,
        ),
        criteria_fingerprint="fp-geo",
        run_date=date(2026, 11, 7),
        created_at=datetime(2026, 11, 7, 7, 0, tzinfo=timezone.utc),
        trigger=Trigger.API,
        listings=(ScoredListing(make_listing(year_built=2021), 0.9, "ok"),),
        scraped_count=1,
        discarded_count=0,
    )
    return result


async def test_json_repository_roundtrip_keeps_geo_and_year(tmp_path: Path) -> None:
    repo = JsonFileListingRepository(tmp_path / "results", logger=RecordingLogger())
    result = build_result_with_geo()

    await repo.save(result)
    restored = await repo.get_by_id(result.search_id)

    assert restored is not None
    assert restored.criteria.postal_code == "28031"
    assert restored.criteria.radius_km == 5
    assert restored.criteria.min_year_built == 2020
    assert restored.listings[0].listing.year_built == 2021


async def test_json_repository_reads_legacy_payload_without_new_fields(tmp_path: Path) -> None:
    search_id = uuid4()
    directory = tmp_path / "results"
    directory.mkdir()
    legacy = {
        "search_id": str(search_id),
        "criteria_fingerprint": "fp-legacy",
        "run_date": "2026-11-05",
        "created_at": "2026-11-05T07:00:00+00:00",
        "trigger": "api",
        "scraped_count": 1,
        "discarded_count": 0,
        "matched_count": 1,
        "criteria": {"location": "Madrid", "portals": ["idealista"], "max_results": 40},
        "listings": [
            {
                "portal": "idealista",
                "url": "https://www.idealista.com/inmueble/1/",
                "title": "Piso",
                "price_eur": 1000.0,
                "relevance": 0.9,
                "reason": "ok",
            }
        ],
    }
    (directory / f"{search_id}.json").write_text(json.dumps(legacy), encoding="utf-8")

    repo = JsonFileListingRepository(directory, logger=RecordingLogger())
    restored = await repo.get_by_id(search_id)

    assert restored is not None
    assert restored.criteria.postal_code is None
    assert restored.criteria.radius_km is None
    assert restored.criteria.min_year_built is None
    assert restored.listings[0].listing.year_built is None
