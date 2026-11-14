"""Tests unitarios de la búsqueda diaria programada (idempotencia y errores)."""

from __future__ import annotations

from datetime import date

import pytest

from app.application.services.scheduled_search import ScheduledSearchService
from app.application.services.search_service import SearchService
from app.domain.exceptions import ScraperException
from app.domain.models import SearchCriteria, Trigger
from app.infrastructure.repositories.memory_repository import InMemoryListingRepository
from app.tests.conftest import (
    ExplodingRepository,
    FakeClassifier,
    FakeNotifier,
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
    notifier: FakeNotifier | None = None,
    alert_click_url: str | None = None,
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
        notifier=notifier,
        alert_click_url=alert_click_url,
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


async def test_manual_search_does_not_suppress_daily_job() -> None:
    clock = MutableClock(date(2026, 11, 4))
    repository = InMemoryListingRepository(logger=RecordingLogger())
    scraper = FakeScraper([make_listing()])
    search_service = SearchService(
        scraper,
        FakeClassifier(),
        repository,
        logger=RecordingLogger(),
        today=clock,
    )
    criteria = SearchCriteria(location="Madrid")

    await search_service.run_search(criteria, trigger=Trigger.PROFILE.value)
    scheduled = ScheduledSearchService(
        search_service,
        repository,
        criteria,
        today=clock,
    )
    result = await scheduled.run_daily_job()

    assert result is not None
    assert result.trigger is Trigger.SCHEDULER
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


async def test_first_run_alerts_every_listing() -> None:
    notifier = FakeNotifier()
    service, _, _ = build(
        scraper=FakeScraper([make_listing(url="https://p/1"), make_listing(url="https://p/2")]),
        notifier=notifier,
    )

    await service.run_daily_job()

    assert len(notifier.digests) == 1, "una sola notificación resumen"
    digest = notifier.digests[0]
    assert digest["title"] == "2 viviendas nuevas \u2014 Madrid"
    assert "https://p/1" in digest["message"]
    assert "https://p/2" in digest["message"]
    assert digest["click_url"] == "https://p/1"


async def test_daily_job_alerts_only_listings_absent_from_the_previous_run() -> None:
    clock = MutableClock(date(2026, 11, 4))
    notifier = FakeNotifier()
    scraper = FakeScraper(
        [make_listing(url="https://p/1"), make_listing(url="https://p/2")]
    )
    service, _, _ = build(scraper=scraper, clock=clock, notifier=notifier)

    await service.run_daily_job()

    clock.current = date(2026, 11, 5)
    scraper.listings = [
        make_listing(url="https://p/1"),
        make_listing(url="https://p/3", title="Chalet nuevo"),
    ]
    await service.run_daily_job()

    assert len(notifier.digests) == 2
    digest = notifier.digests[1]
    assert digest["title"] == "1 vivienda nueva \u2014 Madrid"
    assert "https://p/3" in digest["message"]
    assert "https://p/1" not in digest["message"], "la vivienda de ayer no es nueva"
    assert digest["click_url"] == "https://p/3"


async def test_daily_job_stays_silent_when_nothing_is_new() -> None:
    clock = MutableClock(date(2026, 11, 4))
    notifier = FakeNotifier()
    service, _, logger = build(clock=clock, notifier=notifier)

    await service.run_daily_job()
    clock.current = date(2026, 11, 5)
    await service.run_daily_job()

    assert len(notifier.digests) == 1, "solo la primera mañana avisa"
    assert logger.find("listing_alert_skipped") is not None


async def test_a_manual_run_does_not_become_the_reference_for_new_listings() -> None:
    clock = MutableClock(date(2026, 11, 4))
    repository = InMemoryListingRepository(logger=RecordingLogger())
    scraper = FakeScraper([make_listing(url="https://p/1")])
    search_service = SearchService(
        scraper,
        FakeClassifier(),
        repository,
        logger=RecordingLogger(),
        today=clock,
    )
    await search_service.run_search(SearchCriteria(location="Madrid"), trigger=Trigger.PROFILE.value)

    notifier = FakeNotifier()
    scheduled = ScheduledSearchService(
        search_service,
        repository,
        SearchCriteria(location="Madrid"),
        notifier=notifier,
        logger=RecordingLogger(),
        today=clock,
    )
    await scheduled.run_daily_job()

    assert len(notifier.digests) == 1
    assert "https://p/1" in notifier.digests[0]["message"]


async def test_a_failed_alert_does_not_break_the_daily_job() -> None:
    notifier = FakeNotifier(error=RuntimeError("ntfy caído"))
    service, _, logger = build(notifier=notifier)

    result = await service.run_daily_job()

    assert result is not None, "la búsqueda ya está guardada y se devuelve igual"
    failed = logger.find("listing_alert_failed")
    assert failed is not None
    assert failed["level"] == "exception"
    assert failed["error_code"] == "NOTIFICATION_FAILURE"


async def test_configured_dashboard_url_is_the_notification_target() -> None:
    notifier = FakeNotifier()
    service, _, _ = build(notifier=notifier, alert_click_url="https://oaichousefinder.example/")

    await service.run_daily_job()

    assert notifier.digests[0]["click_url"] == "https://oaichousefinder.example/"


async def test_the_digest_caps_the_listing_blocks() -> None:
    listings = [make_listing(url=f"https://p/{index}") for index in range(15)]
    notifier = FakeNotifier()
    service, _, _ = build(scraper=FakeScraper(listings), notifier=notifier)

    await service.run_daily_job()

    digest = notifier.digests[0]
    assert digest["title"].startswith("15 viviendas nuevas")
    assert digest["message"].count("https://p/") == 10
    assert digest["message"].endswith("\u2026y 5 m\u00e1s.")
