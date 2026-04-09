import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "fanfics.db"


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
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
    """)
    db.commit()
    db.close()


def upsert_work(ao3_id: str, url: str, title: str, author: str,
                summary: str, fandom: str, tags: list[str],
                rating: str, total_chapters: str, last_updated: str) -> int:
    db = get_db()
    db.execute("""
        INSERT INTO works (ao3_id, url, title, author, summary, fandom, tags, rating, total_chapters, last_updated, last_checked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(ao3_id) DO UPDATE SET
            title=excluded.title, author=excluded.author, summary=excluded.summary,
            fandom=excluded.fandom, tags=excluded.tags, rating=excluded.rating,
            total_chapters=excluded.total_chapters, last_updated=excluded.last_updated,
            last_checked=CURRENT_TIMESTAMP
    """, (ao3_id, url, title, author, summary, fandom, json.dumps(tags),
          rating, total_chapters, last_updated))
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


def get_all_works() -> list[dict]:
    db = get_db()
    rows = db.execute("SELECT * FROM works ORDER BY added_at DESC").fetchall()
    result = [dict(r) for r in rows]
    db.close()
    for r in result:
        r["tags"] = json.loads(r["tags"]) if r["tags"] else []
    return result


def get_work(work_id: int) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM works WHERE id = ?", (work_id,)).fetchone()
    if not row:
        db.close()
        return None
    result = dict(row)
    result["tags"] = json.loads(result["tags"]) if result["tags"] else []
    db.close()
    return result


def get_work_by_ao3_id(ao3_id: str) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM works WHERE ao3_id = ?", (ao3_id,)).fetchone()
    if not row:
        db.close()
        return None
    result = dict(row)
    result["tags"] = json.loads(result["tags"]) if result["tags"] else []
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
    db.execute("DELETE FROM works WHERE id = ?", (work_id,))
    db.commit()
    db.close()
