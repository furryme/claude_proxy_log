"""
dataset_cleaner.py — Clean raw proxy logs into a training-ready dataset.

Reads from SQLite (logs/raw.db), decompresses body/response, reconstructs SSE.
Writes: logs/cleaned/YYYY-MM-DD_HH/dataset.jsonl

Each output line:
    {
        "request": { "model": "...", "messages": [...], ... },
        "response": { "text": "...", "thinking": "...", "is_streaming": true, ... },
        "metadata": { "seq_id": 1, "model": "...", "duration_ms": 1234, ... }
    }
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import brotli
except ImportError:
    brotli = None

from log_store import get_connection, DB_PATH

LOGS_ROOT = Path(os.environ.get("LOGS_ROOT", "logs"))


# ── decompression ────────────────────────────────────────────
def _decompress(raw_bytes: bytes) -> str:
    """Decompress response bytes (gzip/brotli/plaintext)."""
    if raw_bytes[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw_bytes).decode("utf-8", errors="replace")
        except Exception:
            pass
    if brotli:
        try:
            return brotli.decompress(raw_bytes).decode("utf-8", errors="replace")
        except Exception:
            pass
    return raw_bytes.decode("utf-8", errors="replace")


# ── SSE parsing ──────────────────────────────────────────────
def _parse_sse_data(frame: str) -> dict | None:
    for line in frame.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except (json.JSONDecodeError, TypeError):
                return None
    return None


def _extract_content(content: list) -> tuple[str, str]:
    texts, thinkings = [], []
    for cb in content:
        if not isinstance(cb, dict):
            continue
        if cb.get("type") == "text":
            texts.append(cb.get("text", ""))
        elif cb.get("type") == "thinking":
            thinkings.append(cb.get("thinking", ""))
    return "".join(texts), "".join(thinkings)


# ── response reconstruction ──────────────────────────────────
def _is_streaming_response(text: str) -> bool:
    return any(m in text for m in ("content_block_delta", "message_start", "message_delta"))


def _reconstruct_streaming(text: str) -> dict[str, Any]:
    text_parts, thinking_parts = [], []
    usage: dict = {}
    stop_reason: str | None = None
    content_blocks_count = 0

    for frame in text.split("\n\n"):
        obj = _parse_sse_data(frame.strip())
        if not obj:
            continue
        et = obj.get("type", "")

        if et == "message_start":
            msg = obj.get("message", {})
            if msg.get("usage"):
                usage.update(msg["usage"])
        elif et == "content_block_start":
            cb = obj.get("content_block", {})
            content_blocks_count += 1
            if cb.get("type") == "text" and cb.get("text"):
                text_parts.append(cb["text"])
            if cb.get("type") == "thinking" and cb.get("thinking"):
                thinking_parts.append(cb["thinking"])
        elif et == "content_block_delta":
            delta = obj.get("delta", {})
            dt = delta.get("type", "")
            if dt == "text_delta" and delta.get("text"):
                text_parts.append(delta["text"])
            elif dt == "thinking_delta" and delta.get("thinking"):
                thinking_parts.append(delta["thinking"])
        elif et == "message_delta":
            if obj.get("usage"):
                usage.update(obj["usage"])
            delta = obj.get("delta", {})
            stop_reason = delta.get("stop_reason", stop_reason)

    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts),
        "is_streaming": True,
        "usage": usage,
        "stop_reason": stop_reason,
        "content_blocks_count": content_blocks_count,
    }


def _reconstruct_non_streaming(text: str) -> dict[str, Any]:
    # Try plain JSON
    try:
        obj = json.loads(text)
        content = obj.get("content", [])
        text_out, thinking = _extract_content(content)
        if text_out or thinking:
            return {
                "text": text_out,
                "thinking": thinking,
                "is_streaming": False,
                "usage": obj.get("usage", {}),
                "stop_reason": obj.get("stop_reason"),
            }
    except json.JSONDecodeError:
        pass

    # Try SSE
    for frame in text.split("\n\n"):
        obj = _parse_sse_data(frame.strip())
        if not obj:
            continue
        et = obj.get("type", "")
        if et == "message":
            t, th = _extract_content(obj.get("content", []))
            return {"text": t, "thinking": th, "is_streaming": False,
                    "usage": obj.get("usage", {}), "stop_reason": obj.get("stop_reason")}
        if et == "message_start":
            msg = obj.get("message", {})
            if msg.get("content"):
                t, th = _extract_content(msg["content"])
                if t or th:
                    return {"text": t, "thinking": th, "is_streaming": False,
                            "usage": msg.get("usage", {}), "stop_reason": msg.get("stop_reason")}
    return {}


# ── record-level processing ──────────────────────────────────
def _clean_request_body(body_str: str) -> dict | None:
    try:
        body = json.loads(body_str) if isinstance(body_str, str) else body_str
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    result = {}
    for k in ("messages", "model", "system"):
        if k in body:
            result[k] = body[k]
    for k in ("max_tokens", "temperature", "top_p", "top_k", "tools",
              "tool_choice", "thinking", "betas"):
        if k in body:
            result[k] = body[k]
    return result


def _should_keep(row) -> str | None:
    if row["status"] != 200:
        return f"status={row['status']}"
    if row.get("error"):
        return f"error={row['error']}"
    if row.get("method", "").upper() != "POST":
        return f"non_post={row['method']}"
    if "/v1/messages" not in row.get("path", ""):
        return f"not_messages_path={row['path']}"
    return None


# ── main processing ──────────────────────────────────────────
def _process_entry(row) -> dict | None:
    """Process one DB row, return cleaned record or None."""
    skip = _should_keep(row)
    if skip:
        return None

    # Decompress and parse body
    try:
        body_str = gzip.decompress(row["body"]).decode("utf-8") if row.get("body") else ""
    except Exception:
        body_str = ""

    cleaned_body = _clean_request_body(body_str)
    if not cleaned_body or not cleaned_body.get("messages"):
        return None

    # Decompress and reconstruct response
    response_raw = row.get("response")
    if not response_raw:
        return None

    text_data = _decompress(response_raw)
    response = _reconstruct_streaming(text_data) if _is_streaming_response(text_data) else _reconstruct_non_streaming(text_data)

    resp_text = (response.get("text") or "").strip()
    resp_thinking = (response.get("thinking") or "").strip()
    if len(resp_text) < 2 and len(resp_thinking) < 2:
        return None

    return {
        "request": cleaned_body,
        "response": {
            "text": response.get("text", ""),
            "thinking": response.get("thinking", ""),
            "is_streaming": response.get("is_streaming", False),
            "usage": response.get("usage", {}),
            "stop_reason": response.get("stop_reason"),
        },
        "metadata": {
            "seq_id": row["seq_id"],
            "model": row.get("model", cleaned_body.get("model", "")),
            "status": row.get("status"),
            "duration_ms": row.get("duration_ms"),
        },
    }


def clean_time_window(
    db_path: str | Path,
    hour_key: str,
    output_dir: str | Path,
    min_output_len: int = 2,
) -> dict[str, int]:
    """Clean all entries for a time window, write dataset.jsonl."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "files_processed": 0,
        "records_total": 0,
        "records_kept": 0,
        "skipped": defaultdict(int),
        "records_deduped": 0,
    }

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE hour_key = ? ORDER BY seq_id", (hour_key,)
        ).fetchall()
    finally:
        conn.close()

    stats["files_processed"] = 1  # one "source": the DB
    seen_hashes: set[str] = set()
    all_records: list[dict] = []

    for row in rows:
        rec = _process_entry(dict(row))
        if rec is None:
            continue

        stats["records_total"] += 1

        # Dedup
        input_key = json.dumps(
            {"model": rec["request"].get("model"), "messages": rec["request"].get("messages")},
            sort_keys=True, ensure_ascii=False,
        )
        h = hashlib.md5(input_key.encode()).hexdigest()
        if h in seen_hashes:
            stats["records_deduped"] += 1
            continue
        seen_hashes.add(h)

        resp_text = rec["response"]["text"].strip()
        resp_thinking = rec["response"].get("thinking", "").strip()
        if len(resp_text) < min_output_len and len(resp_thinking) < min_output_len:
            stats["skipped"]["short_output"] += 1
            continue

        stats["records_kept"] += 1
        all_records.append(rec)

    out_file = output_dir / "dataset.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[cleaner] Source:   SQLite ({hour_key})", file=sys.stderr)
    print(f"[cleaner] Records:  {stats['records_total']}", file=sys.stderr)
    print(f"[cleaner] Kept:     {stats['records_kept']}", file=sys.stderr)
    print(f"[cleaner] Deduped:  {stats['records_deduped']}", file=sys.stderr)
    skips = dict(stats["skipped"])
    if skips:
        print(f"[cleaner] Skipped:  {skips}", file=sys.stderr)
    print(f"[cleaner] Output:   {out_file}", file=sys.stderr)

    return stats


# Legacy compatibility — clean_directory delegates to clean_time_window
def clean_directory(input_dir: str | Path, output_dir: str | Path, min_output_len: int = 2) -> dict[str, int]:
    """Legacy: infer hour_key from directory name and use SQLite."""
    dir_name = Path(input_dir).name  # e.g. '2026-06-16' or '2026-06-16_17'
    if len(dir_name) == 10:  # YYYY-MM-DD
        # Scan all hours in this day
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT DISTINCT hour_key FROM entries WHERE hour_key LIKE ? ORDER BY hour_key",
                (dir_name + "%",),
            ).fetchall()
        finally:
            conn.close()
        # For simplicity, clean the first hour found
        if not rows:
            return {"files_processed": 0, "records_total": 0, "records_kept": 0, "skipped": {}, "records_deduped": 0}
        hour_key = rows[0]["hour_key"]
    else:
        hour_key = dir_name

    return clean_time_window(DB_PATH, hour_key, output_dir, min_output_len)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Clean proxy logs into a training dataset")
    parser.add_argument("--hour-key", help="Hour key (YYYY-MM-DD_HH) to clean")
    parser.add_argument("--min-output", type=int, default=2, help="Minimum output text length")
    args = parser.parse_args()

    if args.hour_key:
        output_dir = LOGS_ROOT / "cleaned" / args.hour_key
        clean_time_window(DB_PATH, args.hour_key, output_dir, args.min_output)
    else:
        # Clean all time windows
        conn = get_connection()
        try:
            windows = conn.execute("SELECT DISTINCT hour_key FROM entries ORDER BY hour_key").fetchall()
        finally:
            conn.close()
        for row in windows:
            hk = row["hour_key"]
            output_dir = LOGS_ROOT / "cleaned" / hk
            clean_time_window(DB_PATH, hk, output_dir, args.min_output)
