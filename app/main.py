"""Punto de entrada de la aplicación.

Arranca FastAPI con el ``lifespan`` que construye el contenedor de dependencias y
gestiona el ciclo de vida de APScheduler. Ejecutar con:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

#: UI estática servida por el propio backend (sin build ni CDN).
WEB_DIR = Path(__file__).resolve().parent / "web"

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
            close_scraper = getattr(services.scraper, "aclose", None)
            if close_scraper is not None:
                await close_scraper()
            logger.info("app_shutdown", environment=settings.environment)

    application = FastAPI(
        title="Agente de búsqueda inmobiliaria personalizada",
        description=(
            "Scraping de Idealista y Fotocasa con proveedor intercambiable (HTTP propio "
            "o actores de Apify), clasificación semántica con DeepSeek y búsqueda diaria "
            "programada. Arquitectura hexagonal."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(RequestContextMiddleware)
    register_exception_handlers(application)
    application.include_router(router)
    application.include_router(health_router)
    _mount_web_ui(application)
    return application


def _mount_web_ui(application: FastAPI) -> None:
    """Sirve la UI estática (``app/web``) y su página índice."""
    index = WEB_DIR / "index.html"
    if not index.exists():  # pragma: no cover - defensa ante empaquetados incompletos
        logger.warning("web_ui_missing", error_code=None, directory=str(WEB_DIR))
        return
    application.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @application.get("/", include_in_schema=False)
    async def index_page() -> FileResponse:
        return FileResponse(index, media_type="text/html; charset=utf-8")


app = create_app()
