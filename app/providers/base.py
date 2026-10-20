"""Provider interface and the normalised book record.

Every provider returns the same `book` dict so the rest of the app (matching,
tagging, sidecars, the UI) never cares where metadata came from. To add a new
lookup source, subclass MetadataProvider and register it in providers/__init__.py.
"""
from __future__ import annotations

import html
import re
from typing import Any, Protocol


def empty_book() -> dict[str, Any]:
    return {
        "asin": None,
        "region": None,
        "provider": None,
        "title": "",
        "subtitle": None,
        "authors": [],        # [{"name": str, "asin": str|None}]
        "narrators": [],      # [str]
        "series": [],         # [{"name": str, "sequence": str|None, "asin": str|None}]
        "runtime_min": None,
        "release_date": None,  # YYYY-MM-DD
        "year": None,
        "publisher": None,
        "description": None,  # plain text
        "language": None,
        "format": None,       # unabridged | abridged | ...
        "genres": [],
        "cover_url": None,
        "explicit": False,
        "isbn": None,
        "chapters": None,     # [{"title": str, "start_ms": int, "length_ms": int}] | None
        "runtime_ms": None,   # precise runtime from chapter data when known
    }


class MetadataProvider(Protocol):
    name: str

    async def search(self, query: str, limit: int = 25) -> list[dict]: ...

    async def get(self, identifier: str) -> dict | None: ...


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    s = re.sub(r"(?i)<br\s*/?>", "\n", value)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = re.sub(r"(?i)</li\s*>", "\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip() or None


def primary_series(book: dict) -> dict | None:
    return book["series"][0] if book.get("series") else None


def author_names(book: dict) -> list[str]:
    return [a["name"] for a in book.get("authors", []) if a.get("name")]
