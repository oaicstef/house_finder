"""Prueba de humo del scraper, portal a portal, sin pasar por la API.

Sirve para el *spike* inicial y para diagnosticar un portal caído en producción:

    python -m app.tools.scraper_probe --portal fotocasa --location Madrid
    python -m app.tools.scraper_probe --portal idealista --location Madrid --max-items 5

Usa la misma configuración que la aplicación (``.env`` incluido) y el mismo
``ScraperPort`` que atiende ``POST /api/v1/searches``; no escribe resultados.
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import Settings
from app.domain.exceptions import ConfigurationException, DomainException
from app.domain.models import Portal, SearchCriteria
from app.infrastructure.logging_config import configure_logging
from app.infrastructure.scrapers.factory import build_scraper

__all__ = ["main"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prueba de humo del scraper por portal")
    parser.add_argument("--portal", choices=[portal.value for portal in Portal], default="fotocasa")
    parser.add_argument("--location", default="Madrid", help="Texto libre del portal")
    parser.add_argument("--max-items", type=int, default=5)
    parser.add_argument("--max-price", type=float, default=None)
    parser.add_argument("--min-size", type=float, default=None)
    parser.add_argument("--min-rooms", type=int, default=None)
    parser.add_argument(
        "--operation",
        choices=["rent", "sale"],
        default=None,
        help="Por defecto, SEARCH_OPERATION del entorno",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    settings = Settings(environment="test")
    if args.operation:
        settings = settings.model_copy(update={"search_operation": args.operation})
    configure_logging(settings)

    portal = Portal.parse(args.portal)
    provider = settings.scraper_providers_map.get(portal, "none")
    print(f"portal={portal.value} provider={provider} location={args.location!r}")
    if provider == "none":
        print("el portal está desactivado en SCRAPER_PROVIDERS")
        return 1
    # Solo se prueba el portal pedido: no debe exigir credenciales de los demás.
    settings = settings.model_copy(update={"scraper_providers": f"{portal.value}={provider}"})

    scraper = build_scraper(settings)
    criteria = SearchCriteria(
        location=args.location,
        portals=(portal,),
        max_results=max(1, args.max_items),
        max_price_eur=args.max_price,
        min_size_m2=args.min_size,
        min_rooms=args.min_rooms,
    )
    try:
        listings = await scraper.fetch_listings(criteria)
    finally:
        close = getattr(scraper, "aclose", None)
        if close is not None:
            await close()

    print(f"resultado: {len(listings)} anuncios")
    for listing in listings[: args.max_items]:
        print(
            f"- {listing.portal.value}: {listing.price_eur:,.0f} EUR"
            f" | {listing.size_m2 or '?'} m2 | {listing.rooms or '?'} hab"
            f" | {listing.location} | fotos={len(listing.photos)}"
        )
        print(f"  {listing.url}")
    return 0 if listings else 1


def main() -> int:
    try:
        return asyncio.run(_run(_parse_args()))
    except ConfigurationException as exc:
        print(f"configuración inválida: {exc.message} {exc.details or ''}".rstrip())
        return 2
    except DomainException as exc:
        print(f"el scraping falló [{exc.code}]: {exc.message} {exc.details or ''}".rstrip())
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
