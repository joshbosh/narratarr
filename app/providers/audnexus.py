"""Audnexus enrichment — the same source Audiobookshelf uses.

Adds genres/tags, ISBN and, most usefully, Audible's chapter list, which we can
embed into single-file books whose length lines up with the Audible release.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def enrich(book: dict, base: str, regions: list[str]) -> dict:
    asin = book.get("asin")
    if not asin:
        return book
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for region in [r for r in regions if r]:
            try:
                r = await client.get(f"{base.rstrip('/')}/books/{asin}", params={"region": region})
                if r.status_code != 200:
                    continue
                data = r.json()
                genres = [g["name"] for g in data.get("genres") or [] if g.get("type") == "genre"]
                tags = [g["name"] for g in data.get("genres") or [] if g.get("type") == "tag"]
                if genres:
                    book["genres"] = genres
                if tags:
                    book["tags"] = tags
                book["isbn"] = book.get("isbn") or data.get("isbn")
                if not book.get("description") and data.get("summary"):
                    from .base import html_to_text
                    book["description"] = html_to_text(data["summary"])
                if not book.get("cover_url") and data.get("image"):
                    book["cover_url"] = data["image"]

                c = await client.get(f"{base.rstrip('/')}/books/{asin}/chapters", params={"region": region})
                if c.status_code == 200:
                    cd = c.json()
                    chapters = [
                        {"title": ch.get("title") or f"Chapter {i + 1}",
                         "start_ms": int(ch.get("startOffsetMs") or 0),
                         "length_ms": int(ch.get("lengthMs") or 0)}
                        for i, ch in enumerate(cd.get("chapters") or [])
                    ]
                    if chapters:
                        book["chapters"] = chapters
                        book["runtime_ms"] = cd.get("runtimeLengthMs") or sum(c["length_ms"] for c in chapters)
                        book["chapters_accurate"] = bool(cd.get("isAccurate", True))
                return book
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Audnexus %s enrichment for %s failed: %s", region, asin, e)
    return book
