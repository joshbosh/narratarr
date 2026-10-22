from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .config import DATA_DIR, SettingsStore
from .db import Database
from .jobs import JobManager
from .system import Health, UpdateChecker

logging.basicConfig(level=os.environ.get("NARRATARR_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(DATA_DIR / "narratarr.db")
    settings = SettingsStore(db)
    for p in (settings.incomplete, settings.completed):
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logging.warning("Can't create %s: %s", p, e)
    jobs = JobManager(db, settings)
    app.state.db, app.state.settings, app.state.jobs = db, settings, jobs
    app.state.updates = UpdateChecker(settings)
    app.state.health = Health(settings, jobs)
    await jobs.start()

    async def update_loop():
        while True:
            await app.state.updates.check()
            await asyncio.sleep(3600)

    update_task = asyncio.create_task(update_loop())
    yield
    update_task.cancel()
    await jobs.stop()


app = FastAPI(title="Narratarr", version=__version__, lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    # Single-page app: any non-API route serves index.html
    if path.startswith("api/"):
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})
