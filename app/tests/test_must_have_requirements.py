"""Tests de los requisitos obligatorios (``must_have``)."""

from __future__ import annotations

from datetime import date

import pytest

from app.api.schemas import SearchCreateRequest
from app.application.services.search_service import SearchService
from app.domain.models import Listing, Portal, SearchCriteria, normalize_text
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.tests.conftest import FakeClassifier, FakeScraper, RecordingLogger


def build_service(
    scraper: FakeScraper,
    classifier: FakeClassifier,
    repository: InMemoryListingRepository,
) -> tuple[SearchService, RecordingLogger]:
    logger = RecordingLogger()
    service = SearchService(
        scraper, classifier, repository, logger=logger, today=lambda: date(2026, 11, 5)
    )
    return service, logger


def chalet_with_padel(url: str = "https://www.idealista.com/inmueble/padel/") -> Listing:
    return Listing(
        portal=Portal.IDEALISTA,
        url=url,
        title="Chalet adosado en Pau de Carabanchel",
        price_eur=880000.0,
        location="Madrid",
        size_m2=225.0,
        rooms=4,
        bathrooms=3,
        description="Zona común con dos piscinas y pista pádel para hacer deporte.",
        amenities=("swimming_pool",),
    )


def chalet_without_padel() -> Listing:
    return Listing(
        portal=Portal.IDEALISTA,
        url="https://www.idealista.com/inmueble/sin-padel/",
        title="Chalet pareado en Tres Olivos",
        price_eur=880000.0,
        location="Madrid",
        size_m2=267.0,
        rooms=4,
        bathrooms=3,
        description="Chalet con piscina privada y jardín.",
        amenities=("swimming_pool", "garden"),
    )


# --- Dominio -----------------------------------------------------------------


def test_normalize_text_strips_accents_and_case() -> None:
    assert normalize_text("Pista PÁDEL") == "pista padel"


@pytest.mark.parametrize("term", ["padel", "pádel", "PADEL"])
def test_satisfies_all_matches_accent_variants(term: str) -> None:
    assert chalet_with_padel().satisfies_all((term,)) is True


def test_satisfies_all_fails_when_term_is_absent() -> None:
    assert chalet_without_padel().satisfies_all(("padel",)) is False


def test_satisfies_all_uses_amenity_labels_for_pool() -> None:
    """`swimming_pool` cuenta como "piscina" aunque la descripción no lo diga."""
    listing = Listing(
        portal=Portal.IDEALISTA,
        url="https://x.test/1",
        title="Chalet",
        price_eur=100.0,
        location="Madrid",
        amenities=("swimming_pool",),
    )

    assert listing.satisfies_all(("piscina",)) is True


def test_satisfies_all_requires_every_term() -> None:
    """Semántica AND: 'chalet' y 'piscina' sí, 'padel' no -> False."""
    assert chalet_without_padel().satisfies_all(("chalet", "piscina")) is True
    assert chalet_without_padel().satisfies_all(("chalet", "piscina", "padel")) is False


def test_satisfies_all_without_terms_accepts_everything() -> None:
    assert chalet_without_padel().satisfies_all(()) is True


def test_must_have_changes_the_criteria_fingerprint() -> None:
    base = SearchCriteria(location="Madrid")
    with_padel = SearchCriteria(location="Madrid", must_have=("padel",))

    assert base.fingerprint() != with_padel.fingerprint()


def test_must_have_fingerprint_is_order_insensitive() -> None:
    first = SearchCriteria(location="Madrid", must_have=("padel", "piscina"))
    second = SearchCriteria(location="Madrid", must_have=("piscina", "PÁDEL"))

    assert first.fingerprint() == second.fingerprint()


# --- Servicio ----------------------------------------------------------------


async def test_service_discards_listings_missing_a_required_term() -> None:
    good = chalet_with_padel()
    bad = chalet_without_padel()
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, logger = build_service(FakeScraper([good, bad]), FakeClassifier(), repository)

    result = await service.run_search(
        SearchCriteria(location="Madrid", must_have=("padel", "piscina"))
    )

    assert [item.listing.url for item in result.listings] == [good.url]
    assert result.scraped_count == 2
    assert result.discarded_count == 1
    filtered = logger.find("listings_filtered_by_requirements")
    assert filtered is not None
    assert filtered["kept_count"] == 1
    assert filtered["dropped_count"] == 1


async def test_service_skips_llm_when_requirements_filter_everything() -> None:
    classifier = FakeClassifier()
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, _ = build_service(FakeScraper([chalet_without_padel()]), classifier, repository)

    result = await service.run_search(SearchCriteria(location="Madrid", must_have=("padel",)))

    assert result.listings == ()
    assert result.matched_count == 0
    assert result.scraped_count == 1
    assert classifier.calls == []


async def test_service_without_requirements_keeps_previous_behaviour() -> None:
    repository = InMemoryListingRepository(logger=RecordingLogger())
    service, logger = build_service(
        FakeScraper([chalet_with_padel(), chalet_without_padel()]), FakeClassifier(), repository
    )

    result = await service.run_search(SearchCriteria(location="Madrid"))

    assert result.matched_count == 2
    assert logger.find("listings_filtered_by_requirements") is None


# --- Esquema de la API --------------------------------------------------------


def test_schema_accepts_must_have_and_converts_to_domain() -> None:
    request = SearchCreateRequest.model_validate(
        {"location": "Madrid", "must_have": ["padel", "piscina"]}
    )

    assert request.to_domain().must_have == ("padel", "piscina")


def test_schema_defaults_to_no_requirements() -> None:
    assert SearchCreateRequest.model_validate({"location": "Madrid"}).to_domain().must_have == ()


def test_schema_trims_and_deduplicates_requirements() -> None:
    request = SearchCreateRequest.model_validate(
        {"location": "Madrid", "must_have": ["  padel ", "Padel", "piscina"]}
    )

    assert request.must_have == ["padel", "piscina"]


def test_schema_rejects_too_short_requirement() -> None:
    with pytest.raises(ValueError):
        SearchCreateRequest.model_validate({"location": "Madrid", "must_have": ["a"]})


def test_schema_rejects_too_many_requirements() -> None:
    with pytest.raises(ValueError):
        SearchCreateRequest.model_validate(
            {"location": "Madrid", "must_have": [f"term{index}" for index in range(9)]}
        )
