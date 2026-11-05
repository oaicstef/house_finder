"""Tests de integración de la API con ports falsos (sin red)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import ServiceContainer
from app.domain.exceptions import ScraperException
from app.main import create_app
from app.tests.conftest import FakeClassifier, FakeScraper, make_listing


def make_client(container: ServiceContainer) -> TestClient:
    return TestClient(create_app(container=container))


def test_create_search_returns_201_with_results(make_container: Any) -> None:
    listing = make_listing()
    container = make_container(scraper=FakeScraper([listing]))

    with make_client(container) as client:
        response = client.post(
            "/api/v1/searches",
            json={"location": "Madrid", "max_price_eur": 1500, "min_rooms": 2},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["trigger"] == "api"
    assert body["matched_count"] == 1
    assert body["listings"][0]["url"] == listing.url
    assert body["listings"][0]["price_per_m2"] == round(1200.0 / 80.0, 2)
    assert response.headers["X-Request-ID"]


def test_create_search_reports_portals_without_results(make_container: Any) -> None:
    """Un portal vacío debe delatarse: antes la respuesta parecía completa."""
    from app.domain.models import Portal

    fotocasa = make_listing("https://www.fotocasa.es/inmueble/2/", portal=Portal.FOTOCASA)
    container = make_container(scraper=FakeScraper([fotocasa]))

    with make_client(container) as client:
        response = client.post(
            "/api/v1/searches",
            json={"location": "Madrid, Madrid", "portals": ["idealista", "fotocasa"]},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["portals"] == [
        {"portal": "idealista", "matched": 0},
        {"portal": "fotocasa", "matched": 1},
    ]
    assert body["warnings"] == [
        "idealista: sin anuncios; revisa el actor del portal y la ubicación"
    ]


def test_create_search_propagates_request_id_header(make_container: Any) -> None:
    with make_client(make_container()) as client:
        response = client.post(
            "/api/v1/searches",
            json={"location": "Madrid"},
            headers={"X-Request-ID": "req-123"},
        )

    assert response.status_code == 201
    assert response.headers["X-Request-ID"] == "req-123"


def test_get_results_returns_stored_search(make_container: Any) -> None:
    with make_client(make_container()) as client:
        created = client.post("/api/v1/searches", json={"location": "Madrid"}).json()
        response = client.get(f"/api/v1/searches/{created['search_id']}/results")

    assert response.status_code == 200
    assert response.json()["search_id"] == created["search_id"]


def test_get_results_returns_404_for_unknown_search(make_container: Any) -> None:
    with make_client(make_container()) as client:
        response = client.get(f"/api/v1/searches/{uuid4()}/results")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SEARCH_NOT_FOUND"


def test_get_results_returns_422_for_invalid_uuid(make_container: Any) -> None:
    with make_client(make_container()) as client:
        response = client.get("/api/v1/searches/not-a-uuid/results")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_search_returns_422_for_invalid_body(make_container: Any) -> None:
    with make_client(make_container()) as client:
        response = client.post(
            "/api/v1/searches",
            json={"location": "", "min_price_eur": 2000, "max_price_eur": 1000},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_search_returns_502_when_scraper_fails(make_container: Any) -> None:
    container = make_container(scraper=FakeScraper(error=ScraperException("Apify caído")))

    with make_client(container) as client:
        response = client.post("/api/v1/searches", json={"location": "Madrid"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SCRAPER_FAILURE"


def test_scheduled_run_is_idempotent_within_the_same_day(make_container: Any) -> None:
    container = make_container(scraper=FakeScraper([make_listing()]))

    with make_client(container) as client:
        first = client.post("/api/v1/scheduled-searches/run")
        second = client.post("/api/v1/scheduled-searches/run")

    assert first.status_code == 200
    assert first.json()["executed"] is True
    assert first.json()["result"]["trigger"] == "scheduler"
    assert second.status_code == 200
    assert second.json()["executed"] is False
    assert second.json()["reason"] == "already_executed_today"


def test_scheduled_run_returns_503_when_classifier_fails(make_container: Any) -> None:
    from app.domain.exceptions import ClassifierException

    container = make_container(
        classifier=FakeClassifier(error=ClassifierException("LLM caído")),
    )

    with make_client(container) as client:
        response = client.post("/api/v1/scheduled-searches/run")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CLASSIFIER_FAILURE"


def test_health_endpoint(make_container: Any) -> None:
    with make_client(make_container()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "scheduler_enabled": False}
