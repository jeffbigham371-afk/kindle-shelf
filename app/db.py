"""SQLite storage: books, users, sessions, per-user reading state, settings."""

import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row

BUSY = ("queued", "downloading", "converting")


def q(sql, params=(), one=False):
    with _lock:
        cur = _db.execute(sql, params)
        rows = cur.fetchall()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


def x(sql, params=()):
    with _lock:
        cur = _db.execute(sql, params)
        _db.commit()
        return cur


def _columns(table):
    return {r["name"] for r in q(f"PRAGMA table_info({table})")}


def migrate():
    tables = {r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
    if "jobs" in tables and "books" not in tables:
        x("ALTER TABLE jobs RENAME TO books")
    x("""CREATE TABLE IF NOT EXISTS books (
        id TEXT PRIMARY KEY, created REAL, status TEXT, message TEXT, source TEXT,
        title TEXT, author TEXT, files TEXT DEFAULT '{}', sent_at REAL, auto_send INTEGER DEFAULT 0)""")
    extra = {
        "description": "TEXT", "series": "TEXT", "series_index": "REAL", "language": "TEXT",
        "published": "TEXT", "tags": "TEXT", "cover": "TEXT", "added_by": "INTEGER",
        "path": "TEXT", "meta_version": "INTEGER DEFAULT 0", "cover_url": "TEXT",
    }
    have = _columns("books")
    for col, typ in extra.items():
        if col not in have:
            x(f"ALTER TABLE books ADD COLUMN {col} {typ}")
    x("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE COLLATE NOCASE, name TEXT,
        pw_hash TEXT, kindle_email TEXT, is_admin INTEGER DEFAULT 0, auto_send INTEGER DEFAULT 1,
        created REAL)""")
    x("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id INTEGER, created REAL, expires REAL)""")
    x("""CREATE TABLE IF NOT EXISTS user_books (
        user_id INTEGER, book_id TEXT, status TEXT, favorite INTEGER DEFAULT 0,
        sent_at REAL, updated REAL, PRIMARY KEY (user_id, book_id))""")
    x("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    # Books that were mid-flight when the server stopped will never finish.
    x("UPDATE books SET status='error', message='Interrupted by server restart - try adding it again' "
      f"WHERE status IN {BUSY}")


# ---------------------------------------------------------------- books

def book_row(r):
    if r is None:
        return None
    r["files"] = json.loads(r.get("files") or "{}")
    r["tags"] = json.loads(r["tags"]) if r.get("tags") else []
    return r


def get_book(book_id):
    return book_row(q("SELECT * FROM books WHERE id=?", (book_id,), one=True))


def update_book(book_id, **fields):
    for k in ("files", "tags"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    cols = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE books SET {cols} WHERE id=?", (*fields.values(), book_id))


def insert_book(book_id, **fields):
    fields = {"id": book_id, "created": time.time(), "status": "queued",
              "message": "Waiting in line…", **fields}
    cols = ", ".join(fields)
    x(f"INSERT INTO books ({cols}) VALUES ({','.join('?' * len(fields))})", tuple(fields.values()))


def list_books(user_id):
    rows = q("""SELECT b.*, ub.status AS read_status, ub.favorite, ub.sent_at AS my_sent_at
                FROM books b LEFT JOIN user_books ub ON ub.book_id=b.id AND ub.user_id=?
                ORDER BY b.created DESC""", (user_id,))
    return [book_row(r) for r in rows]


def get_user_book(user_id, book_id):
    return q("SELECT * FROM user_books WHERE user_id=? AND book_id=?", (user_id, book_id), one=True) or {}


def set_user_book(user_id, book_id, **fields):
    cur = get_user_book(user_id, book_id)
    merged = {"status": cur.get("status"), "favorite": cur.get("favorite") or 0,
              "sent_at": cur.get("sent_at"), **fields}
    x("""INSERT INTO user_books (user_id, book_id, status, favorite, sent_at, updated)
         VALUES (?,?,?,?,?,?)
         ON CONFLICT(user_id, book_id) DO UPDATE SET status=excluded.status,
           favorite=excluded.favorite, sent_at=excluded.sent_at, updated=excluded.updated""",
      (user_id, book_id, merged["status"], merged["favorite"], merged["sent_at"], time.time()))


# ---------------------------------------------------------------- settings

def get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r and r["value"] not in (None, "") else default


def set_setting(key, value):
    x("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, "" if value is None else str(value)))
