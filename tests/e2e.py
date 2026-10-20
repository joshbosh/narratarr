"""End-to-end test against tests/mocks/mock_server.py.

    python tests/e2e.py   (starts the mock + Narratarr on spare ports, runs scenarios)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
import tempfile
import time
from pathlib import Path

import httpx
import mutagen

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("E2E_WORK") or tempfile.mkdtemp(prefix="narratarr-e2e-"))
WORK.mkdir(parents=True, exist_ok=True)
MOCK_PORT, APP_PORT = 9100, 8787
APP = f"http://127.0.0.1:{APP_PORT}"


def start():
    shutil.rmtree(WORK / "config", ignore_errors=True)
    shutil.rmtree(WORK / "downloads", ignore_errors=True)
    shutil.rmtree(WORK / "slskd-downloads", ignore_errors=True)
    mock = subprocess.Popen([sys.executable, str(ROOT / "tests/mocks/mock_server.py"), str(WORK), str(MOCK_PORT)])
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "NARRATARR_DATA_DIR": str(WORK / "config"),
        "NARRATARR_AUDIBLE_BASE_OVERRIDE": f"http://127.0.0.1:{MOCK_PORT}/{{region}}",
        "NARRATARR_METADATA__AUDNEXUS_URL": f"http://127.0.0.1:{MOCK_PORT}/audnexus",
        "NARRATARR_SLSKD__URL": f"http://127.0.0.1:{MOCK_PORT}",
        "NARRATARR_SLSKD__API_KEY": "testkey",
        "NARRATARR_METADATA__REGION": "au",
        "NARRATARR_METADATA__FALLBACK_REGION": "us",
        "NARRATARR_SLSKD__DOWNLOADS_PATH": str(WORK / "slskd-downloads"),
        "NARRATARR_PATHS__DOWNLOADS": str(WORK / "downloads"),
        "NARRATARR_SLSKD__SEARCH_SECONDS": "5",
        "NARRATARR_GENERAL__POLL_SECONDS": "1",
        "NARRATARR_BUSY_WAIT": "2",
        "NARRATARR_ABS_WATCHER_WAIT": "8",
    }
    app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(APP_PORT), "--log-level", "warning"],
                           cwd=ROOT, env=env, stdout=open(WORK / "app.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            httpx.get(f"{APP}/api/system/status", timeout=1).raise_for_status()
            httpx.get(f"http://127.0.0.1:{MOCK_PORT}/au/1.0/catalog/products", timeout=1)
            return mock, app
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise SystemExit("servers didn't start")


def wait_status(c, jid, wanted, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in wanted:
            return j
        time.sleep(0.5)
    raise AssertionError(f"job {jid} stuck in {j['status']}: {j['detail']}\n" + "\n".join(l['msg'] for l in j['log']))


def search(c, q):
    return c.get("/api/search", params={"q": q}).json()["results"]


def request(c, book):
    r = c.post("/api/jobs", json={"asin": book["asin"], "region": book["region"], "book": book})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def ok(msg):
    print(f"  ✓ {msg}")


def main():
    mock, app = start()
    failures = 0
    try:
        c = httpx.Client(base_url=APP, timeout=30)
        assert c.post("/api/settings/test/slskd").json()["ok"]
        ok("slskd connection test")

        # 1 — narrator-aware search, single m4b, batch destination, chapters embedded
        print("Scenario 1: single-file m4b, narrator-specific edition")
        res = search(c, "tiny test")
        assert {r["asin"] for r in res} == {"B0TINY0001", "B0TINY0002"}, res
        book = next(r for r in res if r["narrators"] == ["Ray Reader"])
        assert book["series"][0]["name"] == "Tiny Saga" and book["region"] == "au"
        assert "._SL500_" not in book["cover_url"]
        ok("Audible AU search returns both narrations; hi-res cover URL")
        jid = request(c, book)
        assert c.post("/api/jobs", json={"asin": book["asin"]}).status_code == 409
        ok("duplicate request rejected")
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["status"], j["detail"], [l["msg"] for l in j["log"]])
        assert j["selected"]["username"] == "goodpeer", j["selected"]
        out = Path(j["output_path"])
        assert out == WORK / "downloads/completed/Jane Author/Tiny Saga/Book 1 - The Tiny Test_ A Novel" or out.name.startswith("Book 1 - The Tiny Test"), out
        f = out / "The Tiny Test.m4b"
        assert f.exists(), list(out.iterdir())
        tags = mutagen.File(str(f))
        assert tags["\xa9alb"] == ["The Tiny Test: A Novel"], tags.get("\xa9alb")
        assert tags["\xa9wrt"] == ["Ray Reader"] and tags["\xa9ART"] == ["Jane Author"]
        assert tags["\xa9mvn"] == ["Tiny Saga"] and tags["----:com.apple.iTunes:ASIN"][0] == b"B0TINY0001"
        assert tags.get("covr"), "cover not embedded"
        probe = json.loads(subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", str(f)],
                                          capture_output=True, text=True).stdout)
        assert [ch["tags"]["title"] for ch in probe["chapters"]] == ["Opening Credits", "Chapter 1", "Chapter 2", "End Credits"], probe
        for side in ("cover.jpg", "desc.txt", "reader.txt", "metadata.opf", "metadata.json", ".narratarr.json"):
            assert (out / side).exists(), side
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        assert meta["narrators"] == ["Ray Reader"] and meta["series"] == ["Tiny Saga #1"] and len(meta["chapters"]) == 4
        assert "opf:role=\"nrt\">Ray Reader" in (out / "metadata.opf").read_text(encoding="utf-8")
        assert not any((WORK / "slskd-downloads").rglob("*.m4b")), "slskd downloads not cleaned"
        ok(f"downloaded from goodpeer → {out.relative_to(WORK)} with tags, cover, 4 chapters, sidecars")

        # 2 — legacy slskd (no batches), multi-disc mp3, names preserved
        print("Scenario 2: multi-disc mp3 via legacy enqueue")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"batches": False})
        book = search(c, "long road home")[0]
        jid = request(c, book)
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        out = Path(j["output_path"])
        files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.mp3"))
        assert files == ["CD1/1-01 Long Road Home.mp3", "CD1/1-02 Long Road Home.mp3",
                         "CD2/2-01 Long Road Home.mp3", "CD2/2-02 Long Road Home.mp3"], files
        t = mutagen.File(str(out / "CD2/2-02 Long Road Home.mp3"))
        assert str(t["TPOS"]) == "2/2" and str(t["TRCK"]) == "4/4" and str(t["TALB"]) == "Long Road Home"
        assert str(t["TCOM"]) == "Kim Narrator" and str(t["TXXX:SERIES"]) == "Road Series" and str(t["TXXX:SERIES-PART"]) == "2"
        ok("4 files across CD1/CD2 kept their names; disc/track numbers + narrator/series tags set")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"batches": True})

        # 3 — failing source → automatic fallback to next candidate
        print("Scenario 3: source fails, next candidate used")
        book = search(c, "fallback book")[0]
        jid = request(c, book)
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 120)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        assert j["selected"]["username"] == "steadypeer", j["selected"]
        assert any("retry" in l["msg"] for l in j["log"]) and any("next candidate" in l["msg"] for l in j["log"])
        ok("flakypeer errored (2 retries), fell back to steadypeer and completed")

        # 3b — slskd busy (500 "wait timed out") must not burn through sources
        print("Scenario 3b: slskd busy → back off, same source")
        c.delete(f"/api/jobs/{jid}")
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/config", json={"busy": 5})
        jid = request(c, book)
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 120)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        assert j["selected"]["username"] == "flakypeer" or j["selected"]["username"] == "steadypeer", j["selected"]
        assert any("slskd busy" in l["msg"] for l in j["log"]), [l["msg"] for l in j["log"]]
        assert not any("trying next candidate" in l["msg"] and "busy" in l["msg"] for l in j["log"])
        assert len(j["selected"]["tried"]) <= 2, j["selected"]["tried"]
        ok("5 busy responses → backed off and retried the same source, no candidates burned")

        # 4 — no results → needs_review, custom search, manual pick
        print("Scenario 4: nothing found → needs review → custom search → pick")
        book = search(c, "nowhere to be found")[0]
        jid = request(c, book)
        j = wait_status(c, jid, {"needs_review", "failed"}, 90)
        assert j["status"] == "needs_review" and not j["candidates"], j["detail"]
        assert len(j["attempts"]) >= 3
        ok(f"{len(j['attempts'])} query variations tried, then parked for review")
        c.post(f"/api/jobs/{jid}/search", json={"query": "jane author everything"}).raise_for_status()
        j = wait_status(c, jid, {"needs_review"}, 30)
        time.sleep(0.5)
        j = wait_status(c, jid, {"needs_review"}, 30)
        assert j["candidates"], j
        cand = j["candidates"][0]
        c.post(f"/api/jobs/{jid}/select", json={"candidate_id": cand["id"]}).raise_for_status()
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 60)
        assert j["status"] == "completed", j["detail"]
        ok(f"manual pick of {cand['display_dir']} (score {cand['score']}) completed")

        # 5 — manual import
        print("Scenario 5: manual import from incomplete/")
        src = WORK / "downloads/incomplete/Ima Porter - Imported Tales (2020) [64kbps]"
        src.mkdir(parents=True, exist_ok=True)
        shutil.copy(WORK / "src/part.mp3", src / "01 - part one.mp3")
        shutil.copy(WORK / "src/part.mp3", src / "02 - part two.mp3")
        folders = c.get("/api/import/scan", params={"root": "incomplete"}).json()
        f = next(x for x in folders if x["name"].startswith("Ima Porter"))
        assert f["suggestion"] == "Ima Porter Imported Tales", f["suggestion"]
        book = search(c, f["suggestion"])[0]
        r = c.post("/api/import", json={"root": "incomplete", "rel": f["rel"], "asin": book["asin"], "region": book["region"], "mode": "move", "book": book})
        r.raise_for_status()
        j = wait_status(c, r.json()["id"], {"completed", "failed"}, 30)
        assert j["status"] == "completed", j
        out = Path(j["output_path"])
        assert (out / "01 - part one.mp3").exists() and not src.exists()
        ok(f"folder name cleaned to “{f['suggestion']}”, imported to {out.relative_to(WORK)}")
        assert c.post("/api/import", json={"root": "incomplete", "rel": "../../..", "asin": "x"}).status_code == 400
        ok("path traversal rejected")

        # 6 — region fallback
        print("Scenario 6: AU → US fallback")
        res = search(c, "american exclusive")
        assert res and res[0]["region"] == "us"
        ok("US result returned when AU has none")

        # 7 — library
        lib = c.get("/api/library").json()["narratarr"]
        assert len(lib) == 5, len(lib)
        assert c.get(f"/api/library/{lib[0]['id']}/cover").status_code == 200
        c.post("/api/library/rescan").raise_for_status()
        assert len(c.get("/api/library").json()["narratarr"]) == 5
        ok("library lists 5 books, covers served, rescan idempotent")
        assert c.get("/queue").status_code == 200 and "Narratarr" in c.get("/library").text
        ok("SPA routes served")

        # 7 — Audiobookshelf
        print("Scenario 7: Audiobookshelf integration")
        abs_local = WORK / "abs-library"
        settings = {"abs.enabled": True, "abs.url": f"http://127.0.0.1:{MOCK_PORT}/abs", "abs.token": "abstoken",
                    "abs.library_id": "lib_books", "abs.folder_id": "fol_main", "abs.local_path": str(abs_local),
                    "abs.delete_after_import": True, "abs.metadata_mode": "fill"}
        c.put("/api/settings", json=settings).raise_for_status()
        t = c.post("/api/settings/test/abs").json()
        assert t["ok"], t
        ok(f"connection test: {t['message']}")

        res = {r["asin"]: r for r in search(c, "existing story") + search(c, "shelf classic") + search(c, "shelf bound")}
        assert res["B0EXIST001"]["in_abs"] and res["B0EXIST001"]["in_abs"]["link"].endswith("/item/li_exist"), res["B0EXIST001"]
        assert res["B0CLASS001"]["in_abs"], "title+author match for ABS item without ASIN"
        assert not res["B0SHELF001"]["in_abs"]
        ok("Search flags books already in Audiobookshelf (by ASIN, and by title+author when ABS has no ASIN)")

        jid = request(c, res["B0SHELF001"])
        j = wait_status(c, jid, {"completed", "failed", "needs_review"}, 90)
        assert j["status"] == "completed", (j["detail"], [l["msg"] for l in j["log"]])
        for _ in range(60):
            j = c.get(f"/api/jobs/{jid}").json()
            if j["abs_status"] != "pending":
                break
            time.sleep(1)
        assert j["abs_status"] == "imported", (j["abs_status"], j["abs_detail"], [l["msg"] for l in j["log"]])
        dest = abs_local / "Abe Shelver/Shelf Saga/Book 3 - Shelf Bound"
        assert (dest / "Shelf Bound.m4b").exists() and (dest / "cover.jpg").exists(), list(abs_local.rglob("*"))
        assert not (WORK / "downloads/completed/Abe Shelver").exists(), "completed copy should be gone (delete on)"
        item = httpx.get(f"http://127.0.0.1:{MOCK_PORT}/mock/abs/items").json()[j["abs_item_id"]]
        m = item["meta"]
        assert m["asin"] == "B0SHELF001" and m["narrators"] == ["Nia Reader"], m
        assert m["series"] == [{"name": "Shelf Saga", "sequence": "3"}] and m["genres"], m
        assert m["publisher"] == "Scanner Pub", "fill mode must keep Audiobookshelf's existing value"
        assert any("Kept Audiobookshelf's publisher" in l["msg"] for l in j["log"])
        assert [ch["title"] for ch in item["chapters"]] == ["Opening", "Middle", "End"], item["chapters"]
        lib = c.get("/api/library").json()
        assert not any(b.get("asin") == "B0SHELF001" for b in lib["narratarr"]), "removed from Narratarr library"
        shelf = next(b for b in lib["audiobookshelf"] if b["asin"] == "B0SHELF001")
        assert shelf["via_narratarr"] == 1
        ok("moved into Audiobookshelf folder (path-mapped), scanned, ASIN/narrator/series/genres filled, "
           "publisher conflict kept + logged, 3 chapters added, removed from Narratarr")

        # copy mode: send an existing Narratarr book
        c.put("/api/settings", json={"abs.delete_after_import": False}).raise_for_status()
        road = next(b for b in lib["narratarr"] if b["title"] == "Long Road Home")
        r = c.post(f"/api/library/{road['id']}/abs")
        r.raise_for_status()
        rj = r.json()["job_id"]
        for _ in range(60):
            j = c.get(f"/api/jobs/{rj}").json()
            if j["abs_status"] not in ("pending", None):
                break
            time.sleep(1)
        assert j["abs_status"] == "imported", (j["abs_detail"], [l["msg"] for l in j["log"]])
        assert (abs_local / "Sam Writer/Road Series/Book 2 - Long Road Home/CD2/2-02 Long Road Home.mp3").exists()
        assert Path(road["path"]).exists(), "copy mode keeps Narratarr's copy"
        lib = c.get("/api/library").json()
        road2 = next(b for b in lib["narratarr"] if b["id"] == road["id"])
        assert road2["abs_status"] == "imported" and road2["abs_link"]
        assert not any(b["title"] == "Long Road Home" for b in lib["audiobookshelf"]), "linked items aren't listed twice"
        ok("copy mode: CD1/CD2 copied, Narratarr keeps its copy and links to the Audiobookshelf item")

        # pending + retry: folder not mounted, then fixed
        c.put("/api/settings", json={"abs.local_path": str(WORK / "not-mounted")}).raise_for_status()
        imp = next(b for b in lib["narratarr"] if b["title"] == "Imported Tales")
        pj = c.post(f"/api/library/{imp['id']}/abs").json()["job_id"]
        for _ in range(20):
            j = c.get(f"/api/jobs/{pj}").json()
            if j.get("abs_detail") and "mounted" in j["abs_detail"]:
                break
            time.sleep(0.5)
        assert j["abs_status"] == "pending" and "mounted" in j["abs_detail"], j["abs_detail"]
        assert any(x["id"] == pj for x in c.get("/api/jobs?scope=active").json()), "pending ABS jobs stay visible"
        c.put("/api/settings", json={"abs.local_path": str(abs_local)}).raise_for_status()
        c.post("/api/abs/sync").raise_for_status()
        for _ in range(60):
            j = c.get(f"/api/jobs/{pj}").json()
            if j["abs_status"] == "imported":
                break
            time.sleep(1)
        assert j["abs_status"] == "imported", j["abs_detail"]
        ok("unmounted folder → pending with a clear reason, retried automatically after the fix")

        # watcher off → falls back to a full library scan
        httpx.post(f"http://127.0.0.1:{MOCK_PORT}/mock/abs", json={"watcher": False})
        tiny = next(b for b in lib["narratarr"] if b["asin"] == "B0TINY0001")
        wj = c.post(f"/api/library/{tiny['id']}/abs").json()["job_id"]
        for _ in range(120):
            j = c.get(f"/api/jobs/{wj}").json()
            if j["abs_status"] == "imported":
                break
            time.sleep(1)
        assert j["abs_status"] == "imported" and any("library scan" in l["msg"] for l in j["log"]), j["abs_detail"]
        ok("folder watcher off → fell back to a library scan and still matched the item")
        assert c.get(f"/api/abs/items/li_exist/cover").status_code == 200
        ok("Audiobookshelf covers proxied")
    except AssertionError as e:
        failures += 1
        print("FAIL:", e)
    finally:
        app.terminate()
        mock.terminate()
    print("\nApp log tail:\n" + "".join((WORK / "app.log").read_text(encoding="utf-8", errors="replace").splitlines(True)[-15:]) if failures else "\nAll scenarios passed.")
    print(f"workdir: {WORK}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
