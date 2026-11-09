"""Tests del perfil de búsqueda configurable, las fotos y la UI estática."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.schemas import SearchCreateRequest
from app.config import DEFAULT_SEARCH_PROFILE, DEFAULT_SEARCH_PROFILE_JSON, Settings
from app.domain.exceptions import ConfigurationException
from app.domain.models import Portal, ScoredListing, SearchCriteria, SearchResult, Trigger
from app.infrastructure.repositories.json_file_repository import JsonFileListingRepository
from app.infrastructure.scrapers.apify_scraper import ApifyListingScraper
from app.main import create_app
from app.tests.conftest import RecordingLogger, make_listing


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


def build_scraper(client: _FakeClient, portal: Portal = Portal.IDEALISTA) -> ApifyListingScraper:
    return ApifyListingScraper(
        token="test-token",
        actor_ids={portal: f"actor-{portal.value}"},
        max_items=10,
        timeout_seconds=60,
        operation="sale",
        client_factory=lambda token: client,
        logger=RecordingLogger(),
    )


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


# --- Perfil de búsqueda -----------------------------------------------------


def build_settings(**kwargs: Any) -> Settings:
    return Settings(environment="test", enable_scheduler=False, _env_file=None, **kwargs)


def test_default_profile_matches_the_requested_body() -> None:
    profile = build_settings().build_search_profile()

    assert profile is not None
    assert profile["postal_code"] == "28031"
    assert profile["radius_km"] == 5
    assert profile["min_year_built"] == 2020
    assert profile["min_size_m2"] == 140
    assert profile["max_results"] == 50
    assert profile["must_have"] == ["padel"]
    # El cuerpo por defecto es válido según el contrato de la API.
    SearchCreateRequest.model_validate(profile)


def test_default_profile_is_used_by_the_daily_job() -> None:
    criteria = build_settings().build_daily_criteria()

    assert criteria.postal_code == "28031"
    assert criteria.radius_km == 5
    assert criteria.min_year_built == 2020
    assert criteria.min_size_m2 == 140.0
    assert criteria.must_have == ("padel",)


def test_profile_can_be_overridden_without_touching_code() -> None:
    payload = {
        "location": "Valencia",
        "portals": ["idealista"],
        "max_price_eur": 500000,
        "postal_code": "46001",
        "radius_km": 3,
    }
    settings = build_settings(search_profile_json=json.dumps(payload))

    criteria = settings.build_daily_criteria()

    assert criteria.location == "Valencia"
    assert criteria.portals == (Portal.IDEALISTA,)
    assert criteria.postal_code == "46001"
    assert criteria.radius_km == 3


def test_empty_profile_falls_back_to_daily_settings() -> None:
    settings = build_settings(
        search_profile_json="",
        daily_search_location="Bilbao",
        daily_max_price_eur=800.0,
    )

    assert settings.build_search_profile() is None
    criteria = settings.build_daily_criteria()
    assert criteria.location == "Bilbao"
    assert criteria.max_price_eur == 800.0


def test_invalid_json_profile_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationException) as exc_info:
        build_settings(search_profile_json="{no es json").build_search_profile()

    assert exc_info.value.code == "CONFIGURATION_ERROR"


def test_profile_with_unknown_field_raises_configuration_error() -> None:
    payload = dict(DEFAULT_SEARCH_PROFILE, unexpected_field=1)

    with pytest.raises(ConfigurationException) as exc_info:
        build_settings(search_profile_json=json.dumps(payload)).build_search_profile()

    assert exc_info.value.code == "CONFIGURATION_ERROR"
    assert exc_info.value.details["fields"]


@pytest.mark.parametrize(
    "payload",
    [
        '["no", "es", "un", "objeto"]',
        json.dumps({"location": "Madrid", "radius_km": 5}),
        json.dumps({"location": "Madrid", "postal_code": "28031"}),
    ],
)
def test_profile_with_invalid_contract_raises_configuration_error(payload: str) -> None:
    with pytest.raises(ConfigurationException):
        build_settings(search_profile_json=payload).build_search_profile()


def test_profile_changes_the_fingerprint() -> None:
    base = build_settings().build_daily_criteria()
    other = build_settings(
        search_profile_json=json.dumps(
            dict(DEFAULT_SEARCH_PROFILE, min_year_built=2015, postal_code="28022")
        )
    ).build_daily_criteria()

    assert base.fingerprint() != other.fingerprint()


def test_default_profile_json_constant_is_valid_json() -> None:
    assert json.loads(DEFAULT_SEARCH_PROFILE_JSON) == DEFAULT_SEARCH_PROFILE


# --- Fotos ------------------------------------------------------------------


async def test_extracts_idealista_photos() -> None:
    images = [{"url": f"https://img4.idealista.com/foto-{index}.webp"} for index in range(3)]
    client = _FakeClient([raw_item(multimedia={"images": images})])

    listings = await build_scraper(client).fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,))
    )

    assert listings[0].photos == (
        "https://img4.idealista.com/foto-0.webp",
        "https://img4.idealista.com/foto-1.webp",
        "https://img4.idealista.com/foto-2.webp",
    )


async def test_extracts_fotocasa_photos() -> None:
    multimedia = [
        {"url": "https://static.fotocasa.es/ads/1.jpg", "classification": "exterior"},
        {"url": "https://static.fotocasa.es/ads/2.jpg", "classification": "bedroom"},
    ]
    item = raw_item(
        "https://www.fotocasa.es/es/comprar/vivienda/madrid/1/1",
        multimedia=multimedia,
        transaction={"price": 300000},
    )
    client = _FakeClient([item])

    listings = await build_scraper(client, Portal.FOTOCASA).fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.FOTOCASA,))
    )

    assert listings[0].photos == (
        "https://static.fotocasa.es/ads/1.jpg",
        "https://static.fotocasa.es/ads/2.jpg",
    )


async def test_deduplicates_and_caps_photos() -> None:
    urls = [f"https://img.test/{index}.jpg" for index in range(10)] + ["https://img.test/0.jpg"]
    client = _FakeClient([raw_item(photos=[{"url": url} for url in urls])])

    listings = await build_scraper(client).fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,))
    )

    photos = listings[0].photos
    assert len(photos) == 6
    assert len(set(photos)) == 6


async def test_ignores_non_http_photo_entries() -> None:
    client = _FakeClient([raw_item(photos=["/local/image.jpg", {"url": None}, {"url": "https://ok.test/1.jpg"}])])

    listings = await build_scraper(client).fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,))
    )

    assert listings[0].photos == ("https://ok.test/1.jpg",)


# --- API y UI ---------------------------------------------------------------


def make_client(make_container: Any, **kwargs: Any) -> TestClient:
    container = make_container(**kwargs)
    container.settings.search_profile_json = DEFAULT_SEARCH_PROFILE_JSON
    return TestClient(create_app(container=container))


def test_search_profile_endpoint_returns_the_active_profile(make_container: Any) -> None:
    with make_client(make_container) as client:
        response = client.get("/api/v1/search-profile")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "profile"
    assert body["profile"]["postal_code"] == "28031"
    assert body["profile"]["min_year_built"] == 2020


def test_profile_run_endpoint_executes_the_configured_search(make_container: Any) -> None:
    listing = make_listing(
        title="Chalet con pista de pádel y piscina",
        price=850000.0,
        size=180.0,
        rooms=4,
        year_built=2021,
        photos=("https://img.test/1.jpg",),
    )
    container = make_container(scraper=None, postal_code_locator=_AlwaysLocates())
    container.scraper.listings = [listing]

    with TestClient(create_app(container=container)) as client:
        response = client.post("/api/v1/search-profile/run")

    assert response.status_code == 201
    body = response.json()
    assert body["trigger"] == "profile"
    assert body["listings"][0]["photos"] == ["https://img.test/1.jpg"]
    assert container.scraper.calls[0].postal_code == "28031"


def test_search_profile_endpoint_reports_configuration_errors(make_container: Any) -> None:
    container = make_container()
    container.settings.search_profile_json = "{roto"

    with TestClient(create_app(container=container)) as client:
        response = client.get("/api/v1/search-profile")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIGURATION_ERROR"


def test_ui_index_and_static_assets_are_served(make_container: Any) -> None:
    with make_client(make_container) as client:
        index = client.get("/")
        script = client.get("/static/app.js")
        styles = client.get("/static/styles.css")

    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "results" in index.text
    # Editor de criterios y versiones con cache-busting en el HTML.
    assert 'id="criteria-form"' in index.text
    assert 'id="save-criteria"' in index.text
    assert 'id="reset-criteria"' in index.text
    assert "/static/app.js?v=4" in index.text
    # La UI móvil: viewport con safe-area y barra de acciones fija en CSS.
    assert "viewport-fit=cover" in index.text
    # Orden por distancia disponible para el usuario final.
    assert '<option value="distance">' in index.text
    assert "distance_km" in script.text
    assert "env(safe-area-inset-bottom)" in styles.text
    assert script.status_code == 200
    assert "search-profile/run" in script.text
    # La UI edita el perfil y recupera la última búsqueda guardada.
    assert 'method: "PUT"' in script.text
    assert "searches/latest" in script.text
    assert styles.status_code == 200
    assert "--accent" in styles.text


class _AlwaysLocates:
    async def locate(self, postal_code: str) -> Any:
        from app.domain.models import Coordinates

        return Coordinates(latitude=40.378766, longitude=-3.602295)


# --- Persistencia de fotos --------------------------------------------------


def build_result() -> SearchResult:
    listing = make_listing(photos=("https://img.test/1.jpg", "https://img.test/2.jpg"))
    return SearchResult(
        search_id=uuid4(),
        criteria=SearchCriteria(location="Madrid", postal_code="28031", radius_km=5),
        criteria_fingerprint="fp-photos",
        run_date=date(2026, 11, 7),
        created_at=datetime(2026, 11, 7, 7, 0, tzinfo=timezone.utc),
        trigger=Trigger.PROFILE,
        listings=(ScoredListing(listing, 0.9, "ok"),),
        scraped_count=1,
        discarded_count=0,
    )


async def test_json_repository_roundtrip_keeps_photos(tmp_path: Path) -> None:
    repository = JsonFileListingRepository(tmp_path / "results", logger=RecordingLogger())
    result = build_result()

    await repository.save(result)
    restored = await repository.get_by_id(result.search_id)

    assert restored is not None
    assert restored.listings[0].listing.photos == (
        "https://img.test/1.jpg",
        "https://img.test/2.jpg",
    )
    assert restored.trigger is Trigger.PROFILE


async def test_json_repository_reads_legacy_payload_without_photos(tmp_path: Path) -> None:
    search_id = uuid4()
    directory = tmp_path / "results"
    directory.mkdir()
    legacy = {
        "search_id": str(search_id),
        "criteria_fingerprint": "fp-legacy",
        "run_date": "2026-11-05",
        "created_at": "2026-11-05T07:00:00+00:00",
        "trigger": "api",
        "matched_count": 1,
        "criteria": {"location": "Madrid", "portals": ["idealista"]},
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

    repository = JsonFileListingRepository(directory, logger=RecordingLogger())
    restored = await repository.get_by_id(search_id)

    assert restored is not None
    assert restored.listings[0].listing.photos == ()
