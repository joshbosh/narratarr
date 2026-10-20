"""Audible catalogue lookup (unauthenticated public catalog API).

Searches the primary region and falls back to the secondary region when the
primary has nothing. ASIN lookups do the same — an ASIN not sold in a
marketplace comes back as a bare stub with no title, which we treat as a miss.
"""
from __future__ import annotations

import logging
import os
import re

import httpx

from .base import empty_book, html_to_text, split_role

log = logging.getLogger(__name__)

DOMAINS = {
    "us": "audible.com", "au": "audible.com.au", "uk": "audible.co.uk", "ca": "audible.ca",
    "de": "audible.de", "fr": "audible.fr", "it": "audible.it", "es": "audible.es",
    "jp": "audible.co.jp", "in": "audible.in",
}
RESPONSE_GROUPS = ",".join([
    "contributors", "media", "product_attrs", "product_desc", "product_extended_attrs",
    "series", "category_ladders", "rating",
])
ASIN_RE = re.compile(r"^(B0[0-9A-Z]{8}|\d{9}[\dX])$", re.I)
UA = "Mozilla/5.0 (Narratarr audiobook manager)"


def base_url(region: str) -> str:
    override = os.environ.get("NARRATARR_AUDIBLE_BASE_OVERRIDE")  # used by tests/mocks
    if override:
        return override.replace("{region}", region)
    return f"https://api.{DOMAINS.get(region, 'audible.com')}"


def hires_cover(url: str | None) -> str | None:
    """Strip Amazon's size modifier (._SL500_) to get the original image."""
    if not url:
        return None
    return re.sub(r"\._[A-Z0-9,_]+_(?=\.(?:jpg|jpeg|png)$)", "", url)


def normalise(p: dict, region: str) -> dict:
    b = empty_book()
    b.update({
        "asin": p.get("asin"),
        "region": region,
        "provider": "audible",
        "title": (p.get("title") or "").strip(),
        "subtitle": p.get("subtitle"),
        "authors": [{"name": split_role(a["name"])[0], "role": split_role(a["name"])[1], "asin": a.get("asin")}
                    for a in p.get("authors") or [] if a.get("name")],
        "narrators": [split_role(n["name"])[0] for n in p.get("narrators") or [] if n.get("name")],
        "series": [
            {"name": s.get("title"), "sequence": (s.get("sequence") or None), "asin": s.get("asin")}
            for s in p.get("series") or [] if s.get("title")
        ],
        "runtime_min": p.get("runtime_length_min"),
        "release_date": p.get("release_date") or p.get("issue_date"),
        "publisher": p.get("publisher_name"),
        "description": html_to_text(p.get("publisher_summary") or p.get("merchandising_summary")),
        "language": (p.get("language") or "").capitalize() or None,
        "format": (p.get("format_type") or None),
        "explicit": bool(p.get("is_adult_product")),
    })
    if b["release_date"]:
        b["year"] = str(b["release_date"])[:4]
    imgs = p.get("product_images") or {}
    cover = imgs.get("1024") or imgs.get("500") or next(iter(imgs.values()), None)
    b["cover_url"] = hires_cover(cover)
    genres: list[str] = []
    for ladder in p.get("category_ladders") or []:
        for rung in ladder.get("ladder") or []:
            name = rung.get("name")
            if name and name not in genres:
                genres.append(name)
    b["genres"] = genres[:6]
    return b


class AudibleProvider:
    name = "audible"

    def __init__(self, region: str = "au", fallback: str | None = "us", timeout: float = 15):
        self.region = region
        self.fallback = fallback if fallback and fallback != region else None
        self.timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": UA}, follow_redirects=True)

    async def _search_region(self, client: httpx.AsyncClient, region: str, query: str, limit: int) -> list[dict]:
        params = {
            "num_results": min(max(limit, 1), 50),
            "products_sort_by": "Relevance",
            "response_groups": RESPONSE_GROUPS,
            "image_sizes": "500,1024",
            "keywords": query,
        }
        r = await client.get(f"{base_url(region)}/1.0/catalog/products", params=params)
        r.raise_for_status()
        out = []
        for p in r.json().get("products", []):
            # skip podcasts / periodicals — not audiobooks
            if p.get("content_delivery_type") in ("PodcastParent", "PodcastEpisode", "Periodical"):
                continue
            if not p.get("title"):
                continue
            out.append(normalise(p, region))
        return out

    async def search(self, query: str, limit: int = 25) -> list[dict]:
        query = query.strip()
        if ASIN_RE.match(query):
            book = await self.get(query)
            return [book] if book else []
        async with self._client() as client:
            results = await self._search_region(client, self.region, query, limit)
            if not results and self.fallback:
                results = await self._search_region(client, self.fallback, query, limit)
            return results

    async def get(self, identifier: str, region: str | None = None) -> dict | None:
        regions = [region] if region else [self.region] + ([self.fallback] if self.fallback else [])
        async with self._client() as client:
            for reg in regions:
                try:
                    r = await client.get(
                        f"{base_url(reg)}/1.0/catalog/products/{identifier}",
                        params={"response_groups": RESPONSE_GROUPS, "image_sizes": "500,1024"},
                    )
                    if r.status_code == 404:
                        continue
                    r.raise_for_status()
                    product = r.json().get("product") or {}
                    if product.get("title"):
                        return normalise(product, reg)
                except httpx.HTTPError as e:
                    log.warning("Audible %s lookup for %s failed: %s", reg, identifier, e)
        return None
