"""Tests del adaptador de scraping Apify (cliente falso, sin red)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable

import pytest

from app.domain.exceptions import ScraperException
from app.domain.models import Portal, SearchCriteria
from app.infrastructure.scrapers.apify_scraper import ApifyListingScraper, _to_float
from app.tests.conftest import RecordingLogger

_UNSET = object()


class _FakeDataset:
    def __init__(self, items: Iterable[dict[str, Any]]) -> None:
        self._items = list(items)

    def iterate_items(self) -> Iterable[dict[str, Any]]:
        return iter(self._items)


class _FakeRun:
    """Imita el modelo ``Run`` de apify-client v3 (atributos, no dict)."""

    def __init__(self, dataset_id: str | None, status: str = "SUCCEEDED") -> None:
        self.default_dataset_id = dataset_id
        self.status = status


class _FakeActorHandle:
    def __init__(self, client: "_FakeClient", actor_id: str) -> None:
        self._client = client
        self._actor_id = actor_id

    def call(self, **kwargs: Any) -> Any:
        self._client.calls.append({"actor_id": self._actor_id, **kwargs})
        if self._client.error is not None:
            raise self._client.error
        return self._client.run_result


class _FakeClient:
    def __init__(
        self,
        items: Iterable[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        run_result: Any = _UNSET,
    ) -> None:
        self.items = list(items or [])
        self.error = error
        # Por defecto, un Run v3 con dataset utilizable.
        self.run_result = _FakeRun("ds-1") if run_result is _UNSET else run_result
        self.calls: list[dict[str, Any]] = []
        self.dataset_ids: list[str] = []

    def actor(self, actor_id: str) -> _FakeActorHandle:
        return _FakeActorHandle(self, actor_id)

    def dataset(self, dataset_id: str) -> _FakeDataset:
        self.dataset_ids.append(dataset_id)
        return _FakeDataset(self.items)


RAW_ITEM = {
    "url": "https://www.idealista.com/inmueble/123/",
    "title": "Piso en Chamberí",
    "price": "1.250 €",
    "size": "80 m2",
    "rooms": "2",
    "bathrooms": 1,
    "location": "Chamberí, Madrid",
    "description": "Exterior con ascensor",
}

#: Item con la forma real del actor ``igolaizola/idealista-scraper`` (verificado en vivo).
REAL_ACTOR_ITEM = {
    "url": "https://www.idealista.com/inmueble/112992665/",
    "price": "625000",
    "size": "92",
    "rooms": "3",
    "bathrooms": "1",
    "address": "Piso en Calle del General Palanca, Chopera, Madrid",
    "description": "Piso exterior con ascensor",
    "propertyType": "flat",
    "suggestedTexts": {"title": "Piso en Calle del General Palanca, Chopera, Madrid"},
    "features": {
        "hasAirConditioning": False,
        "hasBoxRoom": False,
        "hasGarden": False,
        "hasSwimmingPool": False,
        "hasTerrace": True,
    },
}


#: Item con la forma real del actor ``igolaizola/fotocasa-scraper`` (verificado en vivo).
#: El precio va anidado en ``transaction.price`` y ``location`` es un objeto de niveles;
#: los datos de contacto de la agencia se omiten a propósito.
REAL_FOTOCASA_ITEM = {
    "propertyId": "191310510",
    "url": "https://www.fotocasa.es/es/comprar/vivienda/madrid-capital/ventas/191310510/d",
    "transaction": {"type": "SALE", "price": 70000, "priceDrop": None},
    "surface": 120,
    "rooms": 5,
    "baths": 3,
    "street": "Calle Montejurra",
    "number": "33",
    "description": "Chalet con piscina y pista de padel en urbanizacion",
    "features": [],
    "dynamicFeatures": ["IS_EXTERIOR"],
    "location": {
        "countryName": "España",
        "level1Name": "Madrid",
        "level2Name": "Madrid",
        "level5Name": "Madrid",
        "level7Name": "Ciudad Lineal",
        "level8Name": "Ventas",
    },
}


def build_scraper(client: _FakeClient, **overrides: Any) -> ApifyListingScraper:
    params: dict[str, Any] = {
        "token": "test-token",
        "actor_ids": {Portal.IDEALISTA: "actor-idealista"},
        "max_items": 40,
        "timeout_seconds": 120,
        "operation": "rent",
        "client_factory": lambda token: client,
        "logger": RecordingLogger(),
    }
    params.update(overrides)
    return ApifyListingScraper(**params)

def idealista_criteria(**overrides: Any) -> SearchCriteria:
    base: dict[str, Any] = {"location": "Madrid", "portals": (Portal.IDEALISTA,)}
    base.update(overrides)
    return SearchCriteria(**base)


async def test_normalizes_raw_item_to_domain_listing() -> None:
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client)

    listings = await scraper.fetch_listings(idealista_criteria())

    assert len(listings) == 1
    listing = listings[0]
    assert listing.portal is Portal.IDEALISTA
    assert listing.url == RAW_ITEM["url"]
    assert listing.price_eur == 1250.0
    assert listing.size_m2 == 80.0
    assert listing.rooms == 2
    assert listing.price_per_m2 == 15.63


async def test_builds_run_input_from_criteria() -> None:
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client)

    await scraper.fetch_listings(
        idealista_criteria(max_price_eur=900000.0, min_size_m2=200.0, min_rooms=4)
    )

    run_input = client.calls[0]["run_input"]
    assert run_input["location"] == "Madrid"
    assert run_input["operation"] == "rent"
    assert run_input["propertyType"] == "homes"
    assert run_input["country"] == "es"
    # Los actores de Idealista esperan strings, no floats.
    assert run_input["maxPrice"] == "900000"
    assert run_input["minSize"] == "200"
    # minRooms no forma parte del esquema: haría fallar la validación del actor.
    assert "minRooms" not in run_input
    # apify-client v3 usa run_timeout (timedelta), no timeout_secs.
    assert client.calls[0]["run_timeout"] == timedelta(seconds=120)
    assert "timeout_secs" not in client.calls[0]


async def test_scraper_does_not_truncate_candidates_to_max_results() -> None:
    """El recorte a `max_results` es del servicio, no del scraper.

    Si el scraper truncara, los filtros de requisitos y el clasificador nunca verían
    más anuncios que los devueltos al cliente.
    """
    items = [dict(RAW_ITEM, url=f"https://www.idealista.com/inmueble/{index}/") for index in range(10)]
    client = _FakeClient(items)
    criteria = SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,), max_results=3)

    listings = await build_scraper(client).fetch_listings(criteria)

    assert len(listings) == 10


async def test_max_items_is_not_capped_by_max_results() -> None:
    """Pedir 5 resultados no debe limitar el scraping a 5 candidatos."""
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client, max_items=40)
    criteria = SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,), max_results=5)

    await scraper.fetch_listings(criteria)

    assert client.calls[0]["run_input"]["maxItems"] == 40


async def test_max_items_respects_the_apify_cap() -> None:
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client, max_items=25)
    criteria = SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA,), max_results=100)

    await scraper.fetch_listings(criteria)

    assert client.calls[0]["run_input"]["maxItems"] == 25


async def test_run_input_omits_unset_optional_filters() -> None:
    client = _FakeClient([RAW_ITEM])

    await build_scraper(client).fetch_listings(idealista_criteria())

    run_input = client.calls[0]["run_input"]
    assert set(run_input) == {"operation", "propertyType", "country", "location", "maxItems"}


async def test_passes_max_charge_usd_when_configured() -> None:
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client, max_charge_usd=0.5)

    await scraper.fetch_listings(idealista_criteria())

    assert client.calls[0]["max_total_charge_usd"] == Decimal("0.5")


async def test_omits_max_charge_usd_when_not_configured() -> None:
    client = _FakeClient([RAW_ITEM])

    await build_scraper(client).fetch_listings(idealista_criteria())

    assert "max_total_charge_usd" not in client.calls[0]


async def test_reads_dataset_from_v3_run_model() -> None:
    client = _FakeClient([RAW_ITEM])

    await build_scraper(client).fetch_listings(idealista_criteria())

    assert client.dataset_ids == ["ds-1"]


async def test_reads_dataset_from_legacy_dict_run() -> None:
    client = _FakeClient([RAW_ITEM], run_result={"defaultDatasetId": "ds-legacy"})

    listings = await build_scraper(client).fetch_listings(idealista_criteria())

    assert len(listings) == 1
    assert client.dataset_ids == ["ds-legacy"]


async def test_raises_when_run_status_is_failed() -> None:
    client = _FakeClient([RAW_ITEM], run_result=_FakeRun("ds-1", status="FAILED"))
    scraper = build_scraper(client)

    with pytest.raises(ScraperException) as exc_info:
        await scraper.fetch_listings(idealista_criteria())

    assert exc_info.value.details["status"] == "FAILED"


async def test_raises_when_run_is_none() -> None:
    scraper = build_scraper(_FakeClient([RAW_ITEM], run_result=None))

    with pytest.raises(ScraperException):
        await scraper.fetch_listings(idealista_criteria())


async def test_filters_items_that_do_not_meet_hard_criteria() -> None:
    client = _FakeClient([RAW_ITEM])

    listings = await build_scraper(client).fetch_listings(idealista_criteria(max_price_eur=1000.0))

    assert listings == []


async def test_skips_items_without_url_or_price() -> None:
    client = _FakeClient([{"title": "sin url", "price": 900}, {"url": "https://x.test/1"}])

    assert await build_scraper(client).fetch_listings(idealista_criteria()) == []


async def test_deduplicates_urls_across_items() -> None:
    client = _FakeClient([RAW_ITEM, dict(RAW_ITEM)])

    listings = await build_scraper(client).fetch_listings(idealista_criteria())

    assert len(listings) == 1


async def test_raises_scraper_exception_when_actor_fails() -> None:
    client = _FakeClient([], error=RuntimeError("boom"))
    scraper = build_scraper(client)

    with pytest.raises(ScraperException) as exc_info:
        await scraper.fetch_listings(idealista_criteria())

    assert exc_info.value.code == "SCRAPER_FAILURE"
    assert exc_info.value.details["portal"] == "idealista"


async def test_raises_scraper_exception_when_actor_is_not_configured() -> None:
    scraper = build_scraper(_FakeClient([RAW_ITEM]), actor_ids={})

    with pytest.raises(ScraperException):
        await scraper.fetch_listings(idealista_criteria())


async def test_unconfigured_portal_is_skipped_without_failing() -> None:
    """Si solo hay actor para un portal, la búsqueda sigue con el resto."""
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client, actor_ids={Portal.IDEALISTA: "actor-idealista"})
    criteria = SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA, Portal.FOTOCASA))

    listings = await scraper.fetch_listings(criteria)

    assert len(listings) == 1


async def test_raises_when_no_portal_has_an_actor() -> None:
    scraper = build_scraper(_FakeClient([RAW_ITEM]), actor_ids={})
    criteria = SearchCriteria(location="Madrid", portals=(Portal.IDEALISTA, Portal.FOTOCASA))

    with pytest.raises(ScraperException) as exc_info:
        await scraper.fetch_listings(criteria)

    assert set(exc_info.value.details["portals"]) == {"idealista", "fotocasa"}


async def test_fotocasa_input_uses_its_own_vocabulary() -> None:
    """Fotocasa usa buy/home y precios enteros, no sale/homes con strings."""
    client = _FakeClient([RAW_ITEM])
    scraper = build_scraper(client, actor_ids={Portal.FOTOCASA: "actor-fotocasa"}, operation="sale")
    criteria = SearchCriteria(
        location="Madrid",
        portals=(Portal.FOTOCASA,),
        max_price_eur=900000.0,
        min_size_m2=200.0,
    )

    await scraper.fetch_listings(criteria)

    run_input = client.calls[0]["run_input"]
    assert run_input["operation"] == "buy"
    assert run_input["propertyType"] == "home"
    assert run_input["maxPrice"] == 900000
    assert isinstance(run_input["maxPrice"], int)
    assert "country" not in run_input


async def test_raises_scraper_exception_when_run_has_no_dataset() -> None:
    scraper = build_scraper(_FakeClient([RAW_ITEM], run_result=_FakeRun(None)))

    with pytest.raises(ScraperException):
        await scraper.fetch_listings(idealista_criteria())


async def test_normalizes_real_actor_item_shape() -> None:
    """Forma real de igolaizola/idealista-scraper: sin 'title' ni 'location' planos."""
    client = _FakeClient([REAL_ACTOR_ITEM])

    listings = await build_scraper(client).fetch_listings(idealista_criteria())

    listing = listings[0]
    assert listing.title == "Piso en Calle del General Palanca, Chopera, Madrid"
    assert listing.location.startswith("Piso en Calle del General Palanca")
    assert listing.price_eur == 625000.0
    assert listing.size_m2 == 92.0
    assert listing.rooms == 3
    assert listing.amenities == ("terrace",)


async def test_extracts_pool_and_garden_amenities() -> None:
    item = dict(REAL_ACTOR_ITEM)
    item["features"] = {"hasSwimmingPool": True, "hasGarden": True, "hasTerrace": False}
    client = _FakeClient([item])

    listings = await build_scraper(client).fetch_listings(idealista_criteria())

    assert set(listings[0].amenities) == {"swimming_pool", "garden"}


async def test_listing_without_amenity_flags_has_empty_amenities() -> None:
    client = _FakeClient([RAW_ITEM])

    listings = await build_scraper(client).fetch_listings(idealista_criteria())

    assert listings[0].amenities == ()


async def test_normalizes_real_fotocasa_item_shape() -> None:
    """Forma real de igolaizola/fotocasa-scraper: precio en ``transaction.price``."""
    client = _FakeClient([REAL_FOTOCASA_ITEM])
    scraper = build_scraper(
        client,
        actor_ids={Portal.FOTOCASA: "actor-fotocasa"},
        operation="sale",
    )

    listings = await scraper.fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.FOTOCASA,), min_size_m2=60.0)
    )

    assert len(listings) == 1
    listing = listings[0]
    assert listing.portal is Portal.FOTOCASA
    assert listing.url == REAL_FOTOCASA_ITEM["url"]
    assert listing.price_eur == 70000.0
    assert listing.size_m2 == 120.0
    assert listing.rooms == 5
    # ``baths`` no ``bathrooms``.
    assert listing.bathrooms == 3
    assert listing.title == "Calle Montejurra 33, Ventas, Ciudad Lineal, Madrid"
    # La ubicación es texto legible, no el diccionario de ids del portal.
    assert listing.location == "Ventas, Ciudad Lineal, Madrid"
    # El actor no publica comodidades: no se inventan.
    assert listing.amenities == ()


async def test_fotocasa_amenities_are_read_from_the_url_slug() -> None:
    """``features`` solo trae ids; el slug sí enumera las comodidades."""
    item = dict(
        REAL_FOTOCASA_ITEM,
        url=(
            "https://www.fotocasa.es/es/comprar/vivienda/pozuelo-de-alarcon/"
            "aire-acondicionado-calefaccion-parking-jardin-terraza-trastero-piscina/190094194/d"
        ),
        features=[{"id": "1"}, {"id": "17"}],
    )
    client = _FakeClient([item])
    scraper = build_scraper(client, actor_ids={Portal.FOTOCASA: "actor-fotocasa"})

    listings = await scraper.fetch_listings(
        SearchCriteria(location="Pozuelo de Alarcón", portals=(Portal.FOTOCASA,))
    )

    listing = listings[0]
    assert set(listing.amenities) == {
        "air_conditioning",
        "garden",
        "terrace",
        "storage_room",
        "swimming_pool",
    }
    # Los requisitos obligatorios reconocen el vocabulario de la URL.
    assert listing.satisfies_all(["piscina", "garaje"]) is False
    assert listing.satisfies_all(["piscina", "jardin"]) is True


async def test_idealista_numeric_url_adds_no_amenities() -> None:
    client = _FakeClient([REAL_ACTOR_ITEM])

    listings = await build_scraper(client).fetch_listings(idealista_criteria())

    assert listings[0].amenities == ("terrace",)


async def test_fotocasa_item_without_price_is_discarded() -> None:
    """Sin ``transaction.price`` el anuncio se descarta, no rompe la búsqueda."""
    broken = dict(REAL_FOTOCASA_ITEM, transaction={"type": "SALE", "price": None})
    client = _FakeClient([broken, REAL_FOTOCASA_ITEM])
    scraper = build_scraper(client, actor_ids={Portal.FOTOCASA: "actor-fotocasa"})

    listings = await scraper.fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.FOTOCASA,))
    )

    assert [listing.url for listing in listings] == [REAL_FOTOCASA_ITEM["url"]]


async def test_fotocasa_item_without_url_is_discarded() -> None:
    broken = {key: value for key, value in REAL_FOTOCASA_ITEM.items() if key != "url"}
    client = _FakeClient([broken])
    scraper = build_scraper(client, actor_ids={Portal.FOTOCASA: "actor-fotocasa"})

    listings = await scraper.fetch_listings(
        SearchCriteria(location="Madrid", portals=(Portal.FOTOCASA,))
    )

    assert listings == []


@pytest.mark.parametrize(
    ("raw", "expected"),    [
        ("1.250,50 €", 1250.5),
        ("1,250.50", 1250.5),
        ("1,250", 1250.0),
        ("85,5 m²", 85.5),
        ("1200", 1200.0),
        (1200, 1200.0),
        ("", None),
        (None, None),
        ("sin precio", None),
    ],
)
def test_to_float_handles_spanish_and_english_formats(raw: Any, expected: float | None) -> None:
    assert _to_float(raw) == expected
