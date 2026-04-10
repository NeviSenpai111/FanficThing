import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "fanfics.db"


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    db = get_db()
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
    db.commit()
    db.close()


def upsert_work(ao3_id: str, url: str, title: str, author: str,
                summary: str, fandom: str, tags: list[str],
                rating: str, total_chapters: str, last_updated: str,
                word_count: int = 0) -> int:
    db = get_db()
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
    db.commit()
    work_id = row["id"]
    db.close()
    return work_id


def upsert_chapter(work_id: int, chapter_index: int, title: str, content: str):
    db = get_db()
    db.execute("""
        INSERT INTO chapters (work_id, chapter_index, title, content)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(work_id, chapter_index) DO UPDATE SET
            title=excluded.title, content=excluded.content
    """, (work_id, chapter_index, title, content))
    db.commit()
    db.close()


def _enrich_work(row: dict) -> dict:
    row["tags"] = json.loads(row["tags"]) if row["tags"] else []
    return row


def get_all_works() -> list[dict]:
    db = get_db()
    rows = db.execute("""
        SELECT w.*, rp.chapter_index as read_chapter, rp.scroll_pct as read_scroll
        FROM works w
        LEFT JOIN reading_progress rp ON rp.work_id = w.id
        ORDER BY w.added_at DESC
    """).fetchall()
    result = [_enrich_work(dict(r)) for r in rows]
    db.close()
    return result


def get_work(work_id: int) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
    if not row:
        db.close()
        return None
    result = _enrich_work(dict(row))
    db.close()
    return result


def get_work_by_ao3_id(ao3_id: str) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM works WHERE ao3_id = ?", (ao3_id,)).fetchone()
    if not row:
        db.close()
        return None
    result = _enrich_work(dict(row))
    db.close()
    return result


def get_chapters(work_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM chapters WHERE work_id = ? ORDER BY chapter_index", (work_id,)
    ).fetchall()
    result = [dict(r) for r in rows]
    db.close()
    return result


def get_chapter_count(work_id: int) -> int:
    db = get_db()
    row = db.execute("SELECT COUNT(*) as c FROM chapters WHERE work_id = ?", (work_id,)).fetchone()
    db.close()
    return row["c"]


def delete_work(work_id: int):
    db = get_db()
    db.execute("DELETE FROM chapters WHERE work_id = ?", (work_id,))
    db.execute("DELETE FROM reading_progress WHERE work_id = ?", (work_id,))
    db.execute("DELETE FROM works WHERE id = ?", (work_id,))
    db.commit()
    db.close()


def save_progress(work_id: int, chapter_index: int, scroll_pct: float):
    db = get_db()
    db.execute("""
        INSERT INTO reading_progress (work_id, chapter_index, scroll_pct, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(work_id) DO UPDATE SET
            chapter_index=excluded.chapter_index, scroll_pct=excluded.scroll_pct,
            updated_at=CURRENT_TIMESTAMP
    """, (work_id, chapter_index, scroll_pct))
    db.commit()
    db.close()


def get_progress(work_id: int) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM reading_progress WHERE work_id = ?", (work_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def search_works(query: str) -> list[dict]:
    db = get_db()
    q = f"%{query}%"
    rows = db.execute("""
        SELECT w.*, rp.chapter_index as read_chapter, rp.scroll_pct as read_scroll
        FROM works w
        LEFT JOIN reading_progress rp ON rp.work_id = w.id
        WHERE w.title LIKE ? OR w.author LIKE ? OR w.fandom LIKE ? OR w.tags LIKE ?
        ORDER BY w.added_at DESC
    """, (q, q, q, q)).fetchall()
    result = [_enrich_work(dict(r)) for r in rows]
    db.close()
    return result
