"""Criterios editables desde la UI y última búsqueda siempre visible.

Sin red: los puertos se sustituyen por los fakes de ``conftest`` y el perfil vive en
``InMemorySearchProfileStore`` o en un ``tmp_path`` para el adaptador JSON.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.domain.models import Coordinates, Portal, SearchCriteria, SearchResult, Trigger
from app.infrastructure.repositories.json_file_repository import JsonFileListingRepository
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.infrastructure.repositories.search_profile_store import (
    InMemorySearchProfileStore,
    JsonSearchProfileStore,
)
from app.tests.conftest import (
    FakeScraper,
    RecordingLogger,
    make_listing,
)

PAYLOAD: dict[str, Any] = {
    "location": "Valencia",
    "portals": ["idealista"],
    "max_price_eur": 750000,
    "min_size_m2": 120,
    "min_rooms": 4,
    "max_results": 25,
    "must_have": ["piscina"],
    "postal_code": "46001",
    "radius_km": 3,
    "min_year_built": 2015,
}


def build_client(make_container: Any, **kwargs: Any) -> Any:
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(container=make_container(**kwargs)))


class _AlwaysLocates:
    """Geocodificador falso: cualquier CP cae en Madrid centro."""

    async def locate(self, postal_code: str) -> Coordinates:
        return Coordinates(latitude=40.378766, longitude=-3.602295)


# --- Perfil editable --------------------------------------------------------


def test_get_profile_reports_the_configured_source(make_container: Any) -> None:
    with build_client(make_container) as client:
        response = client.get("/api/v1/search-profile")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "profile"
    assert body["profile"]["postal_code"] == "28031"


def test_put_profile_saves_and_returns_the_override(make_container: Any) -> None:
    with build_client(make_container) as client:
        saved = client.put("/api/v1/search-profile", json=PAYLOAD)
        fetched = client.get("/api/v1/search-profile")

    assert saved.status_code == 200
    assert saved.json()["source"] == "override"
    assert saved.json()["profile"]["location"] == "Valencia"
    assert fetched.status_code == 200
    assert fetched.json()["source"] == "override"
    assert fetched.json()["profile"]["must_have"] == ["piscina"]


def test_put_profile_survives_a_restart_with_the_json_store(
    make_container: Any,
    tmp_path: Any,
) -> None:
    store = JsonSearchProfileStore(tmp_path / "search-profile.json", logger=RecordingLogger())

    with build_client(make_container, profile_store=store) as client:
        client.put("/api/v1/search-profile", json=PAYLOAD)

    # Un contenedor nuevo (equivalente a reiniciar el pod) lee el mismo fichero.
    with build_client(make_container, profile_store=store) as client:
        response = client.get("/api/v1/search-profile")

    assert response.json()["source"] == "override"
    assert response.json()["profile"]["postal_code"] == "46001"


def test_profile_run_uses_the_saved_override(make_container: Any) -> None:
    scraper = FakeScraper([make_listing()])

    with build_client(
        make_container,
        scraper=scraper,
        postal_code_locator=_AlwaysLocates(),
    ) as client:
        client.put("/api/v1/search-profile", json=PAYLOAD)
        response = client.post("/api/v1/search-profile/run")

    assert response.status_code == 201
    assert response.json()["trigger"] == "profile"
    used = scraper.calls[-1]
    assert used.location == "Valencia"
    assert used.postal_code == "46001"
    assert used.portals == (Portal.IDEALISTA,)
    assert used.must_have == ("piscina",)


def test_delete_profile_restores_the_configured_one(make_container: Any) -> None:
    with build_client(make_container) as client:
        client.put("/api/v1/search-profile", json=PAYLOAD)
        restored = client.delete("/api/v1/search-profile")

    assert restored.status_code == 200
    assert restored.json()["source"] == "profile"
    assert restored.json()["profile"]["postal_code"] == "28031"


def test_put_profile_rejects_an_invalid_body(make_container: Any) -> None:
    invalid = dict(PAYLOAD, min_price_eur=900000, max_price_eur=1000)

    with build_client(make_container) as client:
        response = client.put("/api/v1/search-profile", json=invalid)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_scheduled_job_uses_the_saved_override(make_container: Any) -> None:
    scraper = FakeScraper([make_listing()])
    store = InMemorySearchProfileStore()

    with build_client(
        make_container,
        scraper=scraper,
        profile_store=store,
        postal_code_locator=_AlwaysLocates(),
    ) as client:
        client.put("/api/v1/search-profile", json=PAYLOAD)
        response = client.post("/api/v1/scheduled-searches/run")

    assert response.status_code == 200
    assert response.json()["executed"] is True
    assert scraper.calls[-1].location == "Valencia"


# --- Última búsqueda --------------------------------------------------------


def test_latest_returns_404_when_nothing_is_saved(make_container: Any) -> None:
    with build_client(make_container) as client:
        response = client.get("/api/v1/searches/latest")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SEARCH_NOT_FOUND"


def test_latest_returns_the_last_executed_search(make_container: Any) -> None:
    with build_client(
        make_container,
        scraper=FakeScraper([make_listing()]),
        postal_code_locator=_AlwaysLocates(),
    ) as client:
        executed = client.post("/api/v1/search-profile/run")
        response = client.get("/api/v1/searches/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["search_id"] == executed.json()["search_id"]
    assert body["matched_count"] == executed.json()["matched_count"]
    assert body["listings"] == executed.json()["listings"]


def test_latest_keeps_the_listings_across_browser_sessions(make_container: Any) -> None:
    """Lo que ve el móvil no depende de la pestaña que lanzó la búsqueda."""
    container = make_container(scraper=FakeScraper([make_listing()]))

    with build_client(lambda **_: container) as client:
        executed = client.post("/api/v1/searches", json={"location": "Madrid"})
        # Otro "navegador": cliente nuevo contra el mismo contenedor/servidor.
        with build_client(lambda **_: container) as other_device:
            response = other_device.get("/api/v1/searches/latest")

    assert executed.status_code == 201
    assert len(executed.json()["listings"]) == 1
    assert response.status_code == 200
    assert response.json()["search_id"] == executed.json()["search_id"]
    assert [item["url"] for item in response.json()["listings"]] == [
        "https://www.idealista.com/inmueble/1/"
    ]


def _result(created_at: datetime) -> SearchResult:
    return SearchResult(
        search_id=uuid4(),
        criteria=SearchCriteria(location="Madrid"),
        criteria_fingerprint="fingerprint",
        run_date=date(2026, 11, 8),
        created_at=created_at,
        trigger=Trigger.API,
        listings=(),
        scraped_count=0,
        discarded_count=0,
    )


async def test_memory_repository_returns_the_most_recent_result() -> None:
    repository = InMemoryListingRepository(logger=RecordingLogger())
    oldest = _result(datetime(2026, 11, 7, 7, 0, tzinfo=timezone.utc))
    newest = _result(datetime(2026, 11, 8, 7, 0, tzinfo=timezone.utc))
    await repository.save(oldest)
    await repository.save(newest)

    latest = await repository.find_latest()

    assert latest is not None
    assert latest.search_id == newest.search_id


async def test_json_repository_returns_the_most_recent_result(tmp_path: Any) -> None:
    repository = JsonFileListingRepository(tmp_path, logger=RecordingLogger())
    oldest = _result(datetime(2026, 11, 7, 7, 0, tzinfo=timezone.utc))
    newest = _result(datetime(2026, 11, 8, 7, 0, tzinfo=timezone.utc))
    await repository.save(oldest)
    await repository.save(newest)

    latest = await repository.find_latest()

    assert latest is not None
    assert latest.search_id == newest.search_id


# --- Almacén del perfil -----------------------------------------------------


async def test_json_profile_store_round_trip(tmp_path: Any) -> None:
    store = JsonSearchProfileStore(tmp_path / "search-profile.json", logger=RecordingLogger())
    criteria = SearchCriteria(
        location="Valencia",
        portals=(Portal.IDEALISTA,),
        postal_code="46001",
        radius_km=3,
        must_have=("piscina",),
    )

    await store.save(criteria)
    loaded = await store.get()

    assert loaded is not None
    assert loaded.location == "Valencia"
    assert loaded.portals == (Portal.IDEALISTA,)
    assert loaded.must_have == ("piscina",)

    await store.clear()
    assert await store.get() is None


async def test_json_profile_store_ignores_a_corrupt_file(tmp_path: Any) -> None:
    path = tmp_path / "search-profile.json"
    path.write_text("{no es json", encoding="utf-8")
    logger = RecordingLogger()
    store = JsonSearchProfileStore(path, logger=logger)

    assert await store.get() is None
    assert logger.find("search_profile_store_skipped_file") is not None


async def test_json_profile_store_reports_write_failures(tmp_path: Any) -> None:
    from app.domain.exceptions import RepositoryException

    # Un fichero ocupando el nombre del directorio hace fallar la escritura.
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    store = JsonSearchProfileStore(blocked / "search-profile.json", logger=RecordingLogger())

    with pytest.raises(RepositoryException):
        await store.save(SearchCriteria(location="Madrid"))


def test_persisted_profile_matches_the_api_contract(tmp_path: Any) -> None:
    """El fichero guardado se puede reenviar tal cual como cuerpo de ``POST /searches``."""
    import asyncio

    from app.api.schemas import SearchCreateRequest

    path = tmp_path / "search-profile.json"
    store = JsonSearchProfileStore(path, logger=RecordingLogger())
    criteria = SearchCreateRequest.model_validate(PAYLOAD).to_domain()

    asyncio.run(store.save(criteria))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert store.path == path
    assert SearchCreateRequest.model_validate(payload).to_domain() == criteria
