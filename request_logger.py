"""
request_logger.py — Buffers request/response data and queues to SQLite.

Proxy API (unchanged):
    logger = RequestLogger(method, path, body_bytes, upstream_url, model)
    logger.add_response_chunk(raw_bytes)  # called once with full response
    logger.finish(status=200, error="")   # non-blocking queue.put()

All I/O is async via background writer thread in log_store.py.
Proxy thread only does queue.put_nowait() — zero blocking.
"""

from __future__ import annotations

import time
from typing import Any

# Lazy import — proxy works fine if log_store is unavailable
try:
    from log_store import _next_seq_id, _make_hour_key, enqueue_entry
except ImportError:
    enqueue_entry = None
    _next_seq_id = lambda: 0
    _make_hour_key = lambda ts: ""


class RequestLogger:
    """Buffers a single request/response cycle, queues to SQLite on finish().

    All fields stored in memory until finish() — no I/O until then.
    finish() is non-blocking (just queue.put_nowait).
    """

    def __init__(
        self,
        method: str,
        path: str,
        body_bytes: bytes | None = None,
        upstream_url: str = "",
        model: str = "",
    ):
        self.seq_id = _next_seq_id()
        self.start_time = time.time()
        self.method = method
        self.path = path
        self.upstream = upstream_url
        self.model = model
        self.body_bytes = body_bytes or b""
        self.body_len = len(self.body_bytes)
        self._response_parts: list[bytes] = []

    def add_response_chunk(self, raw_bytes: bytes, tag: str = "response_body"):
        """Append a response chunk. Stores raw bytes (may be brotli/gzip/plaintext)."""
        self._response_parts.append(raw_bytes)

    def finish(self, status: int | None = None, error: str = ""):
        """Mark complete and queue to background writer. Non-blocking."""
        if enqueue_entry is None:
            return

        response = b"".join(self._response_parts) if self._response_parts else None
        duration_ms = (time.time() - self.start_time) * 1000
        hour_key = _make_hour_key(self.start_time)

        entry: dict[str, Any] = {
            "seq_id": self.seq_id,
            "ts": self.start_time,
            "method": self.method,
            "path": self.path,
            "model": self.model,
            "upstream": self.upstream,
            "body": self.body_bytes,
            "body_len": self.body_len,
            "response": response,
            "status": status,
            "duration_ms": duration_ms,
            "error": error or None,
            "hour_key": hour_key,
        }

        try:
            enqueue_entry(entry)
        except Exception:
            pass  # Never break the proxy on logging errors
