"""SQLite-opslag voor batches, producten, afbeeldingen en verwerkingsstappen."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, style TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER REFERENCES batches(id),
  status TEXT NOT NULL DEFAULT 'queued',
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  style TEXT,
  title TEXT, description_html TEXT, product_type TEXT, category TEXT,
  tags TEXT, seo_title TEXT, seo_description TEXT, handle TEXT,
  vendor TEXT, price TEXT,
  analysis_json TEXT,
  shopify_product_id TEXT, shopify_admin_url TEXT,
  next_attempt_at REAL, publish_attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL, started_at REAL, analyzed_at REAL, published_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER REFERENCES products(id),
  position INTEGER,
  original_path TEXT, processed_path TEXT, mask_path TEXT,
  use_processed INTEGER NOT NULL DEFAULT 1,
  integrity_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER REFERENCES products(id),
  step TEXT, status TEXT, attempt INTEGER,
  started_at REAL, ended_at REAL, duration_ms INTEGER,
  message TEXT,
  model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn = connect()
_conn.executescript(SCHEMA)
for _ddl in ("ALTER TABLE products ADD COLUMN dims_json TEXT",
             "ALTER TABLE images ADD COLUMN kind TEXT NOT NULL DEFAULT 'product'"):
    try:
        _conn.execute(_ddl)
    except sqlite3.OperationalError:
        pass  # kolom bestaat al
_conn.commit()


@contextmanager
def tx():
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def query(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def insert(table: str, **values) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    with tx() as c:
        return c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())).lastrowid


def update(table: str, row_id: int, **values) -> None:
    if table == "products":
        values["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in values)
    with tx() as c:
        c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))


def claim(waiting: tuple[str, ...], running: str, counter: str) -> dict | None:
    """Pak atomair het volgende product dat klaarstaat (respecteert backoff-tijd)."""
    marks = ",".join("?" for _ in waiting)
    with tx() as c:
        row = c.execute(
            f"SELECT * FROM products WHERE status IN ({marks}) "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT 1",
            (*waiting, time.time()),
        ).fetchone()
        if not row:
            return None
        now = time.time()
        c.execute(
            f"UPDATE products SET status=?, {counter}={counter}+1, updated_at=?, "
            "started_at=COALESCE(started_at, ?) WHERE id=?",
            (running, now, now, row["id"]),
        )
        return dict(row) | {counter: row[counter] + 1}


def product_full(pid: int) -> dict | None:
    p = one("SELECT * FROM products WHERE id = ?", (pid,))
    if not p:
        return None
    p["tags"] = json.loads(p["tags"] or "[]")
    p["analysis"] = json.loads(p.pop("analysis_json") or "null")
    p["dims"] = json.loads(p.pop("dims_json") or "null")
    p["images"] = query("SELECT * FROM images WHERE product_id = ? ORDER BY position", (pid,))
    for im in p["images"]:
        im["integrity"] = json.loads(im.pop("integrity_json") or "null")
    p["events"] = query("SELECT * FROM events WHERE product_id = ? ORDER BY id", (pid,))
    return p
