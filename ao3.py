"""AO3 scraper using Playwright headless browser."""

import logging
import re

from playwright.async_api import async_playwright, Browser, Playwright
from bs4 import BeautifulSoup

log = logging.getLogger("fanficthing")

AO3_BASE = "https://archiveofourown.org"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"

_playwright: Playwright | None = None
_browser: Browser | None = None


async def startup() -> None:
    """Launch a persistent headless browser to reuse across scrapes."""
    global _playwright, _browser
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True)
    log.info("Playwright browser launched")


async def shutdown() -> None:
    global _playwright, _browser
    if _browser:
        try:
            await _browser.close()
        except Exception as e:
            log.warning(f"Browser close failed (already dead?): {e}")
        _browser = None
    if _playwright:
        try:
            await _playwright.stop()
        except Exception as e:
            log.warning(f"Playwright stop failed: {e}")
        _playwright = None


async def _get_browser() -> Browser:
    """Return the shared browser, (re)launching it if missing/closed."""
    global _browser
    if _browser is None or not _browser.is_connected():
        await startup()
    return _browser  # type: ignore[return-value]


def parse_work_id(url: str) -> str | None:
    m = re.search(r"archiveofourown\.org/works/(\d+)", url)
    return m.group(1) if m else None


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


async def fetch_work(url: str) -> dict:
    """Scrape an AO3 work using the shared headless browser."""
    work_id_match = re.search(r"/works/(\d+)", url)
    if not work_id_match:
        raise ValueError("Invalid AO3 URL")
    work_id = work_id_match.group(1)

    work_url = f"{AO3_BASE}/works/{work_id}?view_adult=true&view_full_work=true"
    log.info(f"Scraping AO3 work {work_id}")

    browser = await _get_browser()
    context = await browser.new_context(user_agent=USER_AGENT)
    page = await context.new_page()

    try:
        await page.goto(work_url, wait_until="domcontentloaded", timeout=120000)
        html = await page.content()

        if "This work could have adult content" in html:
            btn = page.locator("#tos_agree_adult")
            if await btn.count() > 0:
                await btn.click()
                await page.wait_for_load_state("domcontentloaded")
                html = await page.content()
    finally:
        await context.close()

    soup = BeautifulSoup(html, "html.parser")

    title = _text(soup.select_one("h2.title.heading")) or "Untitled"
    author = _text(soup.select_one("a[rel='author']")) or "Anonymous"

    summary_tag = soup.select_one(".summary .userstuff")
    summary = "".join(str(c) for c in summary_tag.children) if summary_tag else ""

    fandom = ", ".join(_text(t) for t in soup.select(".fandom.tags a.tag"))

    all_tags = [
        _text(t) for t in soup.select(
            ".warning.tags a.tag, .relationship.tags a.tag, "
            ".character.tags a.tag, .freeform.tags a.tag"
        )
    ]

    rating = _text(soup.select_one(".rating.tags a.tag"))
    total_chapters = _text(soup.select_one("dd.chapters")) or "1/1"
    last_updated = _text(soup.select_one("dd.status") or soup.select_one("dd.published"))

    word_str = _text(soup.select_one("dd.words")).replace(",", "")
    word_count = int(word_str) if word_str.isdigit() else 0

    chapter_divs = soup.select("#chapters > .chapter")
    chapters = []

    if chapter_divs:
        for i, div in enumerate(chapter_divs):
            heading = div.select_one(".chapter.preface h3.title a")
            ch_title = _text(heading) if heading else ""

            userstuff = div.select_one(".userstuff[role='article']")
            if not userstuff:
                for candidate in div.select(".userstuff"):
                    if candidate.name != "blockquote" and not candidate.find_parent("blockquote"):
                        userstuff = candidate
                        break

            if userstuff:
                for h3 in userstuff.select("h3.landmark"):
                    h3.decompose()
                content = str(userstuff)
            else:
                content = "<p>Could not extract chapter content.</p>"

            chapters.append({"index": i, "title": ch_title, "content": content})
    else:
        userstuff = soup.select_one(".userstuff[role='article']") or soup.select_one("#chapters .userstuff")
        if userstuff:
            for h3 in userstuff.select("h3.landmark"):
                h3.decompose()
            content = str(userstuff)
        else:
            content = "<p>Could not extract chapter content.</p>"
        chapters.append({"index": 0, "title": title, "content": content})

    log.info(f"Scraped '{title}' - {len(chapters)} chapters")

    return {
        "ao3_id": work_id,
        "url": f"{AO3_BASE}/works/{work_id}",
        "title": title,
        "author": author,
        "summary": summary,
        "fandom": fandom,
        "tags": all_tags,
        "rating": rating,
        "word_count": word_count,
        "total_chapters": total_chapters,
        "last_updated": last_updated,
        "chapters": chapters,
    }
