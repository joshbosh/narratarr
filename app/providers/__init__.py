"""Metadata sources.

Audible is the primary source. Hardcover is optional (off until enabled with an API key):
when both are on, results are compared, Audible's values win, and Hardcover fills any gaps.
If Audible doesn't answer, Hardcover is used on its own.
"""
from __future__ import annotations

import asyncio
import logging

from ..matching import last_name, narrator_names, norm, primary_author, short_title
from .audible import AudibleProvider
from .audnexus import enrich
from .base import MetadataProvider
from .hardcover import HardcoverProvider

log = logging.getLogger(__name__)

# Fields Hardcover may fill when Audible has nothing for them.
FILLABLE = ("subtitle", "narrators", "series", "runtime_min", "release_date", "year", "publisher",
            "description", "language", "genres", "cover_url", "isbn")


def _empty(v) -> bool:
    return v in (None, "", [], {})


def same_book(a: dict, b: dict) -> bool:
    if a.get("asin") and b.get("asin"):
        return a["asin"].upper() == b["asin"].upper()
    if a.get("isbn") and b.get("isbn") and a["isbn"] == b["isbn"]:
        return True
    if norm(short_title(a.get("title") or "")) != norm(short_title(b.get("title") or "")):
        return False
    if last_name(primary_author(a)) != last_name(primary_author(b)):
        return False
    na = {last_name(n) for n in narrator_names(a)}
    nb = {last_name(n) for n in narrator_names(b)}
    return not na or not nb or bool(na & nb)   # different narrators = different edition


def fill_gaps(primary: dict, extra: dict) -> dict:
    filled = []
    for f in FILLABLE:
        if _empty(primary.get(f)) and not _empty(extra.get(f)):
            primary[f] = extra[f]
            filled.append(f)
    if _empty(primary.get("authors")) and extra.get("authors"):
        primary["authors"] = extra["authors"]
        filled.append("authors")
    if not primary.get("asin") and extra.get("asin"):
        primary["asin"] = extra["asin"]
    primary["hardcover_id"] = primary.get("hardcover_id") or extra.get("hardcover_id")
    primary["sources"] = sorted(set(primary.get("sources") or [primary.get("provider")]) | {extra.get("provider")})
    if filled:
        primary["filled_from"] = {**(primary.get("filled_from") or {}), **{f: extra.get("provider") for f in filled}}
    return primary


def _tag(books: list[dict]) -> list[dict]:
    for b in books:
        b.setdefault("sources", [b.get("provider")])
        b["ref"] = b.get("ref") if b.get("provider") == "hardcover" and not b.get("asin") else (b.get("asin") or b.get("ref"))
    return books


class MetadataHub:
    name = "hub"

    def __init__(self, settings):
        self.settings = settings
        self.audible = AudibleProvider(settings.get("metadata.region"), settings.get("metadata.fallback_region") or None)
        self.hardcover = (HardcoverProvider(settings.get("metadata.hardcover_token"))
                          if settings.get("metadata.hardcover_enabled") and settings.get("metadata.hardcover_token")
                          else None)

    @property
    def source_count(self) -> int:
        return 1 + (1 if self.hardcover else 0)

    async def search_ex(self, query: str, limit: int = 25) -> tuple[list[dict], dict]:
        """Returns (results, info). info = {sources: n answered, fallback: primary failed}."""
        tasks = [self.audible.search(query, limit)]
        if self.hardcover:
            tasks.append(self.hardcover.search(query, limit))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        primary, extra = res[0], (res[1] if len(res) > 1 else None)
        if isinstance(extra, BaseException):
            log.warning("Hardcover search failed: %s", extra)
            extra = None
        if isinstance(primary, BaseException):
            if extra is None:
                raise primary
            log.warning("Primary metadata source failed, using Hardcover only: %s", primary)
            return _tag(extra), {"sources": 1, "fallback": True}
        primary = _tag(primary)
        if extra is None:
            return primary, {"sources": 1, "fallback": False}
        extra = _tag(extra)
        used: set[int] = set()
        for a in primary:
            for i, h in enumerate(extra):
                if i not in used and same_book(a, h):
                    fill_gaps(a, h)
                    used.add(i)
                    break
        asins = {a["asin"].upper() for a in primary if a.get("asin")}
        tail = [h for i, h in enumerate(extra) if i not in used and not (h.get("asin") and h["asin"].upper() in asins)]
        return primary + tail, {"sources": 2, "fallback": False}

    async def search(self, query: str, limit: int = 25) -> list[dict]:
        return (await self.search_ex(query, limit))[0]

    async def get(self, identifier: str, region: str | None = None) -> dict | None:
        ident = str(identifier or "").strip()
        if ident.lower().startswith("hardcover:"):
            if not self.hardcover:
                return None
            b = await self.hardcover.get(ident)
            return _tag([b])[0] if b else None
        a = None
        try:
            a = await self.audible.get(ident, region) if region else await self.audible.get(ident)
            if a is None and region:
                a = await self.audible.get(ident)
        except Exception as e:  # noqa: BLE001
            log.warning("Primary metadata lookup for %s failed: %s", ident, e)
        h = None
        if self.hardcover:
            try:
                h = await self.hardcover.get(ident)
            except Exception as e:  # noqa: BLE001
                log.warning("Hardcover lookup for %s failed: %s", ident, e)
        if a and h:
            return fill_gaps(_tag([a])[0], h)
        found = a or h
        return _tag([found])[0] if found else None


def get_provider(settings) -> MetadataHub:
    name = settings.get("metadata.provider")
    if name != "audible":
        raise ValueError(f"Unknown metadata provider: {name}")
    return MetadataHub(settings)


async def full_metadata(settings, ident: str, region: str | None = None) -> dict | None:
    """Fetch a book by ASIN or source ref (primary → fallback region → Hardcover) and enrich it."""
    book = await get_provider(settings).get(ident, region)
    if book and book.get("asin") and settings.get("metadata.use_audnexus"):
        regions = [book.get("region"), settings.get("metadata.region"), settings.get("metadata.fallback_region")]
        seen: list[str] = []
        for r in regions:
            if r and r not in seen:
                seen.append(r)
        book = await enrich(book, settings.get("metadata.audnexus_url"), seen)
    return book
