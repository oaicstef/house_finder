"""Prueba manual E2E del digest de viviendas nuevas mediante SMTP real."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from uuid import uuid4

from app.application.services.scheduled_search import ScheduledSearchService
from app.config import Settings
from app.domain.models import (
    Listing,
    Portal,
    ScoredListing,
    SearchCriteria,
    SearchResult,
    Trigger,
)
from app.infrastructure.alerts.email_notifier import EmailNotifier


class SyntheticRepository:
    async def find_by_criteria_and_day(
        self, criteria_fingerprint: str, run_date: date
    ) -> None:
        return None

    async def find_previous_scheduled(
        self, criteria_fingerprint: str, before: date
    ) -> None:
        return None


class SyntheticSearchService:
    async def run_search(
        self, criteria: SearchCriteria, *, trigger: str = "api"
    ) -> SearchResult:
        listing = Listing(
            portal=Portal.IDEALISTA,
            url=f"https://example.invalid/house-finder-smtp-test/{uuid4()}",
            title="Anuncio sintético: prueba de integración SMTP",
            price_eur=1,
            location=criteria.location,
            size_m2=1,
            rooms=1,
            description="Mensaje de prueba; no representa una vivienda real.",
        )
        return SearchResult(
            search_id=uuid4(),
            criteria=criteria,
            criteria_fingerprint=criteria.fingerprint(),
            run_date=date.today(),
            created_at=datetime.now(timezone.utc),
            trigger=Trigger(trigger),
            listings=(ScoredListing(listing, relevance=1.0, reason="Prueba E2E"),),
            scraped_count=1,
            discarded_count=0,
        )


class DeliveryTracker:
    channel = "email"

    def __init__(self, notifier: EmailNotifier) -> None:
        self._notifier = notifier
        self.delivered = False

    async def send_digest(
        self,
        *,
        title: str,
        message: str,
        click_url: str | None = None,
    ) -> None:
        await self._notifier.send_digest(
            title=title,
            message=message,
            click_url=click_url,
        )
        self.delivered = True


async def main() -> None:
    settings = Settings(
        environment="test",
        _env_file=None,
        email_alerts_enabled=True,
    )
    notifier = EmailNotifier(
        host=settings.email_smtp_host,
        port=settings.email_smtp_port,
        sender=settings.email_from,
        recipients=settings.email_recipients,
        username=settings.email_smtp_username,
        password=settings.email_smtp_password,
        starttls=settings.email_smtp_starttls,
        timeout_seconds=settings.email_smtp_timeout_seconds,
    )
    tracker = DeliveryTracker(notifier)
    criteria = SearchCriteria(
        location="Prueba de integración SMTP",
        portals=(Portal.IDEALISTA,),
    )
    service = ScheduledSearchService(
        SyntheticSearchService(),
        SyntheticRepository(),
        criteria,
        notifiers=(tracker,),
    )

    result = await service.run_daily_job()
    if result is None or result.matched_count != 1 or not tracker.delivered:
        raise RuntimeError("El envío E2E SMTP no se completó correctamente")

    print("SMTP aceptó el digest E2E con una vivienda sintética.")


if __name__ == "__main__":
    asyncio.run(main())
