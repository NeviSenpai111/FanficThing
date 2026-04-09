import asyncio
import logging
import re
import io
import zipfile

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("fanficthing")

FICHUB_BASE = "https://fichub.net/api/v0"


def parse_work_id(url: str) -> str | None:
    m = re.search(r"archiveofourown\.org/works/(\d+)", url)
    return m.group(1) if m else None


def _extract_chapters(raw_html: str) -> list[dict]:
    """Extract chapters from FicHub HTML using regex splitting.

    BeautifulSoup misparses FicHub's HTML because of malformed nesting
    (e.g. <p><div>...</div></p>), so we split on chapter div boundaries
    in the raw HTML string instead.
    """
    chapter_starts = list(re.finditer(r'<div\s+id="chap_(\d+)">', raw_html))

    if not chapter_starts:
        soup = BeautifulSoup(raw_html, "html.parser")
        body = soup.find("body")
        if not body:
            return [{"index": 0, "title": "", "content": raw_html}]
        for tag in body.select("h1, h2, nav, #contents-list, style"):
            tag.decompose()
        for p in body.find_all("p", recursive=False):
            text = p.get_text(strip=True)
            if any(text.startswith(s) for s in ("Original source:", "Chapters:", "Words:", "Exported with")):
                p.decompose()
            else:
                break
        return [{"index": 0, "title": "", "content": str(body)}]

    chapters = []
    for idx, match in enumerate(chapter_starts):
        start = match.start()
        if idx + 1 < len(chapter_starts):
            end = chapter_starts[idx + 1].start()
        else:
            end = len(raw_html)

        chunk = raw_html[start:end]

        title_match = re.search(r'<h2[^>]*>(.*?)</h2>', chunk, re.DOTALL)
        title = ""
        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()

        chunk = re.sub(
            r'<div\s+class="chapter_nav">.*?</div>\s*<span\s+class="cfix"></span>\s*<a[^>]*>chapter list</a>\s*</div>',
            '', chunk, flags=re.DOTALL
        )
        chunk = re.sub(r'<div\s+class="chapter_nav">.*?</div>', '', chunk, count=1, flags=re.DOTALL)

        if title_match:
            chunk = chunk.replace(title_match.group(0), '', 1)

        chapters.append({
            "index": idx,
            "title": title,
            "content": chunk,
        })

    return chapters


async def _request_fichub_generation(url: str, client: httpx.AsyncClient) -> dict:
    """Request FicHub to generate the download, with retries for long fics.

    FicHub may take a very long time for uncached fics with many chapters.
    We try the epub endpoint with increasing timeouts, and if it keeps
    failing, we retry — FicHub caches partial progress so each attempt
    gets closer to completion.
    """
    last_error = None

    for attempt in range(6):  # up to 6 attempts
        timeout = 60 + (attempt * 30)  # 60s, 90s, 120s, 150s, 180s, 210s
        log.info(f"FicHub attempt {attempt+1}/6 for {url} (timeout={timeout}s)")
        try:
            resp = await client.get(
                f"{FICHUB_BASE}/epub",
                params={"q": url},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            log.info(f"FicHub response: err={data.get('err')}, has_html={bool(data.get('html_url'))}")

            if data.get("err") == 1:
                # Still processing — wait and retry
                log.info("FicHub still processing, waiting 10s...")
                await asyncio.sleep(10)
                continue

            if data.get("html_url"):
                return data

        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            last_error = e
            log.warning(f"FicHub attempt {attempt+1} timed out: {e}")
            await asyncio.sleep(5)
            continue
        except httpx.HTTPStatusError as e:
            log.warning(f"FicHub attempt {attempt+1} HTTP error: {e.response.status_code}")
            if e.response.status_code in (502, 503, 504):
                await asyncio.sleep(10)
                continue
            raise

    raise RuntimeError(
        f"FicHub could not generate the download after multiple attempts. "
        f"The fic may be too long for FicHub to process right now. "
        f"Try again in a few minutes — FicHub caches progress, so retrying often works."
    )


async def fetch_work(url: str, client: httpx.AsyncClient) -> dict:
    """Fetch metadata and full HTML content for an AO3 work via FicHub."""

    # Step 1: Get metadata (with retries — FicHub can be slow for uncached fics)
    meta = None
    for attempt in range(5):
        try:
            log.info(f"Fetching metadata attempt {attempt+1}/5 for {url}")
            meta_resp = await client.get(f"{FICHUB_BASE}/meta", params={"q": url}, timeout=90)
            meta_resp.raise_for_status()
            meta = meta_resp.json()
            break
        except (httpx.TimeoutException, httpx.ReadTimeout) as e:
            log.warning(f"Metadata attempt {attempt+1} timed out: {e}")
            if attempt < 4:
                await asyncio.sleep(5)
                continue
            raise RuntimeError("FicHub is not responding. Try again in a minute.")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (502, 503, 504) and attempt < 4:
                await asyncio.sleep(5)
                continue
            raise

    work_id = parse_work_id(url) or ""
    tags = []
    ext = meta.get("rawExtendedMeta", {})
    for key in ("warning", "relationship", "character", "freeform"):
        tags.extend(ext.get(key, []))

    rating_list = ext.get("rating", [])
    rating = rating_list[0] if rating_list else ""

    stats = ext.get("stats", {})
    total_chapters = stats.get("chapters", "1/1")
    last_updated = stats.get("published", meta.get("updated", ""))
    fandom_list = ext.get("fandom", [])

    # Step 2: Request generation with retries
    epub_data = await _request_fichub_generation(url, client)

    # Step 3: Download the HTML zip
    html_path = epub_data["html_url"]
    html_resp = await client.get(
        f"https://fichub.net{html_path}", timeout=180, follow_redirects=True
    )
    html_resp.raise_for_status()

    # Step 4: Extract HTML from zip
    z = zipfile.ZipFile(io.BytesIO(html_resp.content))
    html_files = [n for n in z.namelist() if n.endswith(".html")]
    if not html_files:
        raise RuntimeError("No HTML file found in FicHub download")

    full_html = z.read(html_files[0]).decode("utf-8")

    # Step 5: Extract chapters
    chapters = _extract_chapters(full_html)

    return {
        "ao3_id": work_id,
        "url": url.split("?")[0],
        "title": meta.get("title", "Untitled"),
        "author": meta.get("author", "Anonymous"),
        "summary": meta.get("description", ""),
        "fandom": ", ".join(fandom_list),
        "tags": tags,
        "rating": rating,
        "total_chapters": total_chapters,
        "last_updated": last_updated,
        "chapters": chapters,
    }
