"""Tests del repositorio en ficheros JSON (persistencia y reinicio)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.domain.models import Portal, ScoredListing, SearchCriteria, SearchResult, Trigger
from app.infrastructure.repositories.json_file_repository import JsonFileListingRepository
from app.tests.conftest import RecordingLogger, make_listing


def build_result(
    search_id: UUID | None = None,
    *,
    run_date: date = date(2026, 11, 5),
    fingerprint: str = "fp-1",
    trigger: Trigger = Trigger.API,
) -> SearchResult:
    first = make_listing("https://www.idealista.com/inmueble/1/", price=875000.0, size=210.0, rooms=4)
    second = make_listing(
        "https://www.idealista.com/inmueble/2/",
        price=899000.0,
        size=244.0,
        rooms=4,
        portal=Portal.FOTOCASA,
    )
    return SearchResult(
        search_id=search_id or uuid4(),
        criteria=SearchCriteria(
            location="Pozuelo de Alarcón",
            max_price_eur=900000.0,
            min_rooms=4,
            preferences="chalet con piscina",
        ),
        criteria_fingerprint=fingerprint,
        run_date=run_date,
        created_at=datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc),
        trigger=trigger,
        listings=(
            ScoredListing(first, 0.97, "Chalet con piscina"),
            ScoredListing(second, 0.55, "Sin piscina"),
        ),
        scraped_count=12,
        discarded_count=10,
    )


def repository(tmp_path: Path) -> JsonFileListingRepository:
    return JsonFileListingRepository(tmp_path / "results", logger=RecordingLogger())


async def test_save_writes_one_json_file_per_search(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    result = build_result()

    await repo.save(result)

    path = tmp_path / "results" / f"{result.search_id}.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["search_id"] == str(result.search_id)
    assert payload["matched_count"] == 2
    assert payload["scraped_count"] == 12


async def test_persisted_file_contains_listing_urls(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    result = build_result()

    await repo.save(result)

    payload = json.loads((tmp_path / "results" / f"{result.search_id}.json").read_text("utf-8"))
    urls = [item["url"] for item in payload["listings"]]
    assert urls == [
        "https://www.idealista.com/inmueble/1/",
        "https://www.idealista.com/inmueble/2/",
    ]
    assert payload["listings"][0]["relevance"] == 0.97
    assert payload["listings"][0]["portal"] == "idealista"


async def test_raw_payload_is_not_persisted(tmp_path: Path) -> None:
    """``raw`` puede contener datos de contacto: no debe acabar en disco."""
    repo = repository(tmp_path)
    result = build_result()
    listing = make_listing()
    result = SearchResult(
        search_id=result.search_id,
        criteria=result.criteria,
        criteria_fingerprint=result.criteria_fingerprint,
        run_date=result.run_date,
        created_at=result.created_at,
        trigger=result.trigger,
        listings=(ScoredListing(listing, 0.9, "ok"),),
        scraped_count=1,
        discarded_count=0,
    )

    await repo.save(result)

    text = (tmp_path / "results" / f"{result.search_id}.json").read_text("utf-8")
    assert "raw" not in text


async def test_get_by_id_returns_none_for_unknown_id(tmp_path: Path) -> None:
    assert await repository(tmp_path).get_by_id(uuid4()) is None


async def test_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    result = build_result()

    await repo.save(result)
    restored = await repo.get_by_id(result.search_id)

    assert restored is not None
    assert restored.search_id == result.search_id
    assert restored.run_date == result.run_date
    assert restored.created_at == result.created_at
    assert restored.trigger is result.trigger
    assert restored.criteria == result.criteria
    assert restored.criteria_fingerprint == result.criteria_fingerprint
    assert restored.scraped_count == 12
    assert restored.discarded_count == 10
    assert [item.listing.url for item in restored.listings] == [
        item.listing.url for item in result.listings
    ]
    assert restored.listings[0].relevance == 0.97
    assert restored.listings[0].listing.price_per_m2 == result.listings[0].listing.price_per_m2


async def test_results_survive_a_restart(tmp_path: Path) -> None:
    """Un repositorio nuevo sobre el mismo directorio recupera lo guardado."""
    first = repository(tmp_path)
    result = build_result()
    await first.save(result)

    second = repository(tmp_path)  # simula reinicio del proceso
    restored = await second.get_by_id(result.search_id)

    assert restored is not None
    assert restored.search_id == result.search_id


async def test_daily_idempotency_survives_a_restart(tmp_path: Path) -> None:
    """El job diario no debe repetir una búsqueda ya hecha antes del reinicio."""
    first = repository(tmp_path)
    result = build_result(fingerprint="fp-daily", trigger=Trigger.SCHEDULER)
    await first.save(result)

    second = repository(tmp_path)
    found = await second.find_by_criteria_and_day("fp-daily", date(2026, 11, 5))

    assert found is not None
    assert found.search_id == result.search_id
    assert await second.find_by_criteria_and_day("fp-daily", date(2026, 11, 6)) is None


async def test_manual_result_does_not_block_daily_idempotency_after_restart(
    tmp_path: Path,
) -> None:
    first = repository(tmp_path)
    manual = build_result(fingerprint="fp-daily", trigger=Trigger.PROFILE)
    await first.save(manual)

    second = repository(tmp_path)
    assert await second.find_by_criteria_and_day("fp-daily", manual.run_date) is None

    scheduled = build_result(fingerprint="fp-daily", trigger=Trigger.SCHEDULER)
    await second.save(scheduled)
    found = await second.find_by_criteria_and_day("fp-daily", scheduled.run_date)
    assert found is not None
    assert found.search_id == scheduled.search_id


async def test_saving_same_search_id_is_idempotent(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    search_id = uuid4()

    await repo.save(build_result(search_id))
    await repo.save(build_result(search_id))

    files = list((tmp_path / "results").glob("*.json"))
    assert len(files) == 1


async def test_corrupt_file_does_not_prevent_startup(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "roto.json").write_text("{no es json", encoding="utf-8")
    valid = build_result()
    (results_dir / f"{valid.search_id}.json").write_text(
        json.dumps(_payload_of(valid)), encoding="utf-8"
    )

    repo = JsonFileListingRepository(results_dir, logger=RecordingLogger())

    assert await repo.get_by_id(valid.search_id) is not None


def _payload_of(result: SearchResult) -> dict:
    """Payload mínimo válido para el índice (evita duplicar el serializador)."""
    return {
        "search_id": str(result.search_id),
        "criteria_fingerprint": result.criteria_fingerprint,
        "run_date": result.run_date.isoformat(),
        "created_at": result.created_at.isoformat(),
        "trigger": result.trigger.value,
        "scraped_count": 1,
        "discarded_count": 0,
        "matched_count": 0,
        "criteria": {
            "location": "Madrid",
            "portals": ["idealista"],
            "min_price_eur": None,
            "max_price_eur": None,
            "min_size_m2": None,
            "min_rooms": None,
            "max_results": 40,
            "preferences": None,
        },
        "listings": [],
    }


async def test_list_recent_orders_by_modification_time(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    first = build_result(fingerprint="a")
    second = build_result(fingerprint="b")
    await repo.save(first)
    await repo.save(second)

    recent = await repo.list_recent(limit=10)

    assert {item.search_id for item in recent} == {first.search_id, second.search_id}
