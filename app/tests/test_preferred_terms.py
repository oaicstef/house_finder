"""Tests de los términos preferentes (opcionales, destacados primero)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.api.schemas import SearchCreateRequest, SearchResponse
from app.application.services.search_service import SearchService
from app.config import Settings
from app.domain.models import ListingAssessment, Portal, SearchCriteria
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.tests.conftest import FakeClassifier, FakeScraper, RecordingLogger, make_listing

PADEL_URL = "https://www.idealista.com/inmueble/1/"
NO_PADEL_URL = "https://www.idealista.com/inmueble/2/"


def build_service(scraper, classifier) -> tuple[SearchService, RecordingLogger]:
    logger = RecordingLogger()
    return (
        SearchService(
            scraper,
            classifier,
            InMemoryListingRepository(logger=RecordingLogger()),
            logger=logger,
            today=lambda: date(2026, 11, 8),
        ),
        logger,
    )


def test_matches_any_normalizes_accents_and_case() -> None:
    listing = make_listing(description="Chalet con pista de PÁDEL y piscina")

    assert listing.matches_any(("padel",)) == ("padel",)
    assert listing.matches_any(("padel", "piscina")) == ("padel", "piscina")
    assert listing.matches_any(("garaje",)) == ()


@pytest.mark.asyncio
async def test_preferred_terms_do_not_drop_listings() -> None:
    listings = [
        make_listing(PADEL_URL, description="Chalet con pista de pádel"),
        make_listing(NO_PADEL_URL, description="Piso sin pista deportiva"),
    ]
    service, _ = build_service(FakeScraper(listings), FakeClassifier())

    result = await service.run_search(
        SearchCriteria(location="Madrid", preferred_terms=("padel",))
    )

    assert {item.listing.url for item in result.listings} == {PADEL_URL, NO_PADEL_URL}


@pytest.mark.asyncio
async def test_matched_listings_are_ranked_first_even_with_lower_relevance() -> None:
    listings = [
        make_listing(NO_PADEL_URL, description="Piso sin pista deportiva"),
        make_listing(PADEL_URL, description="Chalet con pista de pádel"),
    ]
    classifier = FakeClassifier(
        assessments=[
            ListingAssessment(NO_PADEL_URL, 0.95, True, "Encaja"),
            ListingAssessment(PADEL_URL, 0.70, True, "Encaja"),
        ]
    )
    service, logger = build_service(FakeScraper(listings), classifier)

    result = await service.run_search(
        SearchCriteria(location="Madrid", preferred_terms=("padel",))
    )

    assert [item.listing.url for item in result.listings] == [PADEL_URL, NO_PADEL_URL]
    assert logger.find("listings_ranked_by_preferences") is not None


@pytest.mark.asyncio
async def test_without_preferred_terms_relevance_order_is_kept() -> None:
    listings = [
        make_listing(NO_PADEL_URL, description="Piso sin pista deportiva"),
        make_listing(PADEL_URL, description="Chalet con pista de pádel"),
    ]
    classifier = FakeClassifier(
        assessments=[
            ListingAssessment(NO_PADEL_URL, 0.95, True, "Encaja"),
            ListingAssessment(PADEL_URL, 0.70, True, "Encaja"),
        ]
    )
    service, _ = build_service(FakeScraper(listings), classifier)

    result = await service.run_search(SearchCriteria(location="Madrid"))

    assert [item.listing.url for item in result.listings] == [NO_PADEL_URL, PADEL_URL]


@pytest.mark.asyncio
async def test_response_exposes_matched_terms() -> None:
    listings = [
        make_listing(PADEL_URL, description="Chalet con pista de pádel"),
        make_listing(NO_PADEL_URL, description="Piso sin pista deportiva"),
    ]
    service, _ = build_service(FakeScraper(listings), FakeClassifier())

    result = await service.run_search(
        SearchCriteria(location="Madrid", preferred_terms=("padel",))
    )
    response = SearchResponse.from_domain(result)
    by_url = {item.url: item for item in response.listings}

    assert by_url[PADEL_URL].matched_terms == ["padel"]
    assert by_url[NO_PADEL_URL].matched_terms == []


def test_api_normalizes_and_dedupes_preferred_terms() -> None:
    request = SearchCreateRequest(
        location="Madrid",
        preferred_terms=["Padel", " padel ", "piscina"],
    )

    assert request.preferred_terms == ["Padel", "piscina"]
    assert request.to_domain().preferred_terms == ("Padel", "piscina")


def test_fingerprint_includes_preferred_terms() -> None:
    base = SearchCriteria(location="Madrid")

    assert base.fingerprint() != replace(base, preferred_terms=("padel",)).fingerprint()


def test_daily_preferred_terms_reach_criteria() -> None:
    settings = Settings(
        environment="test",
        search_profile_json="",
        daily_preferred_terms="padel, piscina",
    )

    assert settings.build_daily_criteria().preferred_terms == ("padel", "piscina")
