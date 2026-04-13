import re

from ao3_direct import fetch_work_direct


def parse_work_id(url: str) -> str | None:
    m = re.search(r"archiveofourown\.org/works/(\d+)", url)
    return m.group(1) if m else None


async def fetch_work(url: str) -> dict:
    """Fetch an AO3 work using Playwright headless browser."""
    return await fetch_work_direct(url)
