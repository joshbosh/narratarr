"""HTTP API. Every route sits behind `current_user` (see auth.py)."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from . import __version__
from .auth import User, current_user, require_admin
from .integrations.abs import AbsClient
from .library import find_book_folders, rescan
from .providers import full_metadata, get_provider

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])

LIGHT_JOB_COLS = ("id, kind, asin, region, title, subtitle, authors, narrators, series, runtime_min, cover_url, "
                  "status, detail, progress, selected, transfers, output_path, error, abs_status, abs_detail, abs_item_id, "
                  "created_at, updated_at")


def _state(request: Request):
    return request.app.state.db, request.app.state.settings, request.app.state.jobs


def _abs_ref(abs_sync, row: dict | None) -> dict | None:
    if not row:
        return None
    return {"id": row["id"], "title": row["title"], "link": abs_sync.link(row["id"])}


def _light_job(j: dict) -> dict:
    sel = j.get("selected") or None
    if sel:
        sel = {k: sel.get(k) for k in ("username", "display_dir", "score", "formats", "file_count", "size")}
    transfers = j.get("transfers") or []
    j["selected"] = sel
    j["files_total"] = len(transfers)
    j["files_done"] = sum(1 for t in transfers if t.get("local"))
    j["files_missing"] = sum(1 for t in transfers if t.get("missing_local"))
    j.pop("transfers", None)
    return j


def _with_abs_link(jobs, j: dict) -> dict:
    j["abs_link"] = jobs.abs.link(j["abs_item_id"]) if j.get("abs_item_id") else None
    return j


# ------------------------------------------------------------------- system
@router.get("/system/status")
async def system_status(request: Request):
    db, s, _ = _state(request)
    paths = {}
    for label, p in (("downloads", s.downloads), ("incomplete", s.incomplete), ("completed", s.completed),
                     ("slskd_downloads", Path(s.get("slskd.downloads_path")))):
        paths[label] = {"path": str(p), "exists": p.exists(),
                        "writable": p.exists() and shutil.disk_usage(p) is not None and _writable(p)}
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    lib = db.one("SELECT COUNT(*) n FROM library")["n"]
    return {"version": __version__, "paths": paths, "ffmpeg": bool(shutil.which("ffmpeg")),
            "jobs": counts, "library": lib, "auth": s.get("auth.mode")}


def _writable(p: Path) -> bool:
    import os
    return os.access(p, os.W_OK)


# ------------------------------------------------------------------- search
def _annotate(request: Request, results: list[dict]) -> None:
    """Attach live state to each result: in Narratarr's library, current job, in Audiobookshelf."""
    db, _, jobs = _state(request)
    keys = sorted({k for r in results for k in (r.get("asin"), r.get("ref")) if k})
    in_lib, in_queue = set(), {}
    if keys:
        marks = ",".join("?" * len(keys))
        in_lib = {r["asin"] for r in db.query(f"SELECT asin FROM library WHERE asin IN ({marks})", keys)}
        for r in db.query(f"SELECT id, asin, status FROM jobs WHERE asin IN ({marks}) ORDER BY id", keys):
            in_queue[r["asin"]] = {"id": r["id"], "status": r["status"]}
    abs_sync = jobs.abs
    for r in results:
        r["in_library"] = bool({r.get("asin"), r.get("ref")} & in_lib)
        r["job"] = in_queue.get(r.get("ref")) or in_queue.get(r.get("asin"))
        r["in_abs"] = _abs_ref(abs_sync, abs_sync.lookup(r.get("asin"), r.get("title"),
                                                          (r.get("authors") or [{}])[0].get("name"))) \
            if abs_sync.enabled else None


@router.get("/search")
async def search(request: Request, q: str = Query(..., min_length=1), limit: int = 30):
    s = _state(request)[1]
    hub = get_provider(s)
    try:
        results, info = await hub.search_ex(q, limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Lookup failed: {e}") from e
    _annotate(request, results)
    return {"results": results, "sources": info["sources"], "configured_sources": hub.source_count,
            "fallback": info["fallback"], "region": s.get("metadata.region")}


@router.post("/search/status")
async def search_status(request: Request, payload: dict = Body(...)):
    """Refresh library/queue/Audiobookshelf state for results already on screen (no metadata lookups)."""
    items = [{"asin": i.get("asin"), "ref": i.get("ref") or i.get("asin"), "title": i.get("title"),
              "authors": i.get("authors") or []}
             for i in (payload.get("items") or [])[:100] if i.get("ref") or i.get("asin")]
    _annotate(request, items)
    return {i["ref"]: {"in_library": i["in_library"], "job": i["job"], "in_abs": i["in_abs"]} for i in items}


# --------------------------------------------------------------------- jobs
@router.get("/jobs")
async def list_jobs(request: Request, scope: str = "all"):
    db, _, _ = _state(request)
    where = ""
    if scope == "active":
        where = ("WHERE status IN ('queued','searching','needs_review','downloading','processing') "
                 "OR (status='completed' AND abs_status='pending')")
    elif scope == "history":
        where = "WHERE status IN ('completed','failed','cancelled')"
    rows = db.query(f"SELECT {LIGHT_JOB_COLS} FROM jobs {where} ORDER BY created_at DESC LIMIT 300")
    jobs = _state(request)[2]
    return [_with_abs_link(jobs, _light_job(r)) for r in rows]


@router.post("/jobs")
async def create_job(request: Request, payload: dict = Body(...), user: User = Depends(current_user)):
    _, s, jobs = _state(request)
    ref = payload.get("ref") or payload.get("asin")
    if not ref:
        raise HTTPException(400, "ref or asin required")
    if (existing := jobs.find_active(ref)) or (payload.get("asin") and (existing := jobs.find_active(payload["asin"]))):
        raise HTTPException(409, f"Already in the queue (job {existing['id']})")
    book = payload.get("book")
    if not book or (book.get("ref") or book.get("asin")) != ref:
        book = await full_metadata(s, ref, payload.get("region"))
        if not book:
            raise HTTPException(404, "Not found in the configured metadata sources")
    jid = jobs.create_download(book, user.id)
    return jobs.get(jid)


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: int):
    jobs = _state(request)[2]
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return _with_abs_link(jobs, job)


@router.post("/jobs/{job_id}/select")
async def select_candidate(request: Request, job_id: int, payload: dict = Body(...)):
    jobs = _state(request)[2]
    job = jobs.get(job_id) or {}
    if job.get("status") not in ("needs_review", "downloading", "failed", "cancelled"):
        raise HTTPException(409, f"Can't pick a source while {job.get('status')}")
    if job["status"] == "downloading":
        await jobs._cancel_transfers(job)
    try:
        await jobs.start_download(job_id, payload["candidate_id"])
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return jobs.get(job_id)


@router.post("/jobs/{job_id}/search")
async def custom_search(request: Request, job_id: int, payload: dict = Body(...)):
    jobs = _state(request)[2]
    query = (payload.get("query") or "").strip()
    if len(query) < 3:
        raise HTTPException(400, "Query too short")
    if not jobs.get(job_id):
        raise HTTPException(404)
    jobs.update(job_id, status="searching", detail=f"Custom search: {query}")
    jobs.start_custom_search(job_id, query)
    return {"ok": True}


@router.post("/jobs/{job_id}/redownload-missing")
async def redownload_missing(request: Request, job_id: int):
    jobs = _state(request)[2]
    if not jobs.get(job_id):
        raise HTTPException(404)
    try:
        n = await jobs.redownload_missing(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't queue the missing files: {e}") from e
    if not n:
        raise HTTPException(409, "No missing files")
    return {"requeued": n}


@router.post("/jobs/{job_id}/import-partial")
async def import_partial(request: Request, job_id: int):
    jobs = _state(request)[2]
    job = jobs.get(job_id)
    if not job or not any(t.get("local") for t in job.get("transfers") or []):
        raise HTTPException(409, "Nothing has been downloaded yet")
    jobs.import_partial(job_id)
    return {"ok": True}


@router.get("/activity")
async def activity(request: Request):
    return _state(request)[2].activity()


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: int):
    jobs = _state(request)[2]
    if not jobs.get(job_id):
        raise HTTPException(404)
    jobs.retry(job_id)
    return jobs.get(job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int):
    jobs = _state(request)[2]
    await jobs.cancel(job_id)
    return jobs.get(job_id)


@router.delete("/jobs/{job_id}")
async def delete_job(request: Request, job_id: int):
    db, _, jobs = _state(request)
    job = jobs.get(job_id)
    if job and job["status"] in ("downloading", "searching", "queued"):
        await jobs.cancel(job_id)
    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"ok": True}


@router.post("/jobs/clear")
async def clear_jobs(request: Request):
    db = _state(request)[0]
    db.execute("DELETE FROM jobs WHERE status IN ('completed','cancelled') AND COALESCE(abs_status,'') != 'pending'")
    return {"ok": True}


# ------------------------------------------------------------------ library
@router.get("/library")
async def library(request: Request):
    db, s, jobs = _state(request)
    rows = db.query("SELECT id, asin, title, subtitle, authors, narrators, series, path, cover_path, file_count, "
                    "size_bytes, duration_s, source, added_at, abs_item_id, abs_status FROM library ORDER BY added_at DESC")
    linked = set()
    for r in rows:
        r["has_cover"] = bool(r.pop("cover_path"))
        if r.get("abs_item_id"):
            linked.add(r["abs_item_id"])
            r["abs_link"] = jobs.abs.link(r["abs_item_id"])
    abs_rows = []
    if jobs.abs.enabled:
        for a in db.query("SELECT id, asin, title, subtitle, authors, narrators, series, duration_s, num_tracks, "
                          "size_bytes, has_cover, added_at, via_narratarr FROM abs_items ORDER BY added_at DESC"):
            if a["id"] in linked:
                continue
            a["link"] = jobs.abs.link(a["id"])
            abs_rows.append(a)
    return {
        "narratarr": rows,
        "audiobookshelf": abs_rows,
        "abs": {"enabled": jobs.abs.enabled, "last_sync": jobs.abs.last_sync, "error": jobs.abs.last_error},
    }


@router.get("/library/{item_id}")
async def library_item(request: Request, item_id: int):
    row = _state(request)[0].one("SELECT * FROM library WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404)
    if row.get("abs_item_id"):
        row["abs_link"] = _state(request)[2].abs.link(row["abs_item_id"])
    p = Path(row["path"])
    row["files"] = sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file()) if p.exists() else []
    return row


@router.get("/library/{item_id}/cover")
async def library_cover(request: Request, item_id: int):
    row = _state(request)[0].one("SELECT cover_path FROM library WHERE id=?", (item_id,))
    if not row or not row["cover_path"] or not Path(row["cover_path"]).exists():
        raise HTTPException(404)
    return FileResponse(row["cover_path"], headers={"Cache-Control": "max-age=86400"})


@router.delete("/library/{item_id}", dependencies=[Depends(require_admin)])
async def library_delete(request: Request, item_id: int, delete_files: bool = False):
    db, s, _ = _state(request)
    row = db.one("SELECT * FROM library WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404)
    if delete_files:
        p = Path(row["path"]).resolve()
        if s.completed.resolve() not in p.parents:
            raise HTTPException(400, "Refusing to delete outside the completed folder")
        shutil.rmtree(p, ignore_errors=True)
    db.execute("DELETE FROM library WHERE id=?", (item_id,))
    return {"ok": True}


@router.post("/library/rescan")
async def library_rescan(request: Request):
    db, s, _ = _state(request)
    return await asyncio.to_thread(rescan, db, s.completed)


@router.post("/library/{item_id}/abs")
async def library_send_to_abs(request: Request, item_id: int):
    jobs = _state(request)[2]
    if not jobs.abs.enabled:
        raise HTTPException(400, "Audiobookshelf isn't set up — see Settings")
    try:
        jid = jobs.queue_abs_for_library_item(item_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"job_id": jid}


# ----------------------------------------------------------- audiobookshelf
@router.get("/abs/status")
async def abs_status(request: Request):
    db, _, jobs = _state(request)
    return {"enabled": jobs.abs.enabled, "last_sync": jobs.abs.last_sync, "error": jobs.abs.last_error,
            "items": db.one("SELECT COUNT(*) n FROM abs_items")["n"],
            "pending": db.one("SELECT COUNT(*) n FROM jobs WHERE abs_status='pending'")["n"]}


@router.post("/abs/sync")
async def abs_sync_now(request: Request):
    jobs = _state(request)[2]
    if not jobs.abs.enabled:
        raise HTTPException(400, "Audiobookshelf isn't set up — see Settings")
    try:
        n = await jobs.abs.refresh_index()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Audiobookshelf sync failed: {e}") from e
    jobs.abs.wake.set()  # also retries pending imports
    return {"items": n}


@router.get("/abs/libraries")
async def abs_libraries(request: Request):
    s = _state(request)[1]
    try:
        return await AbsClient(s.get("abs.url"), s.get("abs.token")).libraries()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e) or e.__class__.__name__) from e


@router.get("/abs/items/{item_id}")
async def abs_item(request: Request, item_id: str):
    db, _, jobs = _state(request)
    row = db.one("SELECT * FROM abs_items WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404)
    row["link"] = jobs.abs.link(item_id)
    try:
        full = await jobs.abs.client().item(item_id)
        meta = (full.get("media") or {}).get("metadata") or {}
        row["description"] = meta.get("descriptionPlain") or meta.get("description")
        row["publisher"] = meta.get("publisher")
        row["year"] = meta.get("publishedYear")
        row["num_chapters"] = len((full.get("media") or {}).get("chapters") or [])
    except Exception:  # noqa: BLE001
        pass
    return row


@router.get("/abs/items/{item_id}/cover")
async def abs_item_cover(request: Request, item_id: str):
    from fastapi import Response
    jobs = _state(request)[2]
    try:
        res = await jobs.abs.client().cover(item_id)
    except Exception:  # noqa: BLE001
        res = None
    if not res:
        raise HTTPException(404)
    return Response(content=res[0], media_type=res[1], headers={"Cache-Control": "max-age=86400"})


@router.post("/jobs/{job_id}/abs")
async def job_send_to_abs(request: Request, job_id: int):
    jobs = _state(request)[2]
    job = jobs.get(job_id)
    if not job or job["status"] != "completed":
        raise HTTPException(409, "Only completed jobs can be sent to Audiobookshelf")
    if not jobs.abs.enabled:
        raise HTTPException(400, "Audiobookshelf isn't set up — see Settings")
    jobs.start_abs_import(job_id)
    return {"ok": True}


# ------------------------------------------------------------ manual import
def _import_roots(s) -> list[dict]:
    roots = [
        {"key": "incomplete", "label": "Narratarr incomplete", "path": str(s.incomplete)},
        {"key": "slskd", "label": "slskd downloads", "path": str(s.get("slskd.downloads_path"))},
    ]
    for i, p in enumerate([x.strip() for x in str(s.get("paths.import_roots")).split(",") if x.strip()]):
        roots.append({"key": f"extra{i}", "label": Path(p).name or p, "path": p})
    for r in roots:
        r["exists"] = Path(r["path"]).exists()
    return roots


def _resolve_import(s, root_key: str, rel: str) -> Path:
    root = next((r for r in _import_roots(s) if r["key"] == root_key), None)
    if not root:
        raise HTTPException(404, "Unknown import folder")
    base = Path(root["path"]).resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(400, "Path escapes the import folder")
    if not target.exists():
        raise HTTPException(404, "Folder not found")
    return target


@router.get("/import/roots")
async def import_roots(request: Request):
    return _import_roots(_state(request)[1])


@router.get("/import/scan")
async def import_scan(request: Request, root: str):
    s = _state(request)[1]
    base = _resolve_import(s, root, ".")
    folders = await asyncio.to_thread(find_book_folders, base, [s.completed])
    if root == "incomplete":
        # Narratarr's own staging folders are "<job id> - <title>": suggest the job's author + title instead
        import re as _re
        from .matching import primary_author, short_title
        jobs = _state(request)[2]
        for f in folders:
            m = _re.match(r"^(\d+) - ", f["name"])
            job = jobs.get(int(m.group(1))) if m else None
            if job and job.get("title"):
                f["suggestion"] = f"{primary_author(job['book'] or {})} {short_title(job['title'])}".strip()
    return folders


@router.post("/import")
async def do_import(request: Request, payload: dict = Body(...), user: User = Depends(current_user)):
    _, s, jobs = _state(request)
    target = _resolve_import(s, payload.get("root", ""), payload.get("rel", ""))
    mode = payload.get("mode", "move")
    if mode not in ("move", "copy"):
        raise HTTPException(400, "mode must be move or copy")
    ref = payload.get("ref") or payload.get("asin")
    if not ref:
        raise HTTPException(400, "ref or asin required")
    book = payload.get("book")
    if not book or (book.get("ref") or book.get("asin")) != ref:
        book = await full_metadata(s, ref, payload.get("region"))
    else:
        book = (await full_metadata(s, ref, book.get("region"))) or book
    if not book:
        raise HTTPException(404, "Not found in the configured metadata sources")
    jid = jobs.create_import(book, target, mode, user.id)
    return jobs.get(jid)


# ----------------------------------------------------------------- settings
@router.get("/settings")
async def get_settings(request: Request):
    return _state(request)[1].public()


@router.put("/settings", dependencies=[Depends(require_admin)])
async def put_settings(request: Request, payload: dict = Body(...)):
    s = _state(request)[1]
    s.update(payload)
    _state(request)[2].abs.wake.set()
    for p in (s.incomplete, s.completed):
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return s.public()


@router.post("/settings/test/{target}")
async def test_connection(request: Request, target: str):
    s = _state(request)[1]
    try:
        if target == "slskd":
            from .slskd import SlskdClient
            st = await SlskdClient(s.get("slskd.url"), s.get("slskd.api_key")).status()
            ok = st["connected"] and st["logged_in"]
            msg = (f"Connected as {st['username']}" if ok else f"slskd reachable but not logged in ({st['state']})")
            path = Path(s.get("slskd.downloads_path"))
            if not path.exists():
                msg += f" · ⚠ downloads folder {path} isn't mounted here"
            return {"ok": ok, "message": msg, "detail": st}
        if target == "metadata":
            from .providers.hardcover import HardcoverProvider
            hub = get_provider(s)
            parts, ok = [], True
            try:
                await hub.audible.search("history", 3)
                parts.append(f"Audible: OK ({s.get('metadata.region').upper()})")
            except Exception as e:  # noqa: BLE001
                parts.append(f"Audible: failed ({e})")
                ok = False
            if s.get("metadata.hardcover_enabled"):
                if not s.get("metadata.hardcover_token"):
                    parts.append("Hardcover: enabled but no API key")
                    ok = False
                else:
                    try:
                        await HardcoverProvider(s.get("metadata.hardcover_token")).search("history", 3)
                        parts.append("Hardcover: OK")
                    except Exception as e:  # noqa: BLE001
                        parts.append(f"Hardcover: failed ({e})")
                        ok = False
            return {"ok": ok, "message": " · ".join(parts)}
        if target == "abs":
            jobs = _state(request)[2]
            libs = await AbsClient(s.get("abs.url"), s.get("abs.token")).libraries()
            books = [l for l in libs if l.get("mediaType") == "book"]
            if not s.get("abs.library_id"):
                return {"ok": True, "message": f"Connected · {len(books)} book libraries — pick one below, then save",
                        "libraries": libs}
            try:
                folder, local = await jobs.abs.target_folder()
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": f"Connected, but {e}", "libraries": libs}
            n = await jobs.abs.refresh_index()
            return {"ok": True, "libraries": libs,
                    "message": f"Connected · {n} books in library · files go to {folder['path']}"
                               + (f" (mounted here at {local})" if str(local) != folder["path"] else "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e) or e.__class__.__name__}
    raise HTTPException(404, "Unknown test")
