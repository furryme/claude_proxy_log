#!/usr/bin/env python3
"""
log_viewer.py — View and inspect captured proxy logs from SQLite.

Auto-detects gzip/brotli compression, reconstructs SSE responses.

Usage:
    python log_viewer.py                   # list all entries
    python log_viewer.py --latest          # view latest entry
    python log_viewer.py --seq-id 42       # view specific entry
    python log_viewer.py --summary         # summary only
    python log_viewer.py --raw             # raw decompressed response
    python log_viewer.py --response-only   # response only, skip request
    python log_viewer.py --date 2026-06-16 --hour 17  # filter
    python log_viewer.py --file cleaned/2026-06-16_17/dataset.jsonl --index 0  # view cleaned data
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import brotli
except ImportError:
    brotli = None

from log_store import get_connection, DB_PATH


# ── decompression ────────────────────────────────────────────
def _decompress(raw_bytes: bytes) -> str:
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


def _format_size(n: int) -> str:
    if n < 1024: return f"{n} B"
    if n < 1024**2: return f"{n/1024:.1f} KB"
    return f"{n/(1024**2):.1f} MB"


def _color(code: str, text: str) -> str:
    if not sys.stdout.isatty(): return text
    return f"\033[{code}m{text}\033[0m"


# ── response reconstruction ──────────────────────────────────
def _parse_sse_data(frame: str) -> dict | None:
    for line in frame.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            try: return json.loads(line[5:].strip())
            except (json.JSONDecodeError, TypeError): return None
    return None


def _reconstruct_response(response_bytes: bytes) -> dict:
    if not response_bytes:
        return {"text": "", "thinking": "", "is_streaming": False, "raw_size": 0}

    text = _decompress(response_bytes)
    result = {"raw_size": len(response_bytes), "decompressed_size": len(text.encode("utf-8"))}

    is_streaming = any(m in text for m in ("content_block_delta", "message_start", "message_delta"))

    if is_streaming:
        result["is_streaming"] = True
        text_parts, thinking_parts = [], []
        usage, stop_reason = {}, None
        for frame in text.split("\n\n"):
            obj = _parse_sse_data(frame.strip())
            if not obj: continue
            t = obj.get("type", "")
            if t == "content_block_start":
                cb = obj.get("content_block", {})
                if cb.get("type") == "text" and cb.get("text"): text_parts.append(cb["text"])
                if cb.get("type") == "thinking" and cb.get("thinking"): thinking_parts.append(cb["thinking"])
            elif t == "content_block_delta":
                delta = obj.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"): text_parts.append(delta["text"])
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"): thinking_parts.append(delta["thinking"])
            elif t == "message_delta":
                if obj.get("usage"): usage.update(obj["usage"])
                delta = obj.get("delta", {})
                stop_reason = delta.get("stop_reason", stop_reason)
        result["text"] = "".join(text_parts)
        result["thinking"] = "".join(thinking_parts)
        result["usage"] = usage
        result["stop_reason"] = stop_reason
    else:
        result["is_streaming"] = False
        try:
            obj = json.loads(text)
            content = obj.get("content", [])
            texts = [cb.get("text", "") for cb in content if isinstance(cb, dict) and cb.get("type") == "text"]
            if texts:
                result["text"] = "".join(texts)
                result["thinking"] = ""
                result["usage"] = obj.get("usage", {})
                result["stop_reason"] = obj.get("stop_reason")
                return result
        except json.JSONDecodeError: pass
        result["text"] = text[:500]

    return result


# ── database queries ─────────────────────────────────────────
def list_requests(db_path: str, date_str: str = None, hour_str: str = None) -> list[dict]:
    conn = get_connection()
    try:
        if date_str and hour_str:
            hk = f"{date_str}_{hour_str}"
            rows = conn.execute(
                "SELECT seq_id, ts, model, method, path, status, duration_ms, body_len, COALESCE(length(response),0) as resp_size "
                "FROM entries WHERE hour_key = ? ORDER BY ts", (hk,)
            ).fetchall()
        elif date_str:
            prefix = f"{date_str}_"
            rows = conn.execute(
                "SELECT seq_id, ts, model, method, path, status, duration_ms, body_len, COALESCE(length(response),0) as resp_size "
                "FROM entries WHERE hour_key LIKE ? ORDER BY ts", (prefix + "%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT seq_id, ts, model, method, path, status, duration_ms, body_len, COALESCE(length(response),0) as resp_size "
                "FROM entries ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_request(db_path: str, seq_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM entries WHERE seq_id = ?", (seq_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── display ──────────────────────────────────────────────────
def view_request(seq_id: int, summary_only: bool = False, raw_only: bool = False, response_only: bool = False) -> None:
    entry = get_request(str(DB_PATH), seq_id)
    if not entry:
        print(f"  No entry found for seq_id={seq_id}")
        return

    ts = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S")
    resp_size = len(entry.get("response") or b"")
    print(f"\n{_color('1;33', '='*70)}", file=sys.stderr)
    body_sz = _format_size(entry.get("body_len", 0))
    resp_sz = _format_size(resp_size)
    print(_color('1;33', f'  seq_id={seq_id}  {ts}  body={body_sz}  response={resp_sz}'))
    print(f"{_color('1;33', '='*70)}\n", file=sys.stderr)

    # ── Request ──
    if not response_only:
        model = entry.get("model", "")
        method = entry.get("method", "")
        path = entry.get("path", "")
        body_len = entry.get("body_len", 0)

        print(_color('1;32', "  REQUEST"))
        print(f"  {'─'*66}")
        print(f"  seq_id: {seq_id}  |  {method} {path}")
        if model: print(f"  model: {_color('1;35', model)}")
        print(f"  body size: {_format_size(body_len)}")

        if not summary_only:
            try:
                body_str = gzip.decompress(entry["body"]).decode("utf-8") if entry.get("body") else ""
                body = json.loads(body_str) if body_str else {}
                msgs = body.get("messages", [])
                if msgs:
                    print(f"  messages: {len(msgs)} turns")
                    for i, m in enumerate(msgs):
                        role = m.get("role", "?")
                        content = m.get("content", "")
                        if isinstance(content, str):
                            print(f"    [{role}] {content[:120]}{'...' if len(content) > 120 else ''}")
                        elif isinstance(content, list):
                            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
                            combined = " ".join(texts)[:120]
                            print(f"    [{role}] {combined}{'...' if len(' '.join(texts)) > 120 else ''}")
            except Exception: pass

    if summary_only:
        status = entry.get("status", "?")
        dur = entry.get("duration_ms", 0)
        color = '1;32' if status == 200 else '1;31'
        print(f"  status: {_color(color, status)}  |  duration: {_color('1;33', f'{dur:.0f}ms')}")
        return

    # ── Response ──
    print()
    print(_color('1;32', "  RESPONSE"))
    print(f"  {'─'*66}")

    response_bytes = entry.get("response") or b""
    if not response_bytes:
        print("  (no response data)")
    else:
        if raw_only:
            print(_decompress(response_bytes))
            return

        response = _reconstruct_response(response_bytes)

        status = entry.get("status", "?")
        dur = entry.get("duration_ms", 0)
        color = '1;32' if status == 200 else '1;31'
        print(f"  status: {_color(color, status)}  |  duration: {_color('1;33', f'{dur:.0f}ms')}")
        print(f"  raw size: {_format_size(response.get('raw_size',0))}  |  decompressed: {_format_size(response.get('decompressed_size',0))}")
        print(f"  streaming: {response.get('is_streaming', False)}")

        usage = response.get("usage", {})
        if usage:
            print(f"  tokens: input={usage.get('input_tokens','?')}  output={usage.get('output_tokens','?')}")
        if response.get("stop_reason"):
            print(f"  stop_reason: {response['stop_reason']}")

        text = response.get("text", "")
        if text:
            print(f"\n  {_color('1;37', '  text (len=' + str(len(text)) + ')')}")
            print(f"  {'─'*66}")
            for line in text.split("\n"):
                print(f"  {line}")

        thinking = response.get("thinking", "")
        if thinking:
            print(f"\n  {_color('1;33', '  thinking (len=' + str(len(thinking)) + ')')}")
            print(f"  {'─'*66}")
            for line in thinking.split("\n"):
                print(f"  {line}")

    error = entry.get("error")
    if error:
        print(f"\n  error: {_color('1;31', error)}")


def view_cleaned(filepath: Path, index: int = None) -> None:
    print(f"\n{_color('1;33', '='*70)}", file=sys.stderr)
    print(f"{_color('1;33', f'  {filepath}')} (cleaned dataset)", file=sys.stderr)
    print(f"{_color('1;33', '='*70)}\n", file=sys.stderr)

    with open(filepath) as f:
        lines = f.readlines()

    total = len(lines)
    print(f"Total records: {total}", file=sys.stderr)

    if index is not None:
        lines = [lines[index]]
        print(f"Showing record {index}/{total}", file=sys.stderr)

    for i, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        try: rec = json.loads(line)
        except json.JSONDecodeError: continue

        print(f"\n{_color('1;32', f'--- Record {i} ---')}")
        req = rec.get("request", {})
        print(f"  model: {req.get('model','?')}  |  messages: {len(req.get('messages',[]))} turns")
        resp = rec.get("response", {})
        print(f"  text len: {len(resp.get('text',''))}  |  thinking len: {len(resp.get('thinking',''))}")
        print(f"  streaming: {resp.get('is_streaming')}  |  stop: {resp.get('stop_reason')}")
        usage = resp.get("usage", {})
        if usage: print(f"  tokens: in={usage.get('input_tokens','?')}  out={usage.get('output_tokens','?')}")
        meta = rec.get("metadata", {})
        if meta.get("duration_ms"): print(f"  duration: {meta['duration_ms']:.0f}ms")
        if text := resp.get("text", ""):
            if len(lines) <= 10:
                print(f"\n  text: {text[:500]}{'...' if len(text) > 500 else ''}")


# ── main ─────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="View captured proxy logs")
    parser.add_argument("--file", "-f", type=str, help="Cleaned dataset file to view")
    parser.add_argument("--latest", "-l", action="store_true", help="View latest entry")
    parser.add_argument("--seq-id", "-s", type=int, help="View specific seq_id")
    parser.add_argument("--date", type=str, help="Filter by date (YYYY-MM-DD)")
    parser.add_argument("--hour", type=str, help="Filter by hour (00-23)")
    parser.add_argument("--summary", "-m", action="store_true", help="Summary only")
    parser.add_argument("--raw", action="store_true", help="Raw decompressed response")
    parser.add_argument("--response-only", action="store_true", help="Response only")
    parser.add_argument("--index", "-i", type=int, help="Record index for cleaned files")
    parser.add_argument("--count", action="store_true", help="Show entry count and size per hour_key time window")
    args = parser.parse_args()

    if args.file:
        fp = Path(args.file)
        if not fp.exists():
            print(f"File not found: {fp}", file=sys.stderr)
            sys.exit(1)
        view_cleaned(fp, args.index)
        return

    # ── --count: stats per hour_key ──────────────────────────
    if args.count:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT hour_key, COUNT(*) as cnt, SUM(body_len) as body_total, SUM(COALESCE(length(response),0)) as resp_total "
                "FROM entries GROUP BY hour_key ORDER BY hour_key DESC"
            ).fetchall()
        finally:
            conn.close()
        rows = [dict(r) for r in rows]
        if not rows:
            print("No entries found.")
            sys.exit(1)
        total_entries = sum(r["cnt"] for r in rows)
        total_body = sum(r["body_total"] or 0 for r in rows)
        total_resp = sum(r["resp_total"] or 0 for r in rows)
        print(f"\n{'Time window':<18}  {'Entries':>7}  {'Body size':>10}  {'Resp size':>10}")
        print(f"{'-'*18}  {'-'*7}  {'-'*10}  {'-'*10}")
        for r in rows:
            hk = r["hour_key"]
            cnt = r["cnt"]
            bt = _format_size(r["body_total"] or 0)
            rt = _format_size(r["resp_total"] or 0)
            print(f"  {hk}  {cnt:>5}   {bt:>10}  {rt:>10}")
        sep = f"{'-'*52}"
        print(sep)
        print(f"{'Total':<18}  {total_entries:>5}   {_format_size(total_body):>10}  {_format_size(total_resp):>10}\n")
        return

    requests = list_requests(str(DB_PATH), args.date, args.hour)
    if not requests:
        print("No entries found.", file=sys.stderr)
        sys.exit(1)

    if args.latest:
        view_request(requests[-1]["seq_id"], args.summary, args.raw, args.response_only)
    elif args.seq_id:
        view_request(args.seq_id, args.summary, args.raw, args.response_only)
    else:
        print(f"Found {len(requests)} entr(y/ies):\n")
        for r in requests:
            ts = datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
            status = r.get("status", "?")
            dur = r.get("duration_ms", 0)
            color = '1;32' if status == 200 else '1;31'
            print(f"  {_color('1;36', ts)}  {_color(color, str(status)):>4}  {dur:>7.0f}ms  "
                  f"body={_format_size(r.get('body_len',0)):>8}  resp={_format_size(r.get('resp_size',0)):>8}  "
                  f"seq={r['seq_id']}  {r['model']}")
        print(f"\n  Use --latest, --seq-id N, or --summary.")


if __name__ == "__main__":
    main()
