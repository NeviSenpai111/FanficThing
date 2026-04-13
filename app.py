import asyncio
import logging
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fanficthing")
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import ao3
import database as db


# Track background download jobs: {job_id: {status, title, error, work_id}}
download_jobs: dict[str, dict] = {}
_job_counter = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    works = db.get_all_works()
    return templates.TemplateResponse(request, "index.html", {"works": works})


async def _do_download(job_id: str, url: str):
    """Background task that downloads a fic."""
    try:
        download_jobs[job_id]["status"] = "downloading"
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
            "status": "done",
            "title": data["title"],
            "chapters": len(data["chapters"]),
            "work_id": db_id,
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
    work_id = ao3.parse_work_id(url)
    if not work_id:
        return JSONResponse({"detail": "Invalid AO3 URL"}, status_code=400)

    # Check if already in library
    existing = db.get_work_by_ao3_id(work_id)
    if existing:
        return JSONResponse({
            "status": "done",
            "title": existing["title"],
            "work_id": existing["id"],
            "chapters": db.get_chapter_count(existing["id"]),
        })

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

    if job["status"] == "done":
        resp["title"] = job.get("title", "")
        resp["chapters"] = job.get("chapters", 0)
        resp["work_id"] = job.get("work_id")
        del download_jobs[job_id]
    elif job["status"] == "error":
        resp["error"] = job.get("error", "Unknown error")
        del download_jobs[job_id]

    return JSONResponse(resp)


@app.post("/api/update/{work_id}")
async def update_work(work_id: int):
    work = db.get_work(work_id)
    if not work:
        return JSONResponse({"detail": "Work not found"}, status_code=404)

    stored_count = db.get_chapter_count(work_id)

    try:
        data = await ao3.fetch_work(work["url"])

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

        return JSONResponse({
            "updated": updated,
            "total": new_count,
            "last_updated": data["last_updated"],
        })
    except Exception as e:
        return JSONResponse({"detail": f"Update check failed: {e}"}, status_code=500)


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
async def list_works():
    return db.get_all_works()


@app.delete("/api/works/{work_id}")
async def remove_work(work_id: int):
    db.delete_work(work_id)
    return {"ok": True}
