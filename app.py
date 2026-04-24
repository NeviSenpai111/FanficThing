import asyncio
import json as _json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fanficthing")
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import ao3
import novelbin
import database as db


def _pick_scraper(url: str):
    """Choose the right scraper module for a given source URL."""
    return novelbin if novelbin.is_novelbin_url(url) else ao3


# Track background download jobs: {job_id: {status, title, error, work_id}}
download_jobs: dict[str, dict] = {}
_job_counter = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await ao3.startup()
    try:
        yield
    finally:
        await ao3.shutdown()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    works = db.get_all_works()
    return templates.TemplateResponse(request, "index.html", {"works": works})


async def _do_download(job_id: str, url: str):
    """Background task that downloads a fic.

    For novelbin we stream chapters straight to the DB so a crash partway
    through a 1000-chapter novel doesn't lose everything already scraped.
    """
    try:
        download_jobs[job_id]["status"] = "downloading"

        if novelbin.is_novelbin_url(url):
            from curl_cffi.requests import AsyncSession
            async with novelbin._scrape_lock:
                async with AsyncSession() as session:
                    meta, chapter_urls = await novelbin.fetch_meta(session, url)
                    total = len(chapter_urls)

                    db_id = db.upsert_work(
                        ao3_id=meta["ao3_id"], url=meta["url"], title=meta["title"],
                        author=meta["author"], summary=meta["summary"], fandom=meta["fandom"],
                        tags=meta["tags"], rating=meta["rating"],
                        total_chapters=meta["total_chapters"],
                        last_updated=meta["last_updated"], word_count=0,
                    )

                    saved = 0
                    total_words = 0

                    def on_chapter(ch: dict):
                        nonlocal saved, total_words
                        db.upsert_chapter(db_id, ch["index"], ch["title"], ch["content"])
                        saved += 1
                        total_words += ch.get("word_count", 0)
                        download_jobs[job_id]["progress"] = f"{saved}/{total}"

                    await novelbin.fetch_chapters(
                        session, chapter_urls, on_chapter=on_chapter,
                    )

                    is_ongoing = "ongoing" in meta.get("last_updated", "").lower()
                    final_total = f"{saved}/{'?' if is_ongoing else saved}"
                    db.upsert_work(
                        ao3_id=meta["ao3_id"], url=meta["url"], title=meta["title"],
                        author=meta["author"], summary=meta["summary"], fandom=meta["fandom"],
                        tags=meta["tags"], rating=meta["rating"],
                        total_chapters=final_total,
                        last_updated=meta["last_updated"], word_count=total_words,
                    )

            download_jobs[job_id].update({
                "status": "done", "title": meta["title"],
                "chapters": saved, "work_id": db_id,
            })
            return

        data = await ao3.fetch_work(url)
        db_id = db.upsert_work(
            ao3_id=data["ao3_id"], url=data["url"], title=data["title"],
            author=data["author"], summary=data["summary"], fandom=data["fandom"],
            tags=data["tags"], rating=data["rating"],
            total_chapters=data["total_chapters"], last_updated=data["last_updated"],
            word_count=data.get("word_count", 0),
        )
        for ch in data["chapters"]:
            db.upsert_chapter(db_id, ch["index"], ch["title"], ch["content"])

        download_jobs[job_id].update({
            "status": "done", "title": data["title"],
            "chapters": len(data["chapters"]), "work_id": db_id,
        })
    except Exception as e:
        log.error(f"Download failed for {url}: {e}", exc_info=True)
        download_jobs[job_id].update({
            "status": "error",
            "error": str(e),
        })


@app.post("/api/add")
async def add_work(request: Request):
    global _job_counter
    body = await request.json()
    url = body.get("url", "").strip()
    scraper = _pick_scraper(url)
    work_id = scraper.parse_work_id(url)
    if not work_id:
        return JSONResponse({"detail": "Unsupported URL (expected AO3 or novelbin)"}, status_code=400)

    # Check if already in library
    existing = db.get_work_by_ao3_id(work_id)
    if existing:
        return JSONResponse({
            "status": "done",
            "title": existing["title"],
            "work_id": existing["id"],
            "chapters": db.get_chapter_count(existing["id"]),
        })

    # Guard against duplicate in-flight downloads for the same URL
    for jid, job in download_jobs.items():
        if job.get("url") == url and job.get("status") in ("queued", "downloading"):
            return JSONResponse({"job_id": jid, "status": job["status"]})

    _job_counter += 1
    job_id = f"job_{_job_counter}"
    download_jobs[job_id] = {"status": "queued", "url": url}

    asyncio.create_task(_do_download(job_id, url))

    return JSONResponse({"job_id": job_id, "status": "queued"})


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    job = download_jobs.get(job_id)
    if not job:
        return JSONResponse({"detail": "Job not found"}, status_code=404)

    resp = {"status": job["status"]}
    if job.get("progress"):
        resp["progress"] = job["progress"]

    if job["status"] == "done":
        resp["title"] = job.get("title", "")
        resp["chapters"] = job.get("chapters", 0)
        resp["work_id"] = job.get("work_id")
        del download_jobs[job_id]
    elif job["status"] == "error":
        resp["error"] = job.get("error", "Unknown error")
        del download_jobs[job_id]

    return JSONResponse(resp)


async def _update_one_work(work: dict, stored_count: int) -> dict:
    """Check one work for new chapters and apply them.

    For novelbin, uses the cheap fetch_meta (landing page + chapter archive,
    2 HTTP requests) to discover the current chapter count, then only
    downloads chapters past stored_count. For AO3, the whole work is one
    page so we just refetch and slice as before.

    Returns {updated, total, last_updated}.
    """
    url = work["url"]
    work_id = work["id"]

    if novelbin.is_novelbin_url(url):
        from curl_cffi.requests import AsyncSession
        async with novelbin._scrape_lock:
            async with AsyncSession() as session:
                meta, chapter_urls = await novelbin.fetch_meta(session, url)
                new_total = len(chapter_urls)

                updated = 0
                new_words = 0
                if new_total > stored_count:
                    missing = chapter_urls[stored_count:]

                    def on_chapter(ch: dict):
                        nonlocal updated, new_words
                        db.upsert_chapter(work_id, ch["index"], ch["title"], ch["content"])
                        updated += 1
                        new_words += ch.get("word_count", 0)

                    await novelbin.fetch_chapters(
                        session, missing, on_chapter=on_chapter,
                        start_index=stored_count,
                    )

                final_word_count = (work.get("word_count") or 0) + new_words
                db.upsert_work(
                    ao3_id=meta["ao3_id"], url=meta["url"], title=meta["title"],
                    author=meta["author"], summary=meta["summary"], fandom=meta["fandom"],
                    tags=meta["tags"], rating=meta["rating"],
                    total_chapters=meta["total_chapters"],
                    last_updated=meta["last_updated"],
                    word_count=final_word_count,
                )
        return {"updated": updated, "total": new_total, "last_updated": meta["last_updated"]}

    # AO3: one-page scrape, slice by stored_count.
    data = await ao3.fetch_work(url)
    db.upsert_work(
        ao3_id=data["ao3_id"], url=data["url"], title=data["title"],
        author=data["author"], summary=data["summary"], fandom=data["fandom"],
        tags=data["tags"], rating=data["rating"],
        total_chapters=data["total_chapters"], last_updated=data["last_updated"],
        word_count=data.get("word_count", 0),
    )
    new_count = len(data["chapters"])
    updated = 0
    if new_count > stored_count:
        for ch in data["chapters"][stored_count:]:
            db.upsert_chapter(work_id, ch["index"], ch["title"], ch["content"])
            updated += 1
    return {"updated": updated, "total": new_count, "last_updated": data["last_updated"]}


@app.post("/api/update/{work_id}")
async def update_work(work_id: int):
    work = db.get_work(work_id)
    if not work:
        return JSONResponse({"detail": "Work not found"}, status_code=404)

    stored_count = db.get_chapter_count(work_id)
    try:
        result = await _update_one_work(work, stored_count)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"detail": f"Update check failed: {e}"}, status_code=500)


@app.post("/api/update-all")
async def update_all_works():
    works = db.get_all_works()
    total_new = 0
    updated_works = 0
    failed = 0

    for w in works:
        stored_count = db.get_chapter_count(w["id"])
        try:
            result = await _update_one_work(w, stored_count)
            if result["updated"]:
                total_new += result["updated"]
                updated_works += 1
        except Exception as e:
            log.warning(f"Update-all: failed for {w['title']}: {e}")
            failed += 1

    return JSONResponse({
        "checked": len(works),
        "updated_works": updated_works,
        "new_chapters": total_new,
        "failed": failed,
    })


@app.get("/read/{work_id}", response_class=HTMLResponse)
async def read_work(request: Request, work_id: int):
    work = db.get_work(work_id)
    if not work:
        raise HTTPException(404, "Work not found")
    chapters = db.get_chapters(work_id)
    progress = db.get_progress(work_id)
    return templates.TemplateResponse(request, "reader.html", {
        "work": work, "chapters": chapters, "progress": progress,
    })


@app.post("/api/progress/{work_id}")
async def save_progress(work_id: int, request: Request):
    body = await request.json()
    db.save_progress(work_id, body.get("chapter", 0), body.get("scroll", 0))
    return {"ok": True}


@app.get("/api/progress/{work_id}")
async def get_progress(work_id: int):
    p = db.get_progress(work_id)
    if p:
        return p
    return {"chapter_index": 0, "scroll_pct": 0}


@app.get("/api/search")
async def search_works(q: str = ""):
    if not q.strip():
        return db.get_all_works()
    return db.search_works(q.strip())


@app.get("/api/works")
async def list_works(request: Request):
    _require_share_token(request)
    return db.get_all_works()


@app.delete("/api/works/{work_id}")
async def remove_work(work_id: int):
    db.delete_work(work_id)
    return {"ok": True}


# === LAN peer sharing ===
_PEER_RE = re.compile(r"^[A-Za-z0-9._-]+(?::\d{1,5})?$")
_TOKEN_PATH = Path(__file__).parent / "data" / "share_token.txt"


def _load_or_create_token() -> str:
    if _TOKEN_PATH.exists():
        t = _TOKEN_PATH.read_text().strip()
        if t:
            return t
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(12)
    _TOKEN_PATH.write_text(t)
    return t


SHARE_TOKEN = _load_or_create_token()


def _require_share_token(request: Request):
    supplied = request.headers.get("x-share-token", "")
    if not secrets.compare_digest(supplied, SHARE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing share token")


def _normalize_peer(peer: str) -> str | None:
    peer = (peer or "").strip()
    if peer.startswith("http://"):
        peer = peer[7:]
    elif peer.startswith("https://"):
        peer = peer[8:]
    peer = peer.rstrip("/")
    if not _PEER_RE.match(peer):
        return None
    return peer


async def _peer_get(peer: str, path: str, token: str):
    url = f"http://{peer}{path}"
    def _fetch():
        req = urllib.request.Request(url, headers={"X-Share-Token": token})
        with urllib.request.urlopen(req, timeout=20) as r:
            return _json.loads(r.read().decode("utf-8"))
    return await asyncio.to_thread(_fetch)


@app.get("/api/share-token")
async def share_token():
    """Local-UI helper: expose this instance's share token so the owner can copy it."""
    return {"token": SHARE_TOKEN}


@app.get("/api/export/{work_id}")
async def export_work(work_id: int, request: Request):
    _require_share_token(request)
    work = db.get_work(work_id)
    if not work:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    chapters = db.get_chapters(work_id)
    return {
        "work": {
            "ao3_id": work["ao3_id"], "url": work["url"], "title": work["title"],
            "author": work["author"], "summary": work["summary"],
            "fandom": work["fandom"], "tags": work["tags"], "rating": work["rating"],
            "word_count": work.get("word_count", 0),
            "total_chapters": work["total_chapters"],
            "last_updated": work["last_updated"],
        },
        "chapters": [
            {"index": c["chapter_index"], "title": c["title"], "content": c["content"]}
            for c in chapters
        ],
    }


@app.post("/api/peer/list")
async def peer_list(request: Request):
    body = await request.json()
    peer = _normalize_peer(body.get("peer", ""))
    token = (body.get("token") or "").strip()
    if not peer:
        return JSONResponse({"detail": "Invalid peer address"}, status_code=400)
    if not token:
        return JSONResponse({"detail": "Peer token is required"}, status_code=400)
    try:
        works = await _peer_get(peer, "/api/works", token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return JSONResponse({"detail": "Peer rejected the token."}, status_code=401)
        return JSONResponse({"detail": f"Peer error: {e}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"detail": f"Couldn't reach peer: {e}"}, status_code=502)

    have = {w["ao3_id"] for w in db.get_all_works()}
    slim = [{
        "id": w["id"], "title": w["title"], "author": w["author"],
        "fandom": w.get("fandom", ""),
        "total_chapters": w.get("total_chapters", ""),
        "word_count": w.get("word_count", 0),
        "ao3_id": w["ao3_id"],
        "already_have": w["ao3_id"] in have,
    } for w in works]
    return {"works": slim, "peer": peer}


@app.post("/api/peer/import")
async def peer_import(request: Request):
    body = await request.json()
    peer = _normalize_peer(body.get("peer", ""))
    token = (body.get("token") or "").strip()
    work_id = body.get("work_id")
    if not peer or not isinstance(work_id, int):
        return JSONResponse({"detail": "Invalid request"}, status_code=400)
    if not token:
        return JSONResponse({"detail": "Peer token is required"}, status_code=400)
    try:
        payload = await _peer_get(peer, f"/api/export/{work_id}", token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return JSONResponse({"detail": "Peer rejected the token."}, status_code=401)
        return JSONResponse({"detail": f"Peer error: {e}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"detail": f"Fetch failed: {e}"}, status_code=502)

    w = payload["work"]
    db_id = db.upsert_work(
        ao3_id=w["ao3_id"], url=w["url"], title=w["title"],
        author=w["author"], summary=w["summary"], fandom=w["fandom"],
        tags=w["tags"], rating=w["rating"],
        total_chapters=w["total_chapters"], last_updated=w["last_updated"],
        word_count=w.get("word_count", 0),
    )
    for ch in payload["chapters"]:
        db.upsert_chapter(db_id, ch["index"], ch["title"], ch["content"])
    return {
        "ok": True, "title": w["title"],
        "chapters": len(payload["chapters"]), "work_id": db_id,
    }


WALLBASH_PATH = Path.home() / ".cache" / "hyde" / "wall.dcol"


def _parse_wallbash() -> dict[str, str] | None:
    """Parse HyDE wallbash colors from wall.dcol."""
    if not WALLBASH_PATH.exists():
        return None
    colors = {}
    for line in WALLBASH_PATH.read_text().splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        colors[key.strip()] = val.strip().strip('"')
    return colors


@app.get("/api/theme.css")
async def wallbash_theme():
    """Serve CSS variables derived from HyDE wallbash colors."""
    colors = _parse_wallbash()
    if not colors:
        return Response("/* no wallbash colors found */", media_type="text/css")

    # Dark theme colors from wallbash palette
    bg = colors.get("dcol_pry1", "0f0f17")
    bg2 = colors.get("dcol_1xa1", "161624")
    bg3 = colors.get("dcol_1xa2", "1e1e32")
    accent = colors.get("dcol_1xa6", "a78bfa")
    accent2 = colors.get("dcol_1xa8", "c4b5fd")
    text = colors.get("dcol_txt1", "e8e6f0")
    text2 = colors.get("dcol_1xa5", "8b88a2")
    text3 = colors.get("dcol_1xa3", "5c5a6e")
    card_border = colors.get("dcol_1xa2", "1e1e32")

    # Light theme from the lighter end of the palette
    light_bg = colors.get("dcol_pry4", "faf9f7")
    light_bg2 = colors.get("dcol_1xa9", "f0eeeb")
    light_accent = colors.get("dcol_1xa4", "7c3aed")
    light_text = colors.get("dcol_txt4", "1a1a2e")

    css = f""":root {{
    --bg: #{bg};
    --bg2: #{bg2};
    --bg3: #{bg3};
    --card: rgba(255, 255, 255, 0.04);
    --card-hover: rgba(255, 255, 255, 0.07);
    --card-border: #{card_border}88;
    --accent: #{accent};
    --accent2: #{accent2};
    --accent-glow: #{accent}26;
    --accent-dim: #{accent}14;
    --text: #{text};
    --text2: #{text2};
    --text3: #{text3};
}}

.light-theme {{
    --bg: #{light_bg};
    --bg2: #{light_bg2};
    --bg3: #{light_bg2};
    --card: rgba(255, 255, 255, 0.7);
    --card-hover: rgba(255, 255, 255, 0.9);
    --card-border: #{light_accent}22;
    --accent: #{light_accent};
    --accent2: #{light_accent};
    --accent-glow: #{light_accent}22;
    --accent-dim: #{light_accent}0d;
    --text: #{light_text};
    --text2: #{light_accent};
    --text3: #{light_accent}88;
}}
"""
    return Response(css, media_type="text/css")


@app.get("/favicon.svg")
async def favicon():
    """SVG favicon — a stylized book icon tinted with the wallbash accent."""
    colors = _parse_wallbash() or {}
    accent = colors.get("dcol_1xa6", "a78bfa")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#{accent}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>
<line x1="8" y1="7" x2="16" y2="7"/>
<line x1="8" y1="11" x2="14" y2="11"/>
</svg>'''
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/theme-mtime")
async def wallbash_mtime():
    """Return modification time of wall.dcol for live-reload polling."""
    if WALLBASH_PATH.exists():
        return {"mtime": WALLBASH_PATH.stat().st_mtime}
    return {"mtime": 0}
