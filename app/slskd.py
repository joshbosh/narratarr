"""Minimal async slskd API client (v0).

Search timing note: slskd's `searchTimeout` has been documented as both
milliseconds and seconds across versions, so we don't rely on it. We give
slskd a generous value, enforce our own wall-clock window, then stop the
search with PUT /searches/{id} and read whatever responses arrived.

Downloads: newer slskd builds expose POST /transfers/downloads/batches with an
explicit `destination` (relative to slskd's download dir), which keeps each
job's files in their own folder. Older builds only have the legacy
POST /transfers/downloads/{username}; we fall back to it automatically and
locate files afterwards by name + size.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

FAILED_STATES = ("Errored", "Rejected", "Cancelled", "TimedOut", "Aborted", "Failed")


class SlskdError(Exception):
    pass


class SlskdBusy(SlskdError):
    """slskd itself is busy/unreachable — not the source's fault; retry the same source later."""


def transfer_failed(state: str) -> bool:
    return "Completed" in state and any(s in state for s in FAILED_STATES)


def transfer_succeeded(state: str) -> bool:
    return "Succeeded" in state


class SlskdClient:
    def __init__(self, url: str, api_key: str, timeout: float = 30):
        self.url = url.rstrip("/")
        self.headers = {"X-API-Key": api_key} if api_key else {}
        self.timeout = timeout
        self._batches_supported: bool | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url + "/api/v0", headers=self.headers, timeout=self.timeout)

    async def status(self) -> dict:
        async with self._client() as c:
            r = await c.get("/application")
            if r.status_code == 401:
                raise SlskdError("slskd rejected the API key (401)")
            r.raise_for_status()
            data = r.json()
            server = data.get("server") or {}
            return {
                "version": (data.get("version") or {}).get("full") or (data.get("version") or {}).get("current"),
                "connected": bool(server.get("isConnected") or "Connected" in str(server.get("state", ""))),
                "logged_in": bool(server.get("isLoggedIn") or "LoggedIn" in str(server.get("state", ""))),
                "username": server.get("username"),
                "state": server.get("state"),
            }

    async def search(self, text: str, seconds: int = 25, response_limit: int = 250,
                     max_peer_queue: int = 50, cleanup: bool = True) -> list[dict]:
        sid = str(uuid.uuid4())
        body = {
            "id": sid,
            "searchText": text,
            "searchTimeout": max(seconds, 5) * 1000,
            "responseLimit": response_limit,
            "fileLimit": 20000,
            "filterResponses": True,
            "maximumPeerQueueLength": max_peer_queue,
            "minimumResponseFileCount": 1,
        }
        async with self._client() as c:
            r = await c.post("/searches", json=body)
            if r.status_code >= 400:
                raise SlskdError(f"search failed ({r.status_code}): {r.text[:200]}")
            deadline = time.monotonic() + seconds
            try:
                while time.monotonic() < deadline:
                    await asyncio.sleep(1.5)
                    s = await c.get(f"/searches/{sid}")
                    if s.status_code == 200:
                        st = s.json()
                        if st.get("isComplete") or "Completed" in str(st.get("state", "")):
                            break
            except asyncio.CancelledError:
                # The job was cancelled/removed: stop and discard the search in slskd, then propagate.
                await asyncio.shield(self._abandon_search(sid))
                raise
            await c.put(f"/searches/{sid}")  # stop if still running (no-op otherwise)
            await asyncio.sleep(0.5)
            rr = await c.get(f"/searches/{sid}/responses")
            responses = rr.json() if rr.status_code == 200 else []
            if cleanup:
                try:
                    await c.delete(f"/searches/{sid}")
                except httpx.HTTPError:
                    pass
            return responses

    async def _abandon_search(self, sid: str) -> None:
        try:
            async with self._client() as c:
                await c.put(f"/searches/{sid}")
                await c.delete(f"/searches/{sid}")
        except httpx.HTTPError:
            pass

    async def enqueue(self, username: str, files: list[dict], destination: str | None = None) -> str:
        """Queue files from one user. Returns 'batch' or 'legacy' (how it was queued).

        Raises SlskdBusy when slskd's own enqueue lock is held (429, or 500 "The wait timed
        out" — it serialises enqueues and blocks while connecting to a slow peer) or it can't
        be reached; raises SlskdError for anything that is the source's fault.
        """
        payload_files = [{"filename": f["filename"], "size": f["size"]} for f in files]

        def busy(r: httpx.Response) -> bool:
            return r.status_code == 429 or (r.status_code >= 500 and "wait timed out" in r.text.lower())

        last = ""
        # Enqueue can block while slskd connects to the peer, so allow a long read timeout.
        async with httpx.AsyncClient(base_url=self.url + "/api/v0", headers=self.headers, timeout=120) as c:
            for attempt in range(3):
                try:
                    if destination and self._batches_supported is not False:
                        r = await c.post("/transfers/downloads/batches", json={
                            "id": str(uuid.uuid4()),
                            "username": username,
                            "files": payload_files,
                            "options": {"destination": destination, "externalId": "narratarr"},
                        })
                        # Older slskd has no /batches route: it either 404/405s, or routes
                        # "batches" into downloads/{username} and 400s on the object body.
                        if r.status_code in (404, 405) or (
                                r.status_code == 400 and "QueueDownloadRequest" in r.text):
                            self._batches_supported = False
                        elif busy(r):
                            last = r.text[:120]
                            await asyncio.sleep(3 * (attempt + 1))
                            continue
                        elif r.status_code in (200, 201, 207):
                            self._batches_supported = True
                            data = r.json() if r.content else {}
                            failures = data.get("failures") or []
                            if failures and len(failures) == len(files):
                                raise SlskdError(f"slskd refused all files: {failures[0].get('message')}")
                            return "batch"
                        else:
                            raise SlskdError(f"enqueue failed ({r.status_code}): {r.text[:200]}")
                    r = await c.post(f"/transfers/downloads/{quote(username, safe='')}", json=payload_files)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last = f"{e.__class__.__name__}: {e}"
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if busy(r):
                    last = r.text[:120]
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise SlskdError(f"enqueue failed ({r.status_code}): {r.text[:200]}")
                return "legacy"
        raise SlskdBusy(f"slskd busy: {last.strip(chr(34))}")

    async def user_downloads(self, username: str) -> list[dict]:
        async with self._client() as c:
            r = await c.get(f"/transfers/downloads/{quote(username, safe='')}")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            out = []
            for d in data.get("directories") or []:
                out.extend(d.get("files") or [])
            return out

    async def cancel(self, username: str, transfer_id: str, remove: bool = False) -> None:
        async with self._client() as c:
            try:
                await c.delete(f"/transfers/downloads/{quote(username, safe='')}/{transfer_id}",
                               params={"remove": str(remove).lower()})
            except httpx.HTTPError as e:
                log.debug("cancel %s failed: %s", transfer_id, e)
