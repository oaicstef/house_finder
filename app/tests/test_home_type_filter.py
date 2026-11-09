"""Tests del filtro por tipo de vivienda (piso, adosado, pareado...)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from app.api.schemas import ListingResponse, SearchCreateRequest, SearchResponse
from app.config import Settings
from app.domain.models import (
    HOME_TYPES,
    Listing,
    Portal,
    ScoredListing,
    SearchCriteria,
    SearchResult,
    Trigger,
    parse_home_type,
)
from app.tests.test_apify_scraper import RAW_ITEM, _FakeClient, build_scraper, idealista_criteria


def _item(url: str = RAW_ITEM["url"], sub: str | None = "terracedHouse", **extra: object) -> dict:
    item = dict(RAW_ITEM)
    item["url"] = url
    if sub is not None:
        item["detailedType"] = {"typology": "chalet", "subTypology": sub}
    item.update(extra)
    return item


def test_home_type_aliases_are_normalized() -> None:
    assert parse_home_type("adosado") == "terracedHouse"
    assert parse_home_type("Pareado") == "semiDetachedHouse"
    assert parse_home_type("casa independiente") == "detachedHouse"
    assert parse_home_type("piso") == "flat"
    assert parse_home_type("terracedHouse") == "terracedHouse"
    assert parse_home_type("independantHouse") == "detachedHouse"
    assert parse_home_type("castillo") is None


@pytest.mark.asyncio
async def test_sends_home_type_to_idealista_only() -> None:
    client = _FakeClient([_item()])
    await build_scraper(client).fetch_listings(
        idealista_criteria(home_types=("terracedHouse", "semiDetachedHouse"))
    )

    assert client.calls[0]["run_input"]["homeType"] == [
        "terracedHouse",
        "semiDetachedHouse",
    ]


@pytest.mark.asyncio
async def test_omits_home_type_when_not_configured() -> None:
    client = _FakeClient([_item()])
    await build_scraper(client).fetch_listings(idealista_criteria())

    assert "homeType" not in client.calls[0]["run_input"]


@pytest.mark.asyncio
async def test_extracts_canonical_home_type_from_subtypology() -> None:
    items = [
        _item("https://www.idealista.com/inmueble/1/", sub="semidetachedHouse"),
        _item("https://www.idealista.com/inmueble/2/", sub="independantHouse"),
    ]

    listings = await build_scraper(_FakeClient(items)).fetch_listings(idealista_criteria())

    assert [listing.home_type for listing in listings] == [
        "semiDetachedHouse",
        "detachedHouse",
    ]


@pytest.mark.asyncio
async def test_drops_types_not_requested_and_unknown_ones() -> None:
    items = [
        _item("https://www.idealista.com/inmueble/1/", sub="terracedHouse"),
        _item("https://www.idealista.com/inmueble/2/", sub="semidetachedHouse"),
        _item("https://www.idealista.com/inmueble/3/", sub="flat"),
        _item("https://www.idealista.com/inmueble/4/", sub=None),
    ]

    listings = await build_scraper(_FakeClient(items)).fetch_listings(
        idealista_criteria(home_types=("terracedHouse", "semiDetachedHouse"))
    )

    assert {listing.home_type for listing in listings} == {
        "terracedHouse",
        "semiDetachedHouse",
    }


@pytest.mark.asyncio
async def test_fotocasa_is_skipped_when_home_types_are_set() -> None:
    client = _FakeClient([_item()])
    scraper = build_scraper(
        client,
        actor_ids={
            Portal.IDEALISTA: "actor-idealista",
            Portal.FOTOCASA: "actor-fotocasa",
        },
    )

    await scraper.fetch_listings(
        SearchCriteria(
            location="Madrid",
            portals=(Portal.IDEALISTA, Portal.FOTOCASA),
            home_types=("terracedHouse",),
        )
    )

    actor_calls = [call["actor_id"] for call in client.calls]
    assert actor_calls, "idealista is queried"
    assert set(actor_calls) == {"actor-idealista"}, "fotocasa is never queried"


def test_api_normalizes_alias_and_fingerprint() -> None:
    request = SearchCreateRequest(
        location="Madrid",
        home_types=["adosado", "Pareado", "independiente"],
    )

    assert request.home_types == [
        "terracedHouse",
        "semiDetachedHouse",
        "detachedHouse",
    ]
    assert request.to_domain().home_types == (
        "terracedHouse",
        "semiDetachedHouse",
        "detachedHouse",
    )
    base = SearchCriteria(location="Madrid")
    assert base.fingerprint() != request.to_domain().fingerprint()


def test_listing_response_exposes_home_type() -> None:
    scored = ScoredListing(
        listing=Listing(
            portal=Portal.IDEALISTA,
            url="https://www.idealista.com/inmueble/1/",
            title="Chalet adosado",
            price_eur=400000.0,
            location="Madrid",
            home_type="terracedHouse",
        ),
        relevance=0.9,
        reason="Encaja",
    )

    assert ListingResponse.from_domain(scored).home_type == "terracedHouse"


def test_daily_home_types_reach_criteria() -> None:
    settings = Settings(
        environment="test",
        search_profile_json="",
        daily_home_types="adosado, pareado",
    )

    assert settings.build_daily_criteria().home_types == (
        "terracedHouse",
        "semiDetachedHouse",
    )


def test_home_types_constant_matches_the_actor_contract() -> None:
    assert "terracedHouse" in HOME_TYPES
    assert "semiDetachedHouse" in HOME_TYPES
    assert "detachedHouse" in HOME_TYPES
    assert "flat" in HOME_TYPES
