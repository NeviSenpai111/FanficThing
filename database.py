import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "fanfics.db"

# Works joined with the reader's saved position, as the library shows them.
_WORK_LIST_SQL = """
    SELECT w.*, rp.chapter_index AS read_chapter, rp.scroll_pct AS read_scroll
    FROM works w
    LEFT JOIN reading_progress rp ON rp.work_id = w.id
"""


@contextmanager
def _connect():
    """One connection per call: committed on success, always closed.

    Opening per call keeps the sync-from-async usage in app.py simple, and
    WAL mode makes the reopen cheap. Any exception skips the commit, so a
    half-finished write is rolled back when the connection closes.
    """
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_db():
    with _connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS works (
                id INTEGER PRIMARY KEY,
                ao3_id TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                summary TEXT DEFAULT '',
                fandom TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                rating TEXT DEFAULT '',
                word_count INTEGER DEFAULT 0,
                total_chapters TEXT DEFAULT '?/?',
                last_updated TEXT DEFAULT '',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY,
                work_id INTEGER NOT NULL,
                chapter_index INTEGER NOT NULL,
                title TEXT DEFAULT '',
                content TEXT NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
                UNIQUE(work_id, chapter_index)
            );

            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY,
                work_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                mime TEXT NOT NULL,
                data BLOB NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE,
                UNIQUE(work_id, path)
            );

            CREATE TABLE IF NOT EXISTS reading_progress (
                work_id INTEGER PRIMARY KEY,
                chapter_index INTEGER DEFAULT 0,
                scroll_pct REAL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (work_id) REFERENCES works(id) ON DELETE CASCADE
            );
        """)
        # Migrate: add word_count column if missing (existing DBs)
        cols = [r[1] for r in db.execute("PRAGMA table_info(works)").fetchall()]
        if "word_count" not in cols:
            db.execute("ALTER TABLE works ADD COLUMN word_count INTEGER DEFAULT 0")


def upsert_work(ao3_id: str, url: str, title: str, author: str,
                summary: str, fandom: str, tags: list[str],
                rating: str, total_chapters: str, last_updated: str,
                word_count: int = 0) -> int:
    with _connect() as db:
        db.execute("""
            INSERT INTO works (ao3_id, url, title, author, summary, fandom, tags, rating, word_count, total_chapters, last_updated, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ao3_id) DO UPDATE SET
                title=excluded.title, author=excluded.author, summary=excluded.summary,
                fandom=excluded.fandom, tags=excluded.tags, rating=excluded.rating,
                word_count=excluded.word_count,
                total_chapters=excluded.total_chapters, last_updated=excluded.last_updated,
                last_checked=CURRENT_TIMESTAMP
        """, (ao3_id, url, title, author, summary, fandom, json.dumps(tags),
              rating, word_count, total_chapters, last_updated))
        row = db.execute("SELECT id FROM works WHERE ao3_id = ?", (ao3_id,)).fetchone()
        return row["id"]


def upsert_chapter(work_id: int, chapter_index: int, title: str, content: str):
    with _connect() as db:
        db.execute("""
            INSERT INTO chapters (work_id, chapter_index, title, content)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_id, chapter_index) DO UPDATE SET
                title=excluded.title, content=excluded.content
        """, (work_id, chapter_index, title, content))


def upsert_asset(work_id: int, path: str, mime: str, data: bytes):
    """Store one embedded file (an EPUB image) for a work."""
    with _connect() as db:
        db.execute("""
            INSERT INTO assets (work_id, path, mime, data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_id, path) DO UPDATE SET
                mime=excluded.mime, data=excluded.data
        """, (work_id, path, mime, sqlite3.Binary(data)))


def get_assets(work_id: int) -> list[dict]:
    """Every stored asset for a work, for LAN export."""
    with _connect() as db:
        rows = db.execute(
            "SELECT path, mime, data FROM assets WHERE work_id = ?", (work_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_asset(work_id: int, path: str) -> dict | None:
    with _connect() as db:
        row = db.execute(
            "SELECT mime, data FROM assets WHERE work_id = ? AND path = ?",
            (work_id, path),
        ).fetchone()
        return dict(row) if row else None


def _enrich_work(row: sqlite3.Row) -> dict:
    work = dict(row)
    work["tags"] = json.loads(work["tags"]) if work["tags"] else []
    return work


def get_all_works() -> list[dict]:
    with _connect() as db:
        rows = db.execute(_WORK_LIST_SQL + " ORDER BY w.added_at DESC").fetchall()
        return [_enrich_work(r) for r in rows]


def get_work(work_id: int) -> dict | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
        return _enrich_work(row) if row else None


def get_work_by_ao3_id(ao3_id: str) -> dict | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM works WHERE ao3_id = ?", (ao3_id,)).fetchone()
        return _enrich_work(row) if row else None


def get_chapters(work_id: int) -> list[dict]:
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM chapters WHERE work_id = ? ORDER BY chapter_index", (work_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_chapter_titles(work_id: int) -> list[dict]:
    """Index and title only, so the reader's chapter list stays cheap for long novels."""
    with _connect() as db:
        rows = db.execute(
            "SELECT chapter_index, title FROM chapters WHERE work_id = ? ORDER BY chapter_index",
            (work_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_chapter(work_id: int, chapter_index: int) -> dict | None:
    with _connect() as db:
        row = db.execute(
            "SELECT chapter_index, title, content FROM chapters WHERE work_id = ? AND chapter_index = ?",
            (work_id, chapter_index),
        ).fetchone()
        return dict(row) if row else None


def get_chapter_count(work_id: int) -> int:
    with _connect() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM chapters WHERE work_id = ?", (work_id,)
        ).fetchone()
        return row["c"]


def delete_work(work_id: int):
    # The child rows cascade, but delete them explicitly too so databases
    # created before foreign keys were enforced don't leave orphans behind.
    with _connect() as db:
        db.execute("DELETE FROM chapters WHERE work_id = ?", (work_id,))
        db.execute("DELETE FROM assets WHERE work_id = ?", (work_id,))
        db.execute("DELETE FROM reading_progress WHERE work_id = ?", (work_id,))
        db.execute("DELETE FROM works WHERE id = ?", (work_id,))


def save_progress(work_id: int, chapter_index: int, scroll_pct: float):
    with _connect() as db:
        db.execute("""
            INSERT INTO reading_progress (work_id, chapter_index, scroll_pct, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(work_id) DO UPDATE SET
                chapter_index=excluded.chapter_index, scroll_pct=excluded.scroll_pct,
                updated_at=CURRENT_TIMESTAMP
        """, (work_id, chapter_index, scroll_pct))


def get_progress(work_id: int) -> dict | None:
    with _connect() as db:
        row = db.execute(
            "SELECT * FROM reading_progress WHERE work_id = ?", (work_id,)
        ).fetchone()
        return dict(row) if row else None


def search_works(query: str) -> list[dict]:
    # Escape LIKE wildcards so a search for "100%" doesn't match everything.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    q = f"%{escaped}%"
    with _connect() as db:
        rows = db.execute(_WORK_LIST_SQL + """
            WHERE w.title LIKE ? ESCAPE '\\'
               OR w.author LIKE ? ESCAPE '\\'
               OR w.fandom LIKE ? ESCAPE '\\'
               OR w.tags LIKE ? ESCAPE '\\'
            ORDER BY w.added_at DESC
        """, (q, q, q, q)).fetchall()
        return [_enrich_work(r) for r in rows]
