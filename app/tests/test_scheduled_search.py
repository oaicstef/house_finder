"""Tests unitarios de la búsqueda diaria programada (idempotencia y errores)."""

from __future__ import annotations

from datetime import date

import pytest

from app.application.services.scheduled_search import ScheduledSearchService
from app.application.services.search_service import SearchService
from app.domain.exceptions import ScraperException
from app.domain.models import SearchCriteria, Trigger
from app.infrastructure.alerts.email_notification_state import (
    InMemoryEmailNotificationState,
)
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
    additional_notifiers: tuple[FakeNotifier, ...] = (),
    email_notification_state: InMemoryEmailNotificationState | None = None,
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
        notifiers=tuple(
            item for item in (notifier, *additional_notifiers) if item is not None
        ),
        email_notification_state=email_notification_state,
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


async def test_initial_email_sends_all_listings_then_only_new_ones() -> None:
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
    criteria = SearchCriteria(location="Madrid")
    await search_service.run_search(criteria, trigger=Trigger.SCHEDULER.value)

    clock.current = date(2026, 11, 5)
    scraper.listings = [
        make_listing(
            url=f"https://p/{index}",
            photos=("https://img.example/main.jpg",) if index == 1 else (),
        )
        for index in range(1, 13)
    ]
    email = FakeNotifier()
    email.channel = "email"
    ntfy = FakeNotifier()
    ntfy.channel = "ntfy"
    email_state = InMemoryEmailNotificationState()
    service = ScheduledSearchService(
        search_service,
        repository,
        criteria,
        notifiers=(ntfy, email),
        email_notification_state=email_state,
        today=clock,
        logger=RecordingLogger(),
    )

    await service.run_daily_job()

    assert len(email.digests) == 1
    assert all(
        f"https://p/{index}" in email.digests[0]["message"]
        for index in range(1, 13)
    )
    assert email.digests[0]["html_message"] is not None
    assert 'src="https://img.example/main.jpg"' in email.digests[0]["html_message"]
    assert "\nhttps://p/1\n" not in ntfy.digests[0]["message"]
    assert "\nhttps://p/12\n" not in ntfy.digests[0]["message"]
    assert await email_state.has_sent_initial_digest()

    clock.current = date(2026, 11, 6)
    scraper.listings.append(make_listing(url="https://p/13"))
    await service.run_daily_job()

    assert len(email.digests) == 2
    assert "https://p/13" in email.digests[1]["message"]
    assert "\nhttps://p/1\n" not in email.digests[1]["message"]
    assert "\nhttps://p/12\n" not in email.digests[1]["message"]


async def test_failed_initial_email_does_not_mark_the_digest_as_sent() -> None:
    email = FakeNotifier(error=RuntimeError("SMTP unavailable"))
    email.channel = "email"
    email_state = InMemoryEmailNotificationState()
    service, _, logger = build(
        notifier=email,
        email_notification_state=email_state,
    )

    await service.run_daily_job()

    assert not await email_state.has_sent_initial_digest()
    assert logger.find("listing_alert_failed") is not None


async def test_empty_initial_email_does_not_mark_the_digest_as_sent() -> None:
    email = FakeNotifier()
    email.channel = "email"
    email_state = InMemoryEmailNotificationState()
    service, _, _ = build(
        scraper=FakeScraper(),
        notifier=email,
        email_notification_state=email_state,
    )

    await service.run_daily_job()

    assert not email.digests
    assert not await email_state.has_sent_initial_digest()


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
        notifiers=(notifier,),
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


async def test_a_failed_channel_does_not_prevent_other_alert_channels() -> None:
    failed_notifier = FakeNotifier(error=RuntimeError("ntfy caído"))
    working_notifier = FakeNotifier()
    service, _, logger = build(
        notifier=failed_notifier,
        additional_notifiers=(working_notifier,),
    )

    result = await service.run_daily_job()

    assert result is not None
    assert len(working_notifier.digests) == 1
    failed = logger.find("listing_alert_failed")
    assert failed is not None
    assert failed["channel"] == "fake"


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
