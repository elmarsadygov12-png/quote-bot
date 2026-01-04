import os
import json
import sqlite3
import time
from typing import Optional, Dict, Any, List, Tuple

DB_PATH = os.getenv("DB_PATH", "data.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            gender TEXT NOT NULL DEFAULT 'universal',
            length TEXT NOT NULL DEFAULT 'medium',
            mode TEXT NOT NULL DEFAULT 'clean',
            adult_ok INTEGER NOT NULL DEFAULT 0,
            tone TEXT NOT NULL DEFAULT 'instagram',
            lang TEXT NOT NULL DEFAULT 'ru',
            super_mode INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS quota (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            last_ts REAL NOT NULL DEFAULT 0,
            total_used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS last_analysis (
            user_id INTEGER PRIMARY KEY,
            analysis_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            caption TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """)
        c.commit()


def now() -> float:
    return time.time()


def get_or_create_user(user_id: int) -> Dict[str, Any]:
    ts = now()
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            c.execute(
                "INSERT INTO users (user_id, created_at, updated_at) VALUES (?,?,?)",
                (user_id, ts, ts)
            )
            c.commit()
            row = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)


def update_user(user_id: int, **fields) -> Dict[str, Any]:
    if not fields:
        return get_or_create_user(user_id)
    ts = now()
    sets = ", ".join([f"{k}=?" for k in fields.keys()] + ["updated_at=?"])
    vals = list(fields.values()) + [ts, user_id]
    with _conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE user_id=?", vals)
        c.commit()
    return get_or_create_user(user_id)


def get_quota(user_id: int, day: str) -> Dict[str, Any]:
    with _conn() as c:
        row = c.execute("SELECT * FROM quota WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        if not row:
            c.execute(
                "INSERT INTO quota (user_id, day, used, last_ts, total_used) VALUES (?,?,?,?,?)",
                (user_id, day, 0, 0.0, 0)
            )
            c.commit()
            row = c.execute("SELECT * FROM quota WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        return dict(row)


def update_quota(user_id: int, day: str, used: int, last_ts: float, total_used: int):
    with _conn() as c:
        c.execute("""
        INSERT INTO quota (user_id, day, used, last_ts, total_used)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id, day) DO UPDATE SET
            used=excluded.used,
            last_ts=excluded.last_ts,
            total_used=excluded.total_used
        """, (user_id, day, used, last_ts, total_used))
        c.commit()


def save_analysis(user_id: int, analysis: Dict[str, Any]):
    with _conn() as c:
        c.execute("""
        INSERT INTO last_analysis (user_id, analysis_json, updated_at)
        VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            analysis_json=excluded.analysis_json,
            updated_at=excluded.updated_at
        """, (user_id, json.dumps(analysis, ensure_ascii=False), now()))
        c.commit()


def load_analysis(user_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT analysis_json FROM last_analysis WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["analysis_json"])
        except Exception:
            return None


def add_favorite(user_id: int, caption: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO favorites (user_id, caption, created_at) VALUES (?,?,?)",
            (user_id, caption, now())
        )
        c.commit()


def list_favorites(user_id: int, limit: int = 10) -> List[Tuple[int, str]]:
    with _conn() as c:
        rows = c.execute("""
        SELECT id, caption FROM favorites
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """, (user_id, limit)).fetchall()
        return [(r["id"], r["caption"]) for r in rows]


def count_favorites(user_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM favorites WHERE user_id=?", (user_id,)).fetchone()
        return int(row["n"])
