"""Alertas de viviendas nuevas para el job diario.

La comparación es contra la última ejecución *programada* del mismo perfil: un
anuncio es nuevo cuando su ``(portal, url)`` no aparecía en ese resultado previo.
El mensaje es un único resumen (digest) con las viviendas nuevas, listo para el
notificador push.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Sequence
from urllib.parse import urlsplit

from app.domain.models import ScoredListing, SearchCriteria, SearchResult

__all__ = [
    "DIGEST_MAX_LISTINGS",
    "ListingDigest",
    "build_digest",
    "select_new_listings",
]

#: Viviendas que caben en el mensaje; el resto se resume en una línea final.
DIGEST_MAX_LISTINGS = 10

_PORTAL_LABELS = {"idealista": "Idealista", "fotocasa": "Fotocasa"}


@dataclass(frozen=True, slots=True)
class ListingDigest:
    """Mensaje resumen listo para enviar."""

    title: str
    message: str
    click_url: str | None = None
    html_message: str | None = None


def _key(scored: ScoredListing) -> tuple[str, str]:
    return (scored.listing.portal.value, scored.listing.url)


def select_new_listings(
    current: SearchResult,
    previous: SearchResult | None,
) -> tuple[ScoredListing, ...]:
    """Anuncios de ``current`` que no estaban en ``previous``.

    Sin ejecución previa todo es nuevo: la primera mañana no debe quedarse muda.
    """
    if previous is None:
        return current.listings
    seen = {_key(scored) for scored in previous.listings}
    return tuple(scored for scored in current.listings if _key(scored) not in seen)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _details(scored: ScoredListing) -> str:
    listing = scored.listing
    parts: list[str] = []
    if listing.price_eur:
        parts.append(f"{listing.price_eur:,.0f} \u20ac".replace(",", "."))
    if listing.size_m2:
        parts.append(f"{listing.size_m2:g} m\u00b2")
    if listing.rooms:
        parts.append(f"{listing.rooms} hab.")
    parts.append(_PORTAL_LABELS.get(listing.portal.value, listing.portal.value))
    return " \u00b7 ".join(parts)


def _describe(scored: ScoredListing) -> str:
    listing = scored.listing
    return f"{_details(scored)}\n{listing.title}\n{listing.url}"


def _https_url(value: str) -> str | None:
    """URL segura para atributos HTML que solo admite HTTPS sin credenciales."""
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value.strip()


def _describe_html(scored: ScoredListing, index: int) -> str:
    listing = scored.listing
    title = escape(listing.title)
    listing_url = _https_url(listing.url)
    image_url = _https_url(listing.photos[0]) if listing.photos else None
    if listing_url:
        title = f'<a href="{escape(listing_url, quote=True)}">{title}</a>'
        link = f'<p><a href="{escape(listing_url, quote=True)}">Ver anuncio</a></p>'
    else:
        link = ""

    image = ""
    if image_url:
        image = (
            f'<p><img src="{escape(image_url, quote=True)}" '
            f'alt="Foto principal de {escape(listing.title, quote=True)}" '
            'style="display:block;max-width:100%;height:auto"></p>'
        )

    return (
        f"<section><p>{index}. <strong>{title}</strong></p>"
        f"{image}<p>{escape(_details(scored))}</p>{link}</section>"
    )


def build_digest(
    new_listings: Sequence[ScoredListing],
    criteria: SearchCriteria,
    *,
    dashboard_url: str | None = None,
    max_listings: int = DIGEST_MAX_LISTINGS,
) -> ListingDigest:
    """Resumen push con las viviendas nuevas (una sola notificación)."""
    total = len(new_listings)
    location = criteria.location or "tu b\u00fasqueda"
    title = f"{total} {_plural(total, 'vivienda nueva', 'viviendas nuevas')} \u2014 {location}"

    shown = list(new_listings[: max(1, max_listings)])
    blocks = [
        f"{index}. {_describe(scored)}"
        for index, scored in enumerate(shown, start=1)
    ]
    html_blocks = [
        _describe_html(scored, index)
        for index, scored in enumerate(shown, start=1)
    ]
    hidden = total - len(shown)
    if hidden > 0:
        blocks.append(f"\u2026y {hidden} m\u00e1s.")
        html_blocks.append(f"<p>&hellip;y {hidden} m&aacute;s.</p>")

    click_url = (dashboard_url or "").strip() or (shown[0].listing.url if shown else None)
    html_message = "<!doctype html><html><body>"
    html_message += "".join(html_blocks)
    html_message += "</body></html>"
    return ListingDigest(
        title=title,
        message="\n\n".join(blocks),
        click_url=click_url,
        html_message=html_message,
    )
