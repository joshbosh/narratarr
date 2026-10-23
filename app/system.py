"""Health summary for the sidebar light, and the GitHub update check."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

import httpx

from . import __version__

log = logging.getLogger(__name__)

REPO = "joshbosh/narratarr"
RELEASES_URL = f"https://github.com/{REPO}/releases"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATE_EVERY = 6 * 3600
RETRY_AFTER = 15 * 60
HEALTH_EVERY = 30


def _semver(v: str) -> tuple[int, ...] | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_release_build(version: str = __version__) -> bool:
    return _semver(version) is not None


class UpdateChecker:
    """Asks GitHub for the latest release at most every few hours. Release builds only."""

    def __init__(self, settings):
        self.settings = settings
        self.url = os.environ.get("NARRATARR_UPDATE_URL_OVERRIDE") or LATEST_API  # tests/mocks
        self.latest: dict | None = None
        self.checked_at = 0.0
        self.error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("general.check_updates")) and is_release_build()

    async def check(self, force: bool = False) -> None:
        if not self.enabled:
            return
        async with self._lock:
            if not force and time.time() - self.checked_at < UPDATE_EVERY:
                return
            self.checked_at = time.time()
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(self.url, headers={"accept": "application/vnd.github+json",
                                                       "user-agent": f"Narratarr/{__version__}"})
                r.raise_for_status()
                d = r.json()
                self.latest = {"version": (d.get("tag_name") or "").lstrip("v"), "url": d.get("html_url") or RELEASES_URL,
                               "notes": d.get("body") or "", "published_at": d.get("published_at")}
                self.error = None
            except Exception as e:  # noqa: BLE001
                self.error = str(e) or e.__class__.__name__
                self.checked_at = time.time() - UPDATE_EVERY + RETRY_AFTER  # try again sooner
                log.info("Update check failed: %s", self.error)

    def info(self) -> dict:
        cur, new = _semver(__version__), _semver((self.latest or {}).get("version", ""))
        available = bool(self.enabled and cur and new and new > cur)
        return {"enabled": self.enabled, "release_build": is_release_build(), "current": __version__,
                "available": available, "latest": self.latest if available else None,
                "releases_url": RELEASES_URL, "error": self.error}


class Health:
    """Overall light: ok (green), warn (amber: something optional is down), bad (red: can't download or file books)."""

    def __init__(self, settings, jobs):
        self.settings, self.jobs = settings, jobs
        self._cached: dict | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._at = 0.0

    async def get(self) -> dict:
        async with self._lock:
            if self._cached is None or time.time() - self._at > HEALTH_EVERY:
                self._cached = await self._compute()
                self._at = time.time()
            return self._cached

    async def _compute(self) -> dict:
        s = self.settings
        issues: list[dict] = []

        def add(level: str, text: str) -> None:
            issues.append({"level": level, "text": text})

        # slskd
        if not s.get("slskd.api_key"):
            add("bad", "slskd: no API key set")
        else:
            from .slskd import SlskdClient
            try:
                st = await asyncio.wait_for(SlskdClient(s.get("slskd.url"), s.get("slskd.api_key"), timeout=8).status(), 10)
                if not (st["connected"] and st["logged_in"]):
                    add("bad", f"slskd isn't logged in to Soulseek ({st.get('state') or 'disconnected'})")
            except Exception as e:  # noqa: BLE001
                add("bad", f"slskd unreachable: {str(e) or e.__class__.__name__}")
        # folders
        for label, p in (("Incomplete folder", s.incomplete), ("Completed folder", s.completed),
                         ("slskd downloads folder", Path(s.get("slskd.downloads_path")))):
            if not p.exists():
                add("bad", f"{label} {p} isn't there (not mounted?)")
            elif not os.access(p, os.W_OK):
                add("bad", f"{label} {p} isn't writable")
        # Audiobookshelf (optional)
        if self.jobs.abs.enabled:
            try:
                await asyncio.wait_for(self.jobs.abs.target_folder(), 12)
            except Exception as e:  # noqa: BLE001
                add("warn", f"Audiobookshelf: {str(e) or e.__class__.__name__}")
        # ffmpeg (needed for re-encoding and m4b chapters)
        if not shutil.which("ffmpeg"):
            add("warn", "ffmpeg isn't installed: re-encoding and M4B chapters are unavailable")
        level = "bad" if any(i["level"] == "bad" for i in issues) else "warn" if issues else "ok"
        return {"level": level, "issues": issues}
