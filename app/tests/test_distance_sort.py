"""Tests del origen de búsqueda y de la distancia por anuncio."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from app.api.schemas import ListingResponse, SearchResponse
from app.application.services.search_service import SearchService
from app.domain.models import (
    Coordinates,
    Listing,
    Portal,
    ScoredListing,
    SearchCriteria,
    SearchResult,
    Trigger,
)
from app.infrastructure.repositories.json_file_repository import JsonFileListingRepository
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.tests.conftest import FakeClassifier, FakeScraper, RecordingLogger, make_listing

ORIGIN = (40.4168, -3.7038)  # Puerta del Sol, Madrid
NEARBY = (40.4531, -3.6883)  # ~4 km al noreste


def _scored(listing: Listing, relevance: float = 0.9) -> ScoredListing:
    return ScoredListing(listing=listing, relevance=relevance, reason="Encaja")


def _result(listings: tuple[ScoredListing, ...] = (), **overrides) -> SearchResult:
    base = dict(
        search_id=uuid4(),
        criteria=SearchCriteria(location="Madrid"),
        criteria_fingerprint="fingerprint",
        run_date=date(2026, 11, 9),
        created_at=datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc),
        trigger=Trigger.API,
        listings=listings,
        scraped_count=len(listings),
        discarded_count=0,
        origin_latitude=ORIGIN[0],
        origin_longitude=ORIGIN[1],
    )
    base.update(overrides)
    return SearchResult(**base)


def _listing_with_coordinates(latitude: float | None, longitude: float | None) -> Listing:
    return make_listing(latitude=latitude, longitude=longitude)


def test_response_exposes_distance_from_the_search_origin() -> None:
    listing = _listing_with_coordinates(*NEARBY)
    response = SearchResponse.from_domain(_result((_scored(listing),)))

    expected = round(
        Coordinates(*ORIGIN).distance_km_to(Coordinates(*NEARBY)),
        2,
    )
    assert response.listings[0].distance_km == expected
    assert 3.5 < response.listings[0].distance_km < 5.0


def test_distance_is_null_without_origin() -> None:
    listing = _listing_with_coordinates(*NEARBY)
    result = _result(
        (_scored(listing),),
        origin_latitude=None,
        origin_longitude=None,
    )

    assert SearchResponse.from_domain(result).listings[0].distance_km is None


def test_distance_is_null_without_listing_coordinates() -> None:
    listing = _listing_with_coordinates(None, None)
    response = SearchResponse.from_domain(_result((_scored(listing),)))

    assert response.listings[0].distance_km is None


def test_listing_response_defaults_to_null_distance() -> None:
    response = ListingResponse.from_domain(_scored(make_listing()))

    assert response.distance_km is None


@pytest.mark.asyncio
async def test_run_search_records_an_explicit_origin() -> None:
    listing = make_listing(latitude=NEARBY[0], longitude=NEARBY[1])
    logger = RecordingLogger()
    service = SearchService(
        FakeScraper([listing]),
        FakeClassifier(),
        InMemoryListingRepository(logger=RecordingLogger()),
        logger=logger,
        today=lambda: date(2026, 11, 9),
    )

    result = await service.run_search(
        SearchCriteria(
            location="Madrid",
            latitude=ORIGIN[0],
            longitude=ORIGIN[1],
            radius_km=10,
        )
    )

    assert (result.origin_latitude, result.origin_longitude) == ORIGIN


class _AlwaysLocates:
    async def locate(self, postal_code: str) -> Coordinates:
        return Coordinates(*ORIGIN)


@pytest.mark.asyncio
async def test_postal_code_origin_is_recorded_without_touching_the_criteria() -> None:
    service = SearchService(
        FakeScraper([]),
        FakeClassifier(),
        InMemoryListingRepository(logger=RecordingLogger()),
        postal_code_locator=_AlwaysLocates(),
        logger=RecordingLogger(),
        today=lambda: date(2026, 11, 9),
    )

    result = await service.run_search(
        SearchCriteria(location="Madrid", postal_code="28031", radius_km=5)
    )

    assert (result.origin_latitude, result.origin_longitude) == ORIGIN
    assert result.criteria.postal_code == "28031"
    assert result.criteria.latitude is None
    assert result.criteria.longitude is None


@pytest.mark.asyncio
async def test_json_repository_round_trips_the_origin_and_coordinates(tmp_path) -> None:
    listing = _listing_with_coordinates(*NEARBY)
    repository = JsonFileListingRepository(tmp_path, logger=RecordingLogger())
    result = _result((_scored(listing),))
    await repository.save(result)

    restored = await repository.get_by_id(result.search_id)

    assert restored is not None
    assert (restored.origin_latitude, restored.origin_longitude) == ORIGIN
    restored_listing = restored.listings[0].listing
    assert (restored_listing.latitude, restored_listing.longitude) == NEARBY
    # La distancia se puede recalcular al releer la búsqueda guardada.
    distance = SearchResponse.from_domain(restored).listings[0].distance_km
    assert distance is not None and 3.5 < distance < 5.0


@pytest.mark.asyncio
async def test_legacy_results_without_origin_still_load(tmp_path) -> None:
    repository = JsonFileListingRepository(tmp_path, logger=RecordingLogger())
    result = _result()
    await repository.save(result)

    path = tmp_path / f"{result.search_id}.json"
    payload = json.loads(path.read_text())
    payload.pop("origin")
    path.write_text(json.dumps(payload))

    reloaded = JsonFileListingRepository(tmp_path, logger=RecordingLogger())
    restored = await reloaded.get_by_id(result.search_id)

    assert restored is not None
    assert restored.origin_latitude is None
    assert restored.origin_longitude is None
