"""Tests unitarios de la búsqueda diaria programada (idempotencia y errores)."""

from __future__ import annotations

from datetime import date

import pytest

from app.application.services.scheduled_search import ScheduledSearchService
from app.application.services.search_service import SearchService
from app.domain.exceptions import ScraperException
from app.domain.models import SearchCriteria
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.tests.conftest import (
    ExplodingRepository,
    FakeClassifier,
    FakeScraper,
    RecordingLogger,
    make_listing,
)


class MutableClock:
    def __init__(self, current: date) -> None:
        self.current = current

    def __call__(self) -> date:
        return self.current


def build(
    *,
    scraper: FakeScraper | None = None,
    repository: InMemoryListingRepository | None = None,
    clock: MutableClock | None = None,
) -> tuple[ScheduledSearchService, FakeScraper, RecordingLogger]:
    clock = clock or MutableClock(date(2026, 11, 4))
    repository = repository or InMemoryListingRepository(logger=RecordingLogger())
    scraper = scraper or FakeScraper([make_listing()])
    search_service = SearchService(
        scraper,
        FakeClassifier(),
        repository,
        logger=RecordingLogger(),
        today=clock,
    )
    logger = RecordingLogger()
    service = ScheduledSearchService(
        search_service,
        repository,
        SearchCriteria(location="Madrid"),
        logger=logger,
        today=clock,
    )
    return service, scraper, logger


async def test_daily_job_runs_once_per_day_and_is_idempotent() -> None:
    service, scraper, logger = build()

    first = await service.run_daily_job()
    second = await service.run_daily_job()

    assert first is not None
    assert second is None
    assert len(scraper.calls) == 1
    duplicate = logger.find("scheduled_search_skipped_duplicate")
    assert duplicate is not None
    assert duplicate["level"] == "warning"
    assert duplicate["run_date"] == "2026-11-04"


async def test_daily_job_runs_again_on_a_new_day() -> None:
    clock = MutableClock(date(2026, 11, 4))
    service, scraper, _ = build(clock=clock)

    assert await service.run_daily_job() is not None
    clock.current = date(2026, 11, 5)
    assert await service.run_daily_job() is not None
    assert len(scraper.calls) == 2


async def test_daily_job_reraises_domain_error_for_manual_invocation() -> None:
    service, _, logger = build(scraper=FakeScraper(error=ScraperException("Apify caído")))

    with pytest.raises(ScraperException):
        await service.run_daily_job(raise_on_error=True)

    failed = logger.find("scheduled_search_failed")
    assert failed is not None
    assert failed["error_code"] == "SCRAPER_FAILURE"


async def test_scheduler_entrypoint_never_propagates_domain_errors() -> None:
    service, _, logger = build(scraper=FakeScraper(error=ScraperException("Apify caído")))

    await service.scheduler_entrypoint()

    assert "scheduled_search_failed" in logger.event_names()


async def test_scheduler_entrypoint_swallows_unexpected_errors() -> None:
    service, _, logger = build(repository=ExplodingRepository(logger=RecordingLogger()))

    await service.scheduler_entrypoint()

    unexpected = logger.find("scheduled_search_entrypoint_failure")
    assert unexpected is not None
    assert unexpected["level"] == "exception"
