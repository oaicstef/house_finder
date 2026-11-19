"""Tests del resumen (digest) de viviendas nuevas que se envía al móvil."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from app.application.services.listing_alerts import build_digest, select_new_listings
from app.domain.models import Portal, ScoredListing, SearchCriteria, SearchResult, Trigger
from app.tests.conftest import make_listing


def scored(**kwargs: object) -> ScoredListing:
    return ScoredListing(make_listing(**kwargs), 0.9, "Encaja con lo pedido")  # type: ignore[arg-type]


def result(*listings: ScoredListing) -> SearchResult:
    return SearchResult(
        search_id=uuid4(),
        criteria=SearchCriteria(location="Pozuelo de Alarcón"),
        criteria_fingerprint="fp-1",
        run_date=date(2026, 11, 5),
        created_at=datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc),
        trigger=Trigger.SCHEDULER,
        listings=listings,
        scraped_count=len(listings),
        discarded_count=0,
    )


def test_the_digest_describes_price_size_rooms_and_portal() -> None:
    listing = scored(price=875000.0, size=210.0, rooms=4, title="Chalet con piscina")

    digest = build_digest([listing], SearchCriteria(location="Pozuelo de Alarcón"))

    assert "875.000 \u20ac" in digest.message, "miles con punto, no con coma"
    assert "210 m\u00b2" in digest.message
    assert "4 hab." in digest.message
    assert "Idealista" in digest.message
    assert "Chalet con piscina" in digest.message
    assert listing.listing.url in digest.message


def test_html_digest_uses_only_the_first_https_photo_and_escapes_content() -> None:
    listing = scored(
        title='<Casa "nueva"> & piscina',
        url="https://homes.example/1?a=1&b=2",
        photos=("https://img.example/main.jpg", "https://img.example/other.jpg"),
    )

    digest = build_digest([listing], SearchCriteria(location="Madrid"))

    assert digest.html_message is not None
    assert 'src="https://img.example/main.jpg"' in digest.html_message
    assert "other.jpg" not in digest.html_message
    assert "&lt;Casa &quot;nueva&quot;&gt; &amp; piscina" in digest.html_message
    assert "https://homes.example/1?a=1&amp;b=2" in digest.html_message
    assert "<Casa" in digest.message, "el mensaje de texto existente no cambia"


def test_html_digest_omits_non_https_photo_but_keeps_listing_details() -> None:
    listing = scored(photos=("http://img.example/main.jpg",))

    digest = build_digest([listing], SearchCriteria(location="Madrid"))

    assert digest.html_message is not None
    assert "<img " not in digest.html_message
    assert "Piso luminoso en Chamberí" in digest.html_message
    assert listing.listing.url in digest.message


def test_the_title_switches_to_singular_for_one_listing() -> None:
    digest = build_digest([scored()], SearchCriteria(location="Madrid"))

    assert digest.title == "1 vivienda nueva \u2014 Madrid"


def test_the_same_url_on_another_portal_is_a_new_listing() -> None:
    url = "https://www.idealista.com/inmueble/1/"
    previous = result(scored(url=url))
    current = result(scored(url=url, portal=Portal.FOTOCASA))

    assert select_new_listings(current, previous) == current.listings


def test_without_a_previous_run_everything_is_new() -> None:
    current = result(scored(url="https://p/1"), scored(url="https://p/2"))

    assert select_new_listings(current, None) == current.listings
