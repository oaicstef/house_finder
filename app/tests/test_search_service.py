"""Tests unitarios de ``SearchService`` con los puertos mockeados."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app.application.services.search_service import SearchService
from app.domain.exceptions import (
    ClassifierException,
    ScraperException,
    SearchNotFoundError,
    ValidationError,
)
from app.domain.models import ListingAssessment, SearchCriteria
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.tests.conftest import FakeClassifier, FakeScraper, RecordingLogger, make_listing


def build_service(
    scraper: FakeScraper,
    classifier: FakeClassifier,
    repository: InMemoryListingRepository,
) -> tuple[SearchService, RecordingLogger]:
    logger = RecordingLogger()
    service = SearchService(
        scraper,
        classifier,
        repository,
        logger=logger,
        today=lambda: date(2026, 11, 4),
    )
    return service, logger


async def test_warns_when_a_requested_portal_contributes_nothing() -> None:
    """Un portal sin anuncios aceptados se registra en WARNING, no en silencio."""
    from app.domain.models import Portal

    fotocasa = make_listing("https://www.fotocasa.es/inmueble/2/", portal=Portal.FOTOCASA)
    service, logger = build_service(
        FakeScraper([fotocasa]),
        FakeClassifier(),
        InMemoryListingRepository(logger=RecordingLogger()),
    )

    result = await service.run_search(
        SearchCriteria(location="Madrid, Madrid", portals=(Portal.IDEALISTA, Portal.FOTOCASA))
    )

    assert [outcome.portal for outcome in result.portal_outcomes] == [
        Portal.IDEALISTA,
        Portal.FOTOCASA,
    ]
    assert [outcome.matched for outcome in result.portal_outcomes] == [0, 1]
    event = logger.find("search_portal_without_results")
    assert event is not None
    assert event["level"] == "warning"
    assert event["portal"] == "idealista"
    assert event["location"] == "Madrid, Madrid"
    assert result.warnings == (
        "idealista: sin anuncios; revisa el actor del portal y la ubicación",
    )


async def test_no_portal_warning_when_every_portal_contributes() -> None:
    from app.domain.models import Portal

    idealista = make_listing("https://www.idealista.com/inmueble/1/")
    service, logger = build_service(
        FakeScraper([idealista]),
        FakeClassifier(),
        InMemoryListingRepository(logger=RecordingLogger()),
    )

    result = await service.run_search(
        SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,))
    )

    assert "search_portal_without_results" not in logger.event_names()
    assert result.warnings == ()


async def test_run_search_returns_only_matching_listings_sorted_by_relevance() -> None:
    best = make_listing("https://www.idealista.com/inmueble/1/", price=1100.0)
    second = make_listing("https://www.fotocasa.es/inmueble/2/", price=1300.0)
    discarded = make_listing("https://www.idealista.com/inmueble/3/", price=1450.0)

    scraper = FakeScraper([second, discarded, best])
    classifier = FakeClassifier(
        [
            ListingAssessment(listing_url=second.url, relevance=0.72, matches=True, reason="OK"),
            ListingAssessment(listing_url=best.url, relevance=0.95, matches=True, reason="Ideal"),
            ListingAssessment(listing_url=discarded.url, relevance=0.1, matches=False, reason="No"),
        ]
    )
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, logger = build_service(scraper, classifier, repository)

    result = await service.run_search(SearchCriteria(location="Madrid"), trigger="api")

    assert [item.listing.url for item in result.listings] == [best.url, second.url]
    assert result.scraped_count == 3
    assert result.discarded_count == 1
    assert result.trigger.value == "api"
    assert result.run_date == date(2026, 11, 4)
    assert logger.find("search_completed") is not None
    assert logger.find("search_completed")["matched_count"] == 2


async def test_run_search_persists_result_retrievable_by_id() -> None:
    listing = make_listing()
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, _ = build_service(FakeScraper([listing]), FakeClassifier(), repository)

    result = await service.run_search(SearchCriteria(location="Madrid"))

    stored = await service.get_results(result.search_id)
    assert stored.search_id == result.search_id
    assert stored.criteria_fingerprint == result.criteria.fingerprint()


async def test_run_search_limits_number_of_results() -> None:
    listings = [make_listing(f"https://www.idealista.com/inmueble/{index}/") for index in range(5)]
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, _ = build_service(FakeScraper(listings), FakeClassifier(), repository)

    result = await service.run_search(SearchCriteria(location="Madrid", max_results=2))

    assert len(result.listings) == 2
    assert result.discarded_count == 3


async def test_run_search_does_not_call_classifier_without_listings() -> None:
    classifier = FakeClassifier()
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, _ = build_service(FakeScraper([]), classifier, repository)

    result = await service.run_search(SearchCriteria(location="Madrid"))

    assert result.listings == ()
    assert classifier.calls == []


async def test_run_search_propagates_scraper_exception_and_does_not_persist() -> None:
    repository = InMemoryListingRepository(logger=RecordingLogger())
    scraper = FakeScraper(error=ScraperException("Apify caído"))
    service, logger = build_service(scraper, FakeClassifier(), repository)

    with pytest.raises(ScraperException):
        await service.run_search(SearchCriteria(location="Madrid"))

    assert logger.find("search_scraper_failed")["error_code"] == "SCRAPER_FAILURE"
    assert await repository.get_by_id(uuid4()) is None


async def test_run_search_propagates_classifier_exception() -> None:
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, _ = build_service(
        FakeScraper([make_listing()]),
        FakeClassifier(error=ClassifierException("LLM caído")),
        repository,
    )

    with pytest.raises(ClassifierException):
        await service.run_search(SearchCriteria(location="Madrid"))


async def test_run_search_rejects_invalid_price_range_without_scraping() -> None:
    scraper = FakeScraper([make_listing()])
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, _ = build_service(scraper, FakeClassifier(), repository)

    with pytest.raises(ValidationError):
        await service.run_search(SearchCriteria(location="Madrid", min_price_eur=2000.0, max_price_eur=1000.0))

    assert scraper.calls == []


async def test_run_search_rejects_blank_location() -> None:
    service, _ = build_service(FakeScraper(), FakeClassifier(), InMemoryListingRepository())

    with pytest.raises(ValidationError):
        await service.run_search(SearchCriteria(location="   "))


async def test_get_results_raises_not_found_for_unknown_id() -> None:
    service, logger = build_service(
        FakeScraper(),
        FakeClassifier(),
        InMemoryListingRepository(logger=RecordingLogger()),
    )

    with pytest.raises(SearchNotFoundError) as exc_info:
        await service.get_results(uuid4())

    assert exc_info.value.code == "SEARCH_NOT_FOUND"
    assert exc_info.value.http_status == 404
    assert logger.find("search_not_found") is not None
