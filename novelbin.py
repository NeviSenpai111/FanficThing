"""Novelbin scraper.

Novelbin is fronted by Cloudflare, which successfully blocks headless
Chromium from solving its JS challenge on chapter pages. Rather than fight
that, we use `curl_cffi` with Chrome TLS/HTTP2 impersonation to fetch pages
at the HTTP layer — CF's bot detection is mostly fingerprint-based, so a
matching TLS fingerprint passes without a challenge at all.

It also keeps the download loop free of browser overhead.

The site moved from novelbin.com to www.novelbin.cc, which serves the
whole chapter list on the book page (`/book/<slug>/`) instead of the old
`/ajax/chapter-archive` endpoint. Both layouts are handled below; note
that slugs are NOT stable across the move, so a work saved from a
novelbin.com URL has to be re-added from its new .cc link.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

log = logging.getLogger("fanficthing")

BASE = "https://www.novelbin.cc"
# curl_cffi's rolling alias for its newest Chrome fingerprint, so upgrading
# the library keeps the impersonation current without touching this file.
_IMPERSONATE = "chrome"

# Matches every domain/path shape novelbin has used: the current
# .cc/book/<slug> as well as the older .com|.net|.me /b|/novel-book/<slug>.
_URL_RE = re.compile(r"novelbin\.[a-z]{2,}/(?:book|b|novel-book)/([^/?#]+)")

_scrape_lock = asyncio.Lock()

# Polite gap between chapter requests.
_CHAPTER_DELAY_S = 0.3


def is_novelbin_url(url: str) -> bool:
    return bool(_URL_RE.search(url or ""))


def parse_work_id(url: str) -> str | None:
    """Return a stable id like 'nb_<slug>' so the DB schema can key on it."""
    m = _URL_RE.search(url or "")
    return f"nb_{m.group(1)}" if m else None


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


def _strip_noise(el) -> None:
    for tag in el.select(
        "script, style, ins, iframe, noscript, "
        ".ads, .adsbygoogle, [class*='ad-'], [id*='ad-'], [id*='banner']"
    ):
        tag.decompose()


def _chapter_no(url: str) -> int:
    """Chapter number out of a .../chapter-<n>[-slug] URL, 0 if absent."""
    m = re.search(r"/chapter-(\d+)", url.lower())
    return int(m.group(1)) if m else 0


def _chapter_links(roots) -> list[str]:
    """Absolute, de-duplicated chapter URLs from the given soup elements,
    keeping document order."""
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        for a in root.select("a[href]"):
            href = a.get("href", "")
            if "/chapter-" not in href.lower():
                continue
            full = href if href.startswith("http") else BASE + href
            if full not in seen:
                seen.add(full)
                out.append(full)
    return out


async def _get(session: AsyncSession, url: str, *, retries: int = 2) -> str:
    """GET with small retry. Returns the response body text."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = await session.get(url, impersonate=_IMPERSONATE, timeout=30)
            if r.status_code == 200:
                return r.text
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:
            last_err = e
        if attempt < retries:
            await asyncio.sleep(1 + attempt)
    assert last_err is not None
    raise last_err


async def fetch_meta(session: AsyncSession, url: str) -> tuple[dict, list[str]]:
    """Return (work_metadata, list_of_chapter_urls). Caller writes the work
    row then iterates chapter_urls."""
    m = _URL_RE.search(url or "")
    if not m:
        raise ValueError("Invalid novelbin URL")
    slug = m.group(1)
    novel_url = f"{BASE}/book/{slug}/"

    log.info(f"novelbin: fetching meta for '{slug}'")
    html = await _get(session, novel_url)
    soup = BeautifulSoup(html, "html.parser")

    title = (
        _text(soup.select_one(".desc h3.title"))
        or _text(soup.select_one("h3.title"))
        or slug
    )
    author = ""
    genres: list[str] = []
    status = ""
    for li in soup.select(".info-meta li, .info li"):
        label = _text(li.select_one("h3")).rstrip(":").lower()
        if label == "author":
            author = (
                ", ".join(_text(a) for a in li.select("a"))
                or _text(li).replace("Author:", "").strip()
            )
        elif label in ("genre", "genres"):
            genres = [_text(a) for a in li.select("a")]
        elif label == "status":
            status = _text(li).replace("Status:", "").strip()

    summary_el = (
        soup.select_one("#tab-description .desc-text")
        or soup.select_one(".desc-text")
    )
    if summary_el:
        _strip_noise(summary_el)
        summary = "".join(str(c) for c in summary_el.children)
    else:
        summary = ""

    # The book page carries the full chapter list, split across a few
    # .list-chapter columns and already in reading order.
    chapter_urls = _chapter_links(soup.select(".list-chapter"))

    # Older novelbin domains served the list from an ajax endpoint instead.
    if not chapter_urls:
        book_div = soup.select_one("[data-novel-id]")
        novel_id = (book_div.get("data-novel-id") if book_div else None) or slug
        archive_html = await _get(
            session, f"{BASE}/ajax/chapter-archive?novelId={novel_id}"
        )
        chapter_urls = _chapter_links([BeautifulSoup(archive_html, "html.parser")])

    # Last resort: any chapter link anywhere on the page. These include the
    # "latest chapter" shortcuts near the top, so sort by chapter number.
    if not chapter_urls:
        chapter_urls = sorted(_chapter_links([soup]), key=_chapter_no)

    if not chapter_urls:
        raise ValueError("No chapter links found")

    total = len(chapter_urls)
    is_ongoing = "ongoing" in status.lower() or "on-going" in status.lower()

    meta = {
        "ao3_id": f"nb_{slug}",
        "url": novel_url,
        "title": title,
        "author": author or "Unknown",
        "summary": summary,
        "fandom": "",
        "tags": genres,
        "rating": "",
        "word_count": 0,
        "total_chapters": f"{total}/{'?' if is_ongoing else total}",
        "last_updated": status,
    }
    return meta, chapter_urls


def _parse_chapter(html: str, index: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    ch_title = (
        _text(soup.select_one("a.chr-title"))
        or _text(soup.select_one(".chr-text"))
        or _text(soup.select_one("h2"))
        or f"Chapter {index + 1}"
    )
    body = soup.select_one("#chr-content") or soup.select_one(".chapter-content")
    if body is None:
        raise RuntimeError("no #chr-content on page")
    _strip_noise(body)
    if not body.get_text(strip=True):
        raise RuntimeError("#chr-content was empty after strip")

    content = str(body)
    word_count = len(re.findall(r"\w+", body.get_text(" ", strip=True)))
    return {"index": index, "title": ch_title, "content": content, "word_count": word_count}


async def fetch_chapters(
    session: AsyncSession,
    chapter_urls: list[str],
    on_chapter: Callable[[dict], Awaitable[None] | None] | None = None,
    start_index: int = 0,
) -> list[dict]:
    """Download chapters sequentially. If on_chapter is supplied it's invoked
    as each chapter completes (used for streaming into the DB)."""
    out: list[dict] = []
    for i, url in enumerate(chapter_urls):
        idx = start_index + i
        ch: dict | None = None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                html = await _get(session, url, retries=0)
                ch = _parse_chapter(html, idx)
                break
            except Exception as e:
                last_err = e
                log.warning(
                    f"novelbin: chapter {idx + 1} attempt {attempt + 1} failed: {e}"
                )
                await asyncio.sleep(1 + attempt * 2)
        if ch is None:
            log.error(f"novelbin: giving up on chapter {idx + 1}: {last_err}")
            ch = {
                "index": idx,
                "title": f"Chapter {idx + 1}",
                "content": "<p><em>Failed to load this chapter.</em></p>",
                "word_count": 0,
            }

        out.append(ch)
        if on_chapter is not None:
            res = on_chapter(ch)
            if asyncio.iscoroutine(res):
                await res

        await asyncio.sleep(_CHAPTER_DELAY_S)
    return out


async def fetch_work(url: str) -> dict:
    """One-shot fetch. Prefer streaming from app.py for large novels so
    partial progress survives a crash."""
    async with _scrape_lock:
        async with AsyncSession() as session:
            meta, chapter_urls = await fetch_meta(session, url)
            chapters = await fetch_chapters(session, chapter_urls)

    word_count = sum(c.get("word_count", 0) for c in chapters)
    meta["chapters"] = chapters
    meta["word_count"] = word_count
    is_ongoing = "ongoing" in meta.get("last_updated", "").lower()
    meta["total_chapters"] = f"{len(chapters)}/{'?' if is_ongoing else len(chapters)}"
    return meta
