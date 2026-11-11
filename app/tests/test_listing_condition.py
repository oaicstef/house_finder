"""Normalización y respuesta API del estado de conservación del anuncio."""

from __future__ import annotations

from typing import Literal

import pytest

from app.api.schemas import ListingResponse
from app.domain.models import Listing, Portal, ScoredListing
from app.infrastructure.scrapers.normalization import normalize_listing


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("newDevelopment", True, "new"),
        ("newDevelopment", False, "second_hand"),
        ("listingCondition", "Obra nueva", "new"),
        ("propertyCondition", "Segunda mano", "second_hand"),
        ("condition", "used", "second_hand"),
        ("newDevelopment", "not-specified", None),
        ("status", "active", None),
    ],
)
def test_normalizes_explicit_listing_condition(
    field: str, value: object, expected: str | None
) -> None:
    listing = normalize_listing(
        Portal.IDEALISTA,
        {
            "url": "https://example.test/listing/1",
            "price": 250000,
            field: value,
        },
    )

    assert listing is not None
    assert listing.listing_condition == expected


def test_missing_listing_condition_remains_unknown() -> None:
    listing = normalize_listing(
        Portal.IDEALISTA,
        {"url": "https://example.test/listing/1", "price": 250000},
    )

    assert listing is not None
    assert listing.listing_condition is None


@pytest.mark.parametrize("condition", ["new", "second_hand", None])
def test_listing_response_includes_listing_condition(
    condition: Literal["new", "second_hand"] | None,
) -> None:
    listing = Listing(
        portal=Portal.IDEALISTA,
        url="https://example.test/listing/1",
        title="Piso",
        price_eur=250000,
        location="Madrid",
        listing_condition=condition,
    )

    response = ListingResponse.from_domain(
        ScoredListing(listing=listing, relevance=0.9, reason="Encaja")
    )

    assert response.model_dump()["listing_condition"] == condition
