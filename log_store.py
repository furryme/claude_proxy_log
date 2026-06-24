"""
log_store.py — SQLite-based log storage with background batch writer.

Architecture:
    Proxy threads → queue.put() (non-blocking)
    Writer thread  → batch INSERT every 2s or 50 entries

DB: logs/raw.db, WAL mode for concurrent read/write.
Schema: single `entries` table with gzip-compressed body BLOB + raw response BLOB.

Usage:
    from log_store import enqueue_entry, DB_PATH, get_connection

    # Write (called from request_logger.finish())
    enqueue_entry({
        'seq_id': 1, 'ts': 1718553600.0,
        'method': 'POST', 'path': '/v1/messages',
        'model': 'Qwen3.6-27B', 'upstream': 'http://...',
        'body': b'{...json...}',  # will be gzip-compressed
        'body_len': 12345,
        'response': b'\x1b...',   # raw bytes (brotli/gzip/plaintext)
        'status': 200, 'duration_ms': 5000.0,
        'error': None, 'hour_key': '2026-06-16_17',
    })

    # Read (called from cleaner/viewer)
    from log_store import get_connection
    conn = get_connection()
    rows = conn.execute("SELECT * FROM entries WHERE hour_key = ?", (hk,)).fetchall()
"""

from __future__ import annotations

import gzip
import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

_LOGS_ROOT = Path(os.environ.get("LOGS_ROOT", "logs"))
DB_PATH = _LOGS_ROOT / "raw.db"

logger = logging.getLogger(__name__)

# ── global state ─────────────────────────────────────────────
_db_lock = threading.Lock()
_writer_thread: threading.Thread | None = None
_queue: queue.Queue
_seq_lock = threading.Lock()
_seq_counter = 0
_shutdown_event = threading.Event()


def _next_seq_id() -> int:
    global _seq_counter
    with _seq_lock:
        _seq_counter += 1
        return _seq_counter


def _make_hour_key(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d_%H")


# ── DB init ──────────────────────────────────────────────────
def _init_db():
    """Initialize database, create tables, start writer thread."""
    global _writer_thread, _queue

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            seq_id      INTEGER UNIQUE NOT NULL,
            method      TEXT NOT NULL,
            path        TEXT NOT NULL,
            model       TEXT NOT NULL,
            upstream    TEXT,
            body        BLOB,
            body_len    INTEGER,
            response    BLOB,
            status      INTEGER,
            duration_ms REAL,
            error       TEXT,
            hour_key    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hour ON entries(hour_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON entries(ts)")
    conn.commit()

    # Initialize seq counter from DB to avoid collision with existing records
    global _seq_counter
    try:
        _seq_counter = conn.execute("SELECT COALESCE(MAX(seq_id), 0) FROM entries").fetchone()[0]
    except sqlite3.OperationalError:
        _seq_counter = 0

    conn.close()

    _queue = queue.Queue(maxsize=1000)

    _writer_thread = threading.Thread(target=_writer_loop, daemon=True, name="log-writer")
    _writer_thread.start()


def _writer_loop():
    """Background thread: collect entries from queue, batch INSERT."""
    while not _shutdown_event.is_set():
        batch = _collect_batch(timeout=2.0)
        if not batch:
            continue

        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=10)
            try:
                conn.execute("BEGIN")
                cursor = conn.cursor()
                for entry in batch:
                    try:
                        compressed_body = gzip.compress(
                            entry["body"], compresslevel=6
                        ) if entry.get("body") else None
                        cursor.execute(
                            """INSERT INTO entries
                               (ts, seq_id, method, path, model, upstream,
                                body, body_len, response, status, duration_ms,
                                error, hour_key)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                entry["ts"],
                                entry["seq_id"],
                                entry["method"],
                                entry["path"],
                                entry["model"],
                                entry.get("upstream"),
                                compressed_body,
                                entry.get("body_len"),
                                entry.get("response"),
                                entry.get("status"),
                                entry.get("duration_ms"),
                                entry.get("error"),
                                entry["hour_key"],
                            ),
                        )
                    except sqlite3.IntegrityError:
                        pass  # duplicate seq_id, skip
                    except Exception as e:
                        logger.warning("log_store: insert failed: %s", e)
                conn.commit()
            except Exception as e:
                logger.warning("log_store: batch commit failed: %s", e)
                conn.rollback()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("log_store: DB connection failed: %s", e)


def _collect_batch(timeout: float = 2.0, max_size: int = 50) -> list[dict]:
    """Collect entries from queue. Blocks up to timeout waiting for the first entry."""
    batch = []
    try:
        # Block on first item — thread sleeps here when idle
        batch.append(_queue.get(timeout=timeout))
    except queue.Empty:
        return []

    # Drain quickly, then wait for more
    while len(batch) < max_size:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break

    # Wait a bit more for additional entries
    remaining = timeout - 0.1
    if remaining > 0:
        while len(batch) < max_size:
            try:
                batch.append(_queue.get(timeout=remaining))
            except queue.Empty:
                break

    return batch


def enqueue_entry(entry: dict) -> None:
    """Non-blocking enqueue for background writer.

    Drops entry silently if queue is full (never blocks the proxy).
    """
    try:
        _queue.put_nowait(entry)
    except queue.Full:
        pass  # Drop rather than block the proxy
    except RuntimeError:
        pass  # Queue not initialized yet


def shutdown():
    """Gracefully shut down the writer thread."""
    _shutdown_event.set()
    if _writer_thread and _writer_thread.is_alive():
        _writer_thread.join(timeout=5)


def get_connection() -> sqlite3.Connection:
    """Get a new read connection (safe for concurrent use with WAL mode)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# Auto-init on import
_init_db()
