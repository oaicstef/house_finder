"""Punto de entrada de la aplicación.

Arranca FastAPI con el ``lifespan`` que construye el contenedor de dependencias y
gestiona el ciclo de vida de APScheduler. Ejecutar con:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from app.api.dependencies import ServiceContainer, build_container
from app.api.exception_handlers import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import health_router, router
from app.application.services.scheduled_search import SCHEDULED_JOB_ID
from app.config import Settings, get_settings
from app.infrastructure.logging_config import configure_logging, get_logger

__all__ = ["app", "create_app"]

#: Hora local del disparo diario.
DAILY_CRON_HOUR = 7
DAILY_CRON_MINUTE = 0

logger = get_logger("app.main")


def _start_scheduler(settings: Settings, services: ServiceContainer) -> AsyncIOScheduler | None:
    """Arranca el cron diario si está habilitado."""
    if not settings.enable_scheduler:
        logger.info("scheduler_disabled", error_code=None, reason="ENABLE_SCHEDULER=false")
        return None

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        services.scheduled_search_service.scheduler_entrypoint,
        trigger=CronTrigger(
            hour=DAILY_CRON_HOUR,
            minute=DAILY_CRON_MINUTE,
            timezone=settings.timezone,
        ),
        id=SCHEDULED_JOB_ID,
        name="Búsqueda inmobiliaria diaria",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info(
        "scheduler_started",
        job_id=SCHEDULED_JOB_ID,
        cron=f"{DAILY_CRON_MINUTE} {DAILY_CRON_HOUR} * * *",
        timezone=settings.timezone,
    )
    return scheduler


def _stop_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    if scheduler is None:
        return
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("scheduler_stopped", job_id=SCHEDULED_JOB_ID)


def create_app(container: ServiceContainer | None = None) -> FastAPI:
    """Factory de la aplicación (permite inyectar un contenedor en tests)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = container.settings if container is not None else get_settings()
        configure_logging(settings)
        services = container if container is not None else build_container(settings)

        app.state.container = services
        app.state.scheduler = _start_scheduler(settings, services)
        logger.info(
            "app_startup",
            environment=settings.environment,
            scheduler_enabled=settings.enable_scheduler,
        )
        try:
            yield
        finally:
            _stop_scheduler(getattr(app.state, "scheduler", None))
            logger.info("app_shutdown", environment=settings.environment)

    application = FastAPI(
        title="Agente de búsqueda inmobiliaria personalizada",
        description=(
            "Scraping de Idealista y Fotocasa vía Apify, clasificación semántica con "
            "DeepSeek y búsqueda diaria programada. Arquitectura hexagonal."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(RequestContextMiddleware)
    register_exception_handlers(application)
    application.include_router(router)
    application.include_router(health_router)
    return application


app = create_app()
