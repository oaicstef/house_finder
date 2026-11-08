"""Tests del filtro por eficiencia energética (certificado A-G)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.schemas import ListingResponse, SearchCreateRequest
from app.config import Settings
from app.domain.exceptions import ValidationError as DomainValidationError
from app.domain.models import (
    ENERGY_RATING_SCALE,
    Listing,
    Portal,
    ScoredListing,
    SearchCriteria,
)
from app.domain.validation import validate_criteria
from app.tests.test_apify_scraper import (
    RAW_ITEM,
    REAL_FOTOCASA_ITEM,
    _FakeClient,
    build_scraper,
    idealista_criteria,
)


def _item(url: str = RAW_ITEM["url"], **extra: object) -> dict[str, object]:
    item = dict(RAW_ITEM)
    item["url"] = url
    item.update(extra)
    return item


def test_scale_runs_from_best_to_worst() -> None:
    assert ENERGY_RATING_SCALE == ("A", "B", "C", "D", "E", "F", "G")


@pytest.mark.asyncio
async def test_extracts_rating_from_idealista_certification() -> None:
    item = _item(energyCertification={"energyConsumption": {"rating": "B"}})

    listings = await build_scraper(_FakeClient([item])).fetch_listings(idealista_criteria())

    assert listings[0].energy_rating == "B"


@pytest.mark.asyncio
async def test_extracts_rating_from_details_payload() -> None:
    item = _item(_details={"energyCertification": {"energyConsumption": {"rating": "a"}}})

    listings = await build_scraper(_FakeClient([item])).fetch_listings(idealista_criteria())

    assert listings[0].energy_rating == "A"


@pytest.mark.asyncio
async def test_extracts_rating_from_the_real_details_shape() -> None:
    """Idealista publica la letra en ``energyConsumption.type`` ("e")."""
    item = _item(
        _details={
            "energyCertification": {
                "title": "Energy performance certificate",
                "emissions": {"type": "e", "value": 65},
                "energyConsumption": {"type": "e", "value": 154},
            },
            "moreCharacteristics": {"energyCertificationType": "e"},
        }
    )

    listings = await build_scraper(_FakeClient([item])).fetch_listings(idealista_criteria())

    assert listings[0].energy_rating == "E"


@pytest.mark.asyncio
async def test_does_not_treat_the_certificate_title_as_a_rating() -> None:
    item = _item(
        _details={
            "energyCertification": {
                "title": "Energy performance certificate",
                "energyConsumption": {"type": "b"},
            }
        }
    )

    listings = await build_scraper(_FakeClient([item])).fetch_listings(idealista_criteria())

    assert listings[0].energy_rating == "B"


@pytest.mark.asyncio
async def test_normalizes_prefixed_and_lowercase_values() -> None:
    item = _item(energyCertificate="Clase b - 45 kWh/m²")

    listings = await build_scraper(_FakeClient([item])).fetch_listings(idealista_criteria())

    assert listings[0].energy_rating == "B"


@pytest.mark.asyncio
async def test_does_not_mistake_certificate_word_for_a_rating() -> None:
    item = _item(energyCertification="certificate")

    listings = await build_scraper(_FakeClient([item])).fetch_listings(idealista_criteria())

    assert listings[0].energy_rating is None


@pytest.mark.asyncio
async def test_drops_listings_worse_than_the_minimum() -> None:
    items = [
        _item(
            "https://www.idealista.com/inmueble/1/",
            energyCertification={"energyConsumption": {"rating": "A"}},
        ),
        _item(
            "https://www.idealista.com/inmueble/2/",
            energyCertification={"energyConsumption": {"rating": "B"}},
        ),
        _item(
            "https://www.idealista.com/inmueble/3/",
            energyCertification={"energyConsumption": {"rating": "D"}},
        ),
    ]

    listings = await build_scraper(_FakeClient(items)).fetch_listings(
        idealista_criteria(max_energy_rating="B")
    )

    assert {listing.energy_rating for listing in listings} == {"A", "B"}


@pytest.mark.asyncio
async def test_drops_unknown_ratings_when_the_filter_is_active() -> None:
    listings = await build_scraper(_FakeClient([_item()])).fetch_listings(
        idealista_criteria(max_energy_rating="B")
    )

    assert listings == []


@pytest.mark.asyncio
async def test_keeps_unknown_ratings_without_the_filter() -> None:
    listings = await build_scraper(_FakeClient([_item()])).fetch_listings(idealista_criteria())

    assert len(listings) == 1
    assert listings[0].energy_rating is None


@pytest.mark.asyncio
async def test_fetch_details_only_when_the_filter_is_active() -> None:
    client = _FakeClient([_item()])
    await build_scraper(client).fetch_listings(idealista_criteria(max_energy_rating="B"))
    assert client.calls[0]["run_input"]["fetchDetails"] is True

    other = _FakeClient([_item()])
    await build_scraper(other).fetch_listings(idealista_criteria())
    assert "fetchDetails" not in other.calls[0]["run_input"]


@pytest.mark.asyncio
async def test_fotocasa_does_not_receive_fetch_details() -> None:
    client = _FakeClient([REAL_FOTOCASA_ITEM])
    scraper = build_scraper(client, actor_ids={Portal.FOTOCASA: "actor-fotocasa"})

    await scraper.fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.FOTOCASA,), max_energy_rating="B")
    )

    assert "fetchDetails" not in client.calls[0]["run_input"]


def test_fingerprint_changes_with_the_energy_filter() -> None:
    base = SearchCriteria(location="Madrid")

    assert base.fingerprint() != replace(base, max_energy_rating="B").fingerprint()


def test_api_accepts_and_normalizes_lowercase_rating() -> None:
    request = SearchCreateRequest(location="Madrid", max_energy_rating="b")

    assert request.max_energy_rating == "B"
    assert request.to_domain().max_energy_rating == "B"


@pytest.mark.parametrize("value", ["H", "BB", "1"])
def test_api_rejects_invalid_ratings(value: str) -> None:
    with pytest.raises(PydanticValidationError):
        SearchCreateRequest(location="Madrid", max_energy_rating=value)


def test_domain_validation_rejects_invalid_ratings() -> None:
    with pytest.raises(DomainValidationError):
        validate_criteria(SearchCriteria(location="Madrid", max_energy_rating="H"))


def test_listing_response_exposes_the_rating() -> None:
    scored = ScoredListing(
        listing=Listing(
            portal=Portal.IDEALISTA,
            url="https://www.idealista.com/inmueble/1/",
            title="Piso",
            price_eur=250000.0,
            location="Madrid",
            energy_rating="B",
        ),
        relevance=0.9,
        reason="Encaja",
    )

    assert ListingResponse.from_domain(scored).energy_rating == "B"


def test_daily_setting_reaches_criteria_and_blank_means_none() -> None:
    configured = Settings(
        environment="test",
        search_profile_json="",
        daily_max_energy_rating="b",
    )
    assert configured.build_daily_criteria().max_energy_rating == "B"

    blank = Settings(
        environment="test",
        search_profile_json="",
        daily_max_energy_rating="",
    )
    assert blank.build_daily_criteria().max_energy_rating is None
