"""Direct AO3 scraper using Playwright as a fallback when FicHub can't find a fic."""

import asyncio
import logging
import re

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

log = logging.getLogger("fanficthing")

AO3_BASE = "https://archiveofourown.org"


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


async def fetch_work_direct(url: str) -> dict:
    """Scrape an AO3 work directly using a headless browser.

    Uses ?view_full_work=true to get all chapters in one request.
    """
    work_id_match = re.search(r"/works/(\d+)", url)
    if not work_id_match:
        raise ValueError("Invalid AO3 URL")
    work_id = work_id_match.group(1)

    # view_full_work=true loads all chapters on one page
    work_url = f"{AO3_BASE}/works/{work_id}?view_adult=true&view_full_work=true"

    log.info(f"Direct AO3 scrape starting for work {work_id}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"
        )
        page = await context.new_page()

        try:
            await page.goto(work_url, wait_until="domcontentloaded", timeout=120000)
            html = await page.content()

            # Handle adult content gate
            if "This work could have adult content" in html:
                btn = page.locator("#tos_agree_adult")
                if await btn.count() > 0:
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded")
                    html = await page.content()

        finally:
            await browser.close()

    soup = BeautifulSoup(html, "html.parser")

    # Extract metadata
    title_tag = soup.select_one("h2.title.heading")
    title = _text(title_tag) or "Untitled"

    author_tag = soup.select_one("a[rel='author']")
    author = _text(author_tag) or "Anonymous"

    summary_tag = soup.select_one(".summary .userstuff")
    summary = "".join(str(c) for c in summary_tag.children) if summary_tag else ""

    fandom_tags = soup.select(".fandom.tags a.tag")
    fandom = ", ".join(_text(t) for t in fandom_tags)

    all_tags = []
    for tag in soup.select(".warning.tags a.tag, .relationship.tags a.tag, .character.tags a.tag, .freeform.tags a.tag"):
        all_tags.append(_text(tag))

    rating_tag = soup.select_one(".rating.tags a.tag")
    rating = _text(rating_tag)

    chapters_tag = soup.select_one("dd.chapters")
    total_chapters = _text(chapters_tag) or "1/1"

    updated_tag = soup.select_one("dd.status") or soup.select_one("dd.published")
    last_updated = _text(updated_tag)

    words_tag = soup.select_one("dd.words")
    word_str = _text(words_tag).replace(",", "")
    word_count = int(word_str) if word_str.isdigit() else 0

    # Extract chapters from the full work page
    chapters = []
    chapter_divs = soup.select("#chapters > .chapter")

    if chapter_divs:
        for i, div in enumerate(chapter_divs):
            # Chapter title
            heading = div.select_one(".chapter.preface h3.title a")
            ch_title = _text(heading) if heading else ""

            # Chapter body — use role='article' to get the actual text,
            # not the summary blockquote which also has class .userstuff
            userstuff = div.select_one(".userstuff[role='article']")
            if not userstuff:
                # Fallback: get the .userstuff that's NOT inside a blockquote
                for candidate in div.select(".userstuff"):
                    if candidate.name != "blockquote" and not candidate.find_parent("blockquote"):
                        userstuff = candidate
                        break
            content = ""
            if userstuff:
                for h3 in userstuff.select("h3.landmark"):
                    h3.decompose()
                content = str(userstuff)
            else:
                content = "<p>Could not extract chapter content.</p>"

            chapters.append({
                "index": i,
                "title": ch_title,
                "content": content,
            })
    else:
        # Single chapter work
        userstuff = soup.select_one(".userstuff[role='article']")
        if not userstuff:
            userstuff = soup.select_one("#chapters .userstuff")
        content = ""
        if userstuff:
            for h3 in userstuff.select("h3.landmark"):
                h3.decompose()
            content = str(userstuff)
        else:
            content = "<p>Could not extract chapter content.</p>"

        chapters.append({
            "index": 0,
            "title": title,
            "content": content,
        })

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
