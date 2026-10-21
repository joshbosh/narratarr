"""Audiobookshelf integration.

Flow for each finished book (see `AbsSync.import_book`):
  1. copy/move the organised folder from completed/ into the chosen ABS library folder
  2. ask ABS to scan just those files (POST /api/watcher/update), falling back to a
     full library scan if the item hasn't appeared after a while
  3. find the new item by path, then reconcile metadata:
       fill      → keep ABS's values, fill only empty fields, log differences
       overwrite → Narratarr's Audible record replaces ABS's
     plus Audible chapters for single-file books ABS has none for
  4. record the ABS item id; optionally drop Narratarr's own copy and library row

A background loop keeps a local index of the ABS library (for Search badges and
the Library page) and retries imports that couldn't finish (ABS down, folder not
mounted, item not yet scanned).

API endpoints used were checked against advplyr/audiobookshelf server source:
  GET  /api/libraries, GET /api/libraries/:id/items?minified=1&limit&page
  POST /api/watcher/update {libraryId, path, type:'add'}, POST /api/libraries/:id/scan
  GET  /api/items/:id?expanded=1, PATCH /api/items/:id/media {metadata}
  POST /api/items/:id/chapters {chapters:[{start,end,title}]}, GET /api/items/:id/cover
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path, PurePosixPath

import httpx

from ..matching import last_name, norm, short_title

log = logging.getLogger(__name__)

WATCHER_WAIT = int(os.environ.get("NARRATARR_ABS_WATCHER_WAIT", "45"))  # seconds before falling back to a full scan
AUDIO_EXTS = {"m4b", "mp3", "m4a", "flac", "opus", "ogg", "aac", "wma", "wav", "mp4"}


class AbsError(Exception):
    pass


def match_key(title: str | None, author: str | None) -> str:
    return f"{norm(short_title(title or ''))}|{last_name(author or '')}"


class AbsClient:
    def __init__(self, url: str, token: str, timeout: float = 30):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = timeout

    def _c(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=timeout or self.timeout)

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code in (401, 403):
            raise AbsError(f"Audiobookshelf rejected the API key ({r.status_code}) — it needs an admin user's key")
        r.raise_for_status()

    async def libraries(self) -> list[dict]:
        async with self._c() as c:
            r = await c.get("/api/libraries")
            self._check(r)
            data = r.json()
            libs = data.get("libraries", data) if isinstance(data, dict) else data
            return [{"id": lib["id"], "name": lib.get("name"), "mediaType": lib.get("mediaType"),
                     "folders": [{"id": f.get("id"), "path": f.get("fullPath") or f.get("path")}
                                 for f in lib.get("folders") or []]}
                    for lib in libs]

    async def library_items(self, library_id: str, page_size: int = 500) -> list[dict]:
        out: list[dict] = []
        page = 0
        async with self._c(timeout=120) as c:
            while True:
                r = await c.get(f"/api/libraries/{library_id}/items",
                                params={"minified": 1, "limit": page_size, "page": page})
                self._check(r)
                data = r.json()
                results = data.get("results") or []
                out.extend(results)
                total = data.get("total") or 0
                page += 1
                if not results or len(out) >= total or page > 400:
                    return out

    async def item(self, item_id: str) -> dict:
        async with self._c() as c:
            r = await c.get(f"/api/items/{item_id}", params={"expanded": 1})
            self._check(r)
            return r.json()

    async def notify_added(self, library_id: str, abs_paths: list[str]) -> None:
        async with self._c() as c:
            for p in abs_paths:
                r = await c.post("/api/watcher/update", json={"libraryId": library_id, "path": p, "type": "add"})
                self._check(r)

    async def scan_library(self, library_id: str) -> None:
        async with self._c() as c:
            r = await c.post(f"/api/libraries/{library_id}/scan")
            self._check(r)

    async def update_media(self, item_id: str, payload: dict) -> None:
        async with self._c() as c:
            r = await c.patch(f"/api/items/{item_id}/media", json=payload)
            self._check(r)

    async def set_chapters(self, item_id: str, chapters: list[dict]) -> None:
        async with self._c() as c:
            r = await c.post(f"/api/items/{item_id}/chapters", json={"chapters": chapters})
            self._check(r)

    async def cover(self, item_id: str, width: int = 400) -> tuple[bytes, str] | None:
        async with self._c() as c:
            r = await c.get(f"/api/items/{item_id}/cover", params={"width": width, "format": "jpeg"})
            if r.status_code != 200:
                return None
            return r.content, r.headers.get("content-type", "image/jpeg")


# ---------------------------------------------------------------- reconcile
def _abs_fields(meta: dict) -> dict:
    return {
        "title": meta.get("title"), "subtitle": meta.get("subtitle"),
        "authors": [a.get("name") for a in meta.get("authors") or [] if a.get("name")],
        "narrators": list(meta.get("narrators") or []),
        "series": [{"name": s.get("name"), "sequence": s.get("sequence") or None} for s in meta.get("series") or []],
        "genres": list(meta.get("genres") or []),
        "publishedYear": meta.get("publishedYear"), "publishedDate": meta.get("publishedDate"),
        "publisher": meta.get("publisher"), "description": meta.get("description"),
        "isbn": meta.get("isbn"), "asin": meta.get("asin"), "language": meta.get("language"),
    }


def _narratarr_fields(book: dict) -> dict:
    return {
        "title": book.get("title"), "subtitle": book.get("subtitle"),
        "authors": [a["name"] for a in book.get("authors") or []],
        "narrators": list(book.get("narrators") or []),
        "series": [{"name": s["name"], "sequence": s.get("sequence") or None} for s in book.get("series") or []],
        "genres": list(book.get("genres") or []),
        "publishedYear": book.get("year"), "publishedDate": book.get("release_date"),
        "publisher": book.get("publisher"), "description": book.get("description"),
        "isbn": book.get("isbn"), "asin": book.get("asin"), "language": book.get("language"),
    }


def _empty(v) -> bool:
    return v in (None, "", [], {})


def _same(field: str, a, b) -> bool:
    if field in ("authors", "narrators", "genres"):
        return sorted(norm(x) for x in a) == sorted(norm(x) for x in b)
    if field == "series":
        return [(norm(s["name"]), str(s.get("sequence") or "")) for s in a] == \
               [(norm(s["name"]), str(s.get("sequence") or "")) for s in b]
    if field == "description":
        return norm(a)[:200] == norm(b)[:200]
    if field == "asin":
        return str(a).upper() == str(b).upper()
    return norm(str(a)) == norm(str(b))


def plan_metadata(abs_meta: dict, book: dict, mode: str) -> tuple[dict, list[str], list[str]]:
    """Return (PATCH metadata payload, filled fields, conflicts)."""
    have, want = _abs_fields(abs_meta), _narratarr_fields(book)
    payload: dict = {}
    filled: list[str] = []
    conflicts: list[str] = []
    for field, value in want.items():
        if _empty(value):
            continue
        current = have.get(field)
        if _empty(current):
            payload[field] = value
            filled.append(field)
        elif not _same(field, current, value):
            if mode == "overwrite":
                payload[field] = value
                filled.append(field)
            else:
                conflicts.append(f"{field}: Audiobookshelf has {_short(current)}, Audible has {_short(value)}")
    if "authors" in payload:
        payload["authors"] = [{"name": n} for n in payload["authors"]]
    for k in ("publishedYear",):
        if k in payload and payload[k] is not None:
            payload[k] = str(payload[k])
    return payload, filled, conflicts


def _short(v) -> str:
    if isinstance(v, list):
        v = ", ".join(x if isinstance(x, str) else (f"{x['name']} #{x['sequence']}" if x.get("sequence") else x["name"])
                      for x in v)
    s = str(v)
    return f"“{s[:60]}…”" if len(s) > 60 else f"“{s}”"


def chapters_payload(book: dict, item: dict) -> list[dict] | None:
    """Audible chapters for a single-file book ABS has no chapters for, if lengths agree."""
    media = item.get("media") or {}
    if (media.get("chapters") or []) or len(media.get("audioFiles") or media.get("tracks") or []) != 1:
        return None
    chs = book.get("chapters") or []
    expected = (book.get("runtime_ms") or 0) / 1000
    duration = float(media.get("duration") or 0)
    if len(chs) < 2 or not expected or not duration or abs(duration - expected) > max(90, expected * 0.01):
        return None
    out = []
    for i, c in enumerate(chs):
        start = c["start_ms"] / 1000
        end = chs[i + 1]["start_ms"] / 1000 if i + 1 < len(chs) else duration
        out.append({"id": i, "start": round(start, 3), "end": round(min(end, duration), 3), "title": c["title"]})
    return out


# ---------------------------------------------------------------- sync engine
class AbsSync:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self._lock = asyncio.Lock()
        self.last_sync: float | None = None
        self.last_error: str | None = None
        self.wake = asyncio.Event()
        self.syncing = False

    # -- config helpers
    @property
    def enabled(self) -> bool:
        s = self.settings
        return bool(s.get("abs.enabled") and s.get("abs.url") and s.get("abs.token") and s.get("abs.library_id"))

    def client(self) -> AbsClient:
        return AbsClient(self.settings.get("abs.url"), self.settings.get("abs.token"))

    def link(self, item_id: str) -> str:
        base = (self.settings.get("abs.public_url") or self.settings.get("abs.url")).rstrip("/")
        return f"{base}/item/{item_id}"

    async def target_folder(self) -> tuple[dict, Path]:
        """(ABS folder {id, path}, local path inside Narratarr)."""
        libs = await self.client().libraries()
        lib = next((l for l in libs if l["id"] == self.settings.get("abs.library_id")), None)
        if not lib:
            raise AbsError("The selected Audiobookshelf library no longer exists — pick one in Settings")
        if not lib["folders"]:
            raise AbsError(f"Library “{lib['name']}” has no folders")
        fid = self.settings.get("abs.folder_id")
        folder = next((f for f in lib["folders"] if f["id"] == fid), None) or lib["folders"][0]
        local = Path(self.settings.get("abs.local_path") or folder["path"])
        if not local.is_dir():
            raise AbsError(f"Audiobookshelf folder {folder['path']} isn't mounted in Narratarr at {local} — "
                           f"add a volume for it, or set “That folder, as mounted in Narratarr”")
        if not os.access(local, os.W_OK):
            raise AbsError(f"Narratarr can't write to {local} — check ownership matches the container user")
        return folder, local

    # -- library index
    async def refresh_index(self) -> int:
        if not self.enabled:
            return 0
        async with self._lock:
            lib_id = self.settings.get("abs.library_id")
            self.syncing = True
            try:
                items = await self.client().library_items(lib_id)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                raise
            finally:
                self.syncing = False
            via = {r["item_id"] for r in self.db.query("SELECT item_id FROM abs_links")}
            rows = []
            for it in items:
                media = it.get("media") or {}
                m = media.get("metadata") or {}
                authors = [a.strip() for a in (m.get("authorName") or "").split(",") if a.strip()]
                narrators = [n.strip() for n in (m.get("narratorName") or "").split(",") if n.strip()]
                series = []
                if m.get("seriesName"):
                    for part in m["seriesName"].split(", "):
                        name, _, seq = part.partition(" #")
                        series.append({"name": name, "sequence": seq or None})
                rows.append({
                    "id": it["id"], "library_id": lib_id, "asin": (m.get("asin") or "").upper() or None,
                    "match_key": match_key(m.get("title"), authors[0] if authors else ""),
                    "title": m.get("title"), "subtitle": m.get("subtitle"), "authors": authors,
                    "narrators": narrators, "series": series, "duration_s": media.get("duration"),
                    "num_tracks": media.get("numTracks"), "size_bytes": it.get("size"), "path": it.get("path"),
                    "has_cover": 1 if media.get("coverPath") else 0,
                    "added_at": (it.get("addedAt") or 0) / 1000, "via_narratarr": 1 if it["id"] in via else 0,
                })
            self.db.execute("DELETE FROM abs_items")
            for r in rows:
                self.db.insert("abs_items", r)
            self.last_sync, self.last_error = time.time(), None
            return len(rows)

    def lookup(self, asin: str | None, title: str | None, author: str | None) -> dict | None:
        """Match a release to an ABS item: by ASIN, else title + author surname.

        A title match is only trusted when the ABS item has no ASIN (or we have none), so a
        different narration of the same title already in ABS isn't reported as this one.
        """
        if asin:
            row = self.db.one("SELECT * FROM abs_items WHERE asin=?", (asin.upper(),))
            if row:
                return row
        if not title:
            return None
        key = match_key(title, author)
        if asin:
            return self.db.one("SELECT * FROM abs_items WHERE match_key=? AND (asin IS NULL OR asin='')", (key,))
        return self.db.one("SELECT * FROM abs_items WHERE match_key=?", (key,))

    async def _wait_for_item(self, client: AbsClient, lib_id: str, abs_path: str, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        target = abs_path.rstrip("/")
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            for it in await client.library_items(lib_id):
                if (it.get("path") or "").rstrip("/") == target:
                    return it
        return None

    # -- import one book
    async def import_book(self, job: dict, log_fn) -> dict:
        """Returns {"status": imported|pending, "item_id", "detail", "path"}. Raises nothing for ABS problems."""
        s = self.settings
        book = {k: v for k, v in (job.get("book") or {}).items() if not k.startswith("_")}
        src = Path(job["output_path"])
        delete = bool(s.get("abs.delete_after_import"))
        try:
            client = self.client()
            lib_id = s.get("abs.library_id")
            folder, local_root = await self.target_folder()
            completed = s.completed.resolve()
            already_there = local_root.resolve() in src.resolve().parents
            if already_there:
                dest = src
            else:
                rel = src.resolve().relative_to(completed) if completed in src.resolve().parents else Path(src.name)
                dest = local_root / rel
                if dest.exists() and any(dest.iterdir()):
                    marker = dest / ".narratarr.json"
                    if not marker.exists():
                        raise AbsError(f"{dest} already exists in Audiobookshelf's folder and isn't from Narratarr")
                await asyncio.to_thread(_transfer, src, dest, delete, completed)
                log_fn(f"{'Moved' if delete else 'Copied'} into Audiobookshelf folder: {dest}")
            abs_dir = str(PurePosixPath(folder["path"]) / dest.relative_to(local_root).as_posix())

            audio = sorted(p for p in dest.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO_EXTS)
            abs_files = [str(PurePosixPath(folder["path"]) / p.relative_to(local_root).as_posix()) for p in audio]
            try:
                await client.notify_added(lib_id, abs_files)
            except httpx.HTTPError as e:
                log_fn(f"Targeted scan request failed ({e}); falling back to a library scan")
            item = await self._wait_for_item(client, lib_id, abs_dir, timeout=WATCHER_WAIT)
            if not item:
                log_fn("Not picked up by the folder watcher yet — running a library scan")
                await client.scan_library(lib_id)
                item = await self._wait_for_item(client, lib_id, abs_dir, timeout=240)
            if not item:
                return {"status": "pending", "path": str(dest),
                        "detail": "Files are in the Audiobookshelf folder but it hasn't scanned them yet"}

            item_id = item["id"]
            full = await client.item(item_id)
            payload, filled, conflicts = plan_metadata((full.get("media") or {}).get("metadata") or {}, book,
                                                       s.get("abs.metadata_mode"))
            if payload:
                await client.update_media(item_id, {"metadata": payload})
            log_fn(f"Audiobookshelf metadata: {('filled ' + ', '.join(filled)) if filled else 'already complete'}")
            for c in conflicts:
                log_fn(f"Kept Audiobookshelf's {c}")
            chapters = chapters_payload(book, full)
            if chapters:
                await client.set_chapters(item_id, chapters)
                log_fn(f"Added {len(chapters)} Audible chapters in Audiobookshelf")
            n = len(conflicts)
            detail = "Imported" + (f" · {n} field{'s differ' if n != 1 else ' differs'} from Audible — see log" if n else "")
            return {"status": "imported", "item_id": item_id, "path": str(dest), "detail": detail,
                    "conflicts": conflicts}
        except (AbsError, httpx.HTTPError, OSError) as e:
            msg = str(e) or e.__class__.__name__
            log_fn(f"Audiobookshelf import pending: {msg}")
            return {"status": "pending", "detail": msg}


def _transfer(src: Path, dest: Path, move: bool, stop: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move:
        if dest.exists():
            for p in src.rglob("*"):
                if p.is_file():
                    t = dest / p.relative_to(src)
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(t))
            shutil.rmtree(src, ignore_errors=True)
        else:
            shutil.move(str(src), str(dest))
        # tidy empty Author/Series folders left behind in completed/
        parent = src.parent.resolve()
        while parent != stop and stop in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    else:
        shutil.copytree(src, dest, dirs_exist_ok=True)
