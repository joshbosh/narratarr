from __future__ import annotations

from .audible import AudibleProvider
from .audnexus import enrich
from .base import MetadataProvider


def get_provider(settings) -> MetadataProvider:
    name = settings.get("metadata.provider")
    if name == "audible":
        return AudibleProvider(settings.get("metadata.region"), settings.get("metadata.fallback_region") or None)
    raise ValueError(f"Unknown metadata provider: {name}")


async def full_metadata(settings, asin: str, region: str | None = None) -> dict | None:
    """Fetch a book by ASIN (primary region → fallback) and enrich it."""
    provider = get_provider(settings)
    book = await provider.get(asin, region) if region else await provider.get(asin)
    if book is None and region:
        book = await provider.get(asin)
    if book and settings.get("metadata.use_audnexus"):
        regions = [book.get("region"), settings.get("metadata.region"), settings.get("metadata.fallback_region")]
        seen: list[str] = []
        for r in regions:
            if r and r not in seen:
                seen.append(r)
        book = await enrich(book, settings.get("metadata.audnexus_url"), seen)
    return book
