"""EPUB parser.

An EPUB is already the shape this app stores: ordered XHTML documents plus
metadata. So rather than bolting on a separate viewer, we unpack the book
into the same works/chapters rows the scrapers produce — the library card,
the reader, search, progress tracking and LAN sharing then work on
uploaded books with no further changes.

One spine item becomes one chapter. Chapter titles come from the EPUB 3
nav document or the EPUB 2 NCX, falling back to the document's first
heading. Content is sanitized down to plain semantic HTML (the reader
supplies its own typography), and referenced images are extracted so they
can be served back from the DB.
"""

import hashlib
import logging
import posixpath
import re
import zipfile
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

log = logging.getLogger("fanficthing")

CONTAINER_PATH = "META-INF/container.xml"

# Tags whose entire subtree is dropped — presentation, scripting, or
# interactive cruft that has no place in the reader.
_DROP_TREE = (
    "script style link meta title head base "
    "iframe object embed form input button select textarea audio video"
).split()

# Attributes worth keeping. Everything else (class, style, on*, epub:type,
# xml:*, data-*) is stripped so book CSS can't fight the reader's theme.
_KEEP_ATTRS = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "ol": {"start"},
    "col": {"span"},
}

_IMAGE_MIMES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".avif": "image/avif",
}


def is_epub(filename: str, data: bytes) -> bool:
    """Cheap check before doing any real work."""
    if not data.startswith(b"PK"):
        return False
    if (filename or "").lower().endswith(".epub"):
        return True
    try:
        with zipfile.ZipFile(_bytes_io(data)) as z:
            return CONTAINER_PATH in z.namelist()
    except zipfile.BadZipFile:
        return False


def work_id_for(data: bytes) -> str:
    """Content-addressed id, so re-uploading the same file updates the
    existing library entry instead of duplicating it."""
    return "ep_" + hashlib.sha256(data).hexdigest()[:16]


def _bytes_io(data: bytes):
    import io
    return io.BytesIO(data)


def _resolve(base_path: str, href: str) -> str:
    """Resolve an href relative to the document that contains it, returning
    a normalized zip path."""
    href = unquote(urldefrag(href)[0])
    base_dir = posixpath.dirname(base_path)
    return posixpath.normpath(posixpath.join(base_dir, href)).lstrip("/")


class _Zip:
    """Zip wrapper that tolerates the case/encoding drift real EPUBs have."""

    def __init__(self, z: zipfile.ZipFile):
        self._z = z
        self._names = {n.lower(): n for n in z.namelist()}

    def read(self, path: str) -> bytes | None:
        real = self._names.get(path.lower())
        return self._z.read(real) if real else None

    def text(self, path: str) -> str | None:
        raw = self.read(path)
        return raw.decode("utf-8", errors="replace") if raw is not None else None

    def has(self, path: str) -> bool:
        return path.lower() in self._names


def _opf_path(z: _Zip) -> str:
    container = z.text(CONTAINER_PATH)
    if container:
        root = ET.fromstring(container)
        el = root.find(".//{*}rootfile")
        if el is not None and el.get("full-path"):
            return el.get("full-path")
    # Some malformed books skip the container; look for any .opf.
    for lower, real in z._names.items():
        if lower.endswith(".opf"):
            return real
    raise ValueError("Not a valid EPUB: no package document found")


def _meta_text(metadata, tag: str) -> str:
    el = metadata.find(f"{{*}}{tag}")
    return (el.text or "").strip() if el is not None and el.text else ""


def _parse_package(z: _Zip, opf_path: str) -> dict:
    root = ET.fromstring(z.text(opf_path))
    metadata = root.find("{*}metadata")
    manifest = root.find("{*}manifest")
    spine = root.find("{*}spine")
    if manifest is None or spine is None:
        raise ValueError("Not a valid EPUB: package document is incomplete")

    items: dict[str, dict] = {}
    for item in manifest.findall("{*}item"):
        iid, href = item.get("id"), item.get("href")
        if not iid or not href:
            continue
        items[iid] = {
            "path": _resolve(opf_path, href),
            "media_type": (item.get("media-type") or "").lower(),
            "properties": (item.get("properties") or "").split(),
        }

    spine_paths: list[str] = []
    for ref in spine.findall("{*}itemref"):
        item = items.get(ref.get("idref") or "")
        # linear="no" marks front/back matter the reader may skip; keep it,
        # a reader that hides it would just lose content.
        if item and z.has(item["path"]) and item["path"] not in spine_paths:
            spine_paths.append(item["path"])

    authors = [
        (el.text or "").strip()
        for el in (metadata.findall("{*}creator") if metadata is not None else [])
        if (el.text or "").strip()
    ]
    subjects = [
        (el.text or "").strip()
        for el in (metadata.findall("{*}subject") if metadata is not None else [])
        if (el.text or "").strip()
    ]

    return {
        "items": items,
        "spine": spine_paths,
        "toc_id": spine.get("toc"),
        "title": _meta_text(metadata, "title") if metadata is not None else "",
        "authors": authors,
        "subjects": subjects,
        "description": _meta_text(metadata, "description") if metadata is not None else "",
        "publisher": _meta_text(metadata, "publisher") if metadata is not None else "",
        "date": _meta_text(metadata, "date") if metadata is not None else "",
    }


def _toc_titles(z: _Zip, pkg: dict) -> dict[str, str]:
    """Map spine document path -> title, from the EPUB 3 nav doc or the
    EPUB 2 NCX. First entry pointing at a document wins."""
    titles: dict[str, str] = {}

    def record(path: str, text: str):
        text = re.sub(r"\s+", " ", text).strip()
        if path and text and path not in titles:
            titles[path] = text

    nav = next(
        (it for it in pkg["items"].values() if "nav" in it["properties"]), None
    )
    if nav and z.has(nav["path"]):
        soup = BeautifulSoup(z.text(nav["path"]), "html.parser")
        toc_nav = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
        for a in (toc_nav or soup).select("a[href]"):
            record(_resolve(nav["path"], a["href"]), a.get_text(" ", strip=True))

    ncx = pkg["items"].get(pkg["toc_id"] or "")
    if not titles and ncx and z.has(ncx["path"]):
        try:
            root = ET.fromstring(z.text(ncx["path"]))
        except ET.ParseError:
            return titles
        # iterfind, not iter: only ElementPath supports the {*} wildcard.
        for point in root.iterfind(".//{*}navPoint"):
            label = point.find("{*}navLabel/{*}text")
            content = point.find("{*}content")
            if label is not None and content is not None and content.get("src"):
                record(
                    _resolve(ncx["path"], content.get("src")),
                    (label.text or ""),
                )
    return titles


def _sanitize(soup, doc_path: str, index: int, z: _Zip,
              spine_index: dict[str, int], assets: dict) -> None:
    """Strip the document down to safe semantic HTML, rewrite image sources
    to app URLs and internal links to reader anchors."""
    for tag in soup.find_all(_DROP_TREE):
        tag.decompose()

    # <svg><image xlink:href="cover.png"/></svg> is how many books wrap a
    # full-page image. Keep the picture, drop the SVG shell.
    for svg in soup.find_all("svg"):
        inner = svg.find("image")
        href = inner.get("xlink:href") or inner.get("href") if inner else None
        if href:
            # `soup` here is a body Tag, not a BeautifulSoup, so build the
            # replacement through a throwaway parse.
            img = BeautifulSoup("<img/>", "html.parser").img
            img["src"] = href
            svg.replace_with(img)
        else:
            svg.decompose()

    for tag in soup.find_all(True):
        keep = _KEEP_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr == "id":
                # Namespace ids so footnote anchors from different chapters
                # can't collide on the single-page reader.
                tag["id"] = f"ep{index}-{tag['id']}"
            elif attr not in keep:
                del tag[attr]

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            continue
        path = _resolve(doc_path, src)
        ext = posixpath.splitext(path)[1].lower()
        if not z.has(path) or ext not in _IMAGE_MIMES:
            img.decompose()
            continue
        if path not in assets:
            assets[path] = {"mime": _IMAGE_MIMES[ext], "data": z.read(path)}
        img["src"] = "ASSET:" + path
        img["loading"] = "lazy"

    for a in soup.find_all("a"):
        href = a.get("href", "")
        if not href:
            continue
        if href.startswith("#"):
            a["href"] = f"#ep{index}-{href[1:]}"
        elif re.match(r"^(https?|mailto):", href, re.I):
            a["target"] = "_blank"
            a["rel"] = "noopener"
        else:
            target, frag = urldefrag(href)
            path = _resolve(doc_path, target)
            if path in spine_index:
                # Spine position, not chapter index — blank pages get
                # skipped later, so resolution waits for the second pass.
                a["href"] = f"SPINE:{spine_index[path]}" + (f"#{frag}" if frag else "")
            else:
                a.unwrap()  # dead internal link — keep the text, drop the link


def _heading(soup) -> str:
    for level in ("h1", "h2", "h3", "h4", "h5", "h6"):
        el = soup.find(level)
        if el:
            text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if text:
                return text[:200]
    return ""


def parse_epub(data: bytes, filename: str) -> dict:
    """Unpack an EPUB into the app's work/chapter shape.

    Returns {meta, chapters, assets}: `meta` matches what the scrapers
    return, `chapters` is [{index, title, content, word_count}], and
    `assets` is {zip_path: {mime, data}} for referenced images. Image
    sources are left as 'ASSET:<zip_path>' placeholders for the caller to
    rewrite once the work has a database id.
    """
    try:
        zf = zipfile.ZipFile(_bytes_io(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"Not a readable EPUB file: {e}") from e

    with zf:
        z = _Zip(zf)
        pkg = _parse_package(z, _opf_path(z))
        if not pkg["spine"]:
            raise ValueError("EPUB contains no readable documents")

        toc = _toc_titles(z, pkg)
        spine_index = {path: i for i, path in enumerate(pkg["spine"])}
        assets: dict[str, dict] = {}
        chapters: list[dict] = []

        spine_to_chapter: dict[int, int] = {}
        for spine_pos, path in enumerate(pkg["spine"]):
            index = len(chapters)
            raw = z.text(path) or ""
            soup = BeautifulSoup(raw, "html.parser")
            body = soup.body or soup
            _sanitize(body, path, index, z, spine_index, assets)

            text = body.get_text(" ", strip=True)
            if not text and not body.find("img"):
                continue  # blank filler page

            spine_to_chapter[spine_pos] = index
            title = toc.get(path) or _heading(body)
            chapters.append({
                "index": index,
                "title": title,
                "content": "".join(str(c) for c in body.children).strip(),
                "word_count": len(re.findall(r"\w+", text)),
            })

    if not chapters:
        raise ValueError("EPUB contains no readable text")

    # Resolve cross-document links now that spine position -> chapter index
    # is known. A link into a skipped blank page lands on the next real
    # chapter rather than nowhere.
    def _resolve_spine_link(m: re.Match) -> str:
        pos, frag = int(m.group(1)), m.group(2)
        if pos not in spine_to_chapter:
            later = [p for p in spine_to_chapter if p > pos]
            pos = min(later) if later else min(spine_to_chapter)
        idx = spine_to_chapter[pos]
        return f'href="#ep{idx}-{frag}"' if frag else f'href="#chapter-{idx}"'

    for ch in chapters:
        ch["content"] = re.sub(
            r'href="SPINE:(\d+)(?:#([^"]*))?"', _resolve_spine_link, ch["content"]
        )

    total = len(chapters)
    title = pkg["title"] or re.sub(r"\.epub$", "", filename or "", flags=re.I) or "Untitled"
    summary = pkg["description"]
    if summary and "<" not in summary:
        summary = f"<p>{summary}</p>"

    meta = {
        "ao3_id": work_id_for(data),
        "url": "",
        "title": title,
        "author": ", ".join(pkg["authors"]) or "Unknown",
        "summary": summary,
        "fandom": pkg["publisher"],
        "tags": pkg["subjects"],
        "rating": "",
        "word_count": sum(c["word_count"] for c in chapters),
        "total_chapters": f"{total}/{total}",
        "last_updated": (pkg["date"] or "")[:10],
    }
    log.info(f"epub: parsed '{title}' — {total} chapters, {len(assets)} images")
    return {"meta": meta, "chapters": chapters, "assets": assets}
