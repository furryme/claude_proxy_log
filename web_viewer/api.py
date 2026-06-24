#!/usr/bin/env python3
"""
web_viewer/api.py — Lightweight JSON API for browsing SQLite proxy logs in the browser.

Reads from logs/raw.db, decompresses gzip body + brotli/gzip response,
reconstructs SSE streams, and converts Anthropic format → OpenAI format.

Usage:
    python web_viewer/api.py              # default port 8002
    python web_viewer/api.py --port 9000
"""

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import brotli
except ImportError:
    brotli = None

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

sys.path.insert(0, str(PROJECT_DIR))
from log_store import get_connection, DB_PATH


# ── Decompression ──────────────────────────────────────────────
def decompress_body(raw_bytes: bytes) -> str:
    """Decompress gzip body BLOB."""
    try:
        return gzip.decompress(raw_bytes).decode("utf-8", errors="replace")
    except Exception:
        return raw_bytes.decode("utf-8", errors="replace")


def decompress_response(raw_bytes: bytes) -> str:
    """Decompress response BLOB (gzip / brotli / plain)."""
    if not raw_bytes:
        return ""
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


# ── SSE Reconstruction ─────────────────────────────────────────
def reconstruct_sse(text: str) -> dict:
    """Parse SSE stream text into structured response."""
    if not text or not any(m in text for m in ("content_block_delta", "message_start", "message_delta")):
        return None

    text_parts, thinking_parts = [], []
    usage = {}
    stop_reason = None
    tool_use_results = []

    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        for line in frame.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                continue
            try:
                obj = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            t = obj.get("type", "")
            if t == "content_block_start":
                cb = obj.get("content_block", {})
                cb_type = cb.get("type", "")
                if cb_type == "text" and cb.get("text"):
                    text_parts.append(cb["text"])
                elif cb_type == "thinking" and cb.get("thinking"):
                    thinking_parts.append(cb["thinking"])
                elif cb_type == "tool_use":
                    text_parts.append(f"\n\n{cb.get('name', 'tool')}[id:{cb.get('id', '?')}]\n{json.dumps(cb.get('input', {}), ensure_ascii=False)}\n[{cb_type}]\n\n")
            elif t == "content_block_delta":
                delta = obj.get("delta", {})
                d_type = delta.get("type", "")
                if d_type == "text_delta" and delta.get("text"):
                    text_parts.append(delta["text"])
                elif d_type == "thinking_delta" and delta.get("thinking"):
                    thinking_parts.append(delta["thinking"])
            elif t == "message_delta":
                if obj.get("usage"):
                    usage.update(obj["usage"])
                delta = obj.get("delta", {})
                stop_reason = delta.get("stop_reason", stop_reason)

    return {
        "is_streaming": True,
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts),
        "usage": usage,
        "stop_reason": stop_reason,
    }


def parse_non_streaming(text: str) -> dict:
    """Parse a non-streaming JSON response."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {"is_streaming": False, "text": text[:500], "thinking": ""}

    content = obj.get("content", [])
    texts = []
    thinkings = []
    for cb in content:
        if not isinstance(cb, dict):
            continue
        if cb.get("type") == "text":
            texts.append(cb.get("text", ""))
        elif cb.get("type") == "thinking":
            thinkings.append(cb.get("thinking", ""))
        elif cb.get("type") == "tool_use":
            texts.append(f"\n\n{cb.get('name', 'tool')}[id:{cb.get('id', '?')}]\n{json.dumps(cb.get('input', {}), ensure_ascii=False)}\n[tool_use]\n\n")

    return {
        "is_streaming": False,
        "text": "".join(texts),
        "thinking": "".join(thinkings),
        "usage": obj.get("usage", {}),
        "stop_reason": obj.get("stop_reason"),
    }


# ── Anthropic → OpenAI Conversion ──────────────────────────────
def convert_anthropic_to_openai(body_dict: dict) -> dict:
    """Convert stored Anthropic request to OpenAI-style messages."""
    messages = []
    system_parts = []

    # top-level system
    system = body_dict.get("system")
    if system:
        if isinstance(system, str):
            system_parts.append(system)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if not t.startswith("x-anthropic-billing-header"):
                        system_parts.append(t)

    # messages
    for msg in body_dict.get("messages", []):
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_parts.append(block.get("text", ""))
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")
        content_parts = []
        reasoning = []

        if isinstance(content, str):
            content_parts.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                if bt == "text":
                    content_parts.append({"type": "text", "text": block.get("text", "")})
                elif bt == "thinking":
                    reasoning.append(block.get("thinking", ""))
                elif bt == "redacted_thinking":
                    reasoning.append("[redacted thinking]")
                elif bt == "tool_use":
                    cb = block
                    content_parts.append({
                        "type": "tool_use",
                        "name": cb.get("name", ""),
                        "id": cb.get("id", ""),
                        "input": cb.get("input", {}),
                    })
                elif bt == "tool_result":
                    content_parts.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": block.get("content", ""),
                    })

        out_msg = {"role": role}
        if content_parts:
            # Only simplify to string when single text block — never simplify
            # tool_use/tool_result as they carry metadata (id, tool_use_id)
            if len(content_parts) == 1 and content_parts[0]["type"] == "text":
                out_msg["content"] = content_parts[0]["text"]
            else:
                out_msg["content"] = content_parts
        if reasoning:
            out_msg["reasoning"] = "".join(reasoning)

        if role == "user" and "content" not in out_msg:
            continue
        messages.append(out_msg)

    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

    # Convert tools from Anthropic to OpenAI format
    raw_tools = body_dict.get("tools", [])
    openai_tools = []
    for t in raw_tools:
        if "function" in t:
            # Already OpenAI format
            openai_tools.append(t)
        else:
            # Anthropic format: {name, description, input_schema}
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object"}),
                },
            })

    return {
        "model": body_dict.get("model", ""),
        "messages": messages,
        "max_tokens": body_dict.get("max_tokens"),
        "temperature": body_dict.get("temperature"),
        "tools": openai_tools if openai_tools else None,
    }


# ── HTTP API ────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence logs

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_index()
            return

        if path == "/static/main.css":
            self._serve_css()
            return

        if path == "/static/app.js":
            self._serve_js()
            return

        try:
            if path == "/api/summary":
                self._handle_summary()
            elif path == "/api/hours":
                self._handle_hours()
            elif path == "/api/requests":
                self._handle_requests(params)
            elif path == "/api/requests/":
                self._json_response({"error": "seq_id required"}, 400)
            elif path.startswith("/api/requests/"):
                seq_id = int(path.split("/")[-1])
                self._handle_request_detail(seq_id, params)
            elif path == "/api/date-requests":
                self._handle_date_requests(params)
            elif path == "/api/search":
                self._handle_search(params)
            else:
                self._json_response({"error": "not found"}, 404)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_index(self):
        index_path = SCRIPT_DIR / "index.html"
        if index_path.exists():
            self._html_response(index_path.read_text(encoding="utf-8"))
        else:
            self._html_response("<h1>web_viewer</h1><p>Put index.html in web_viewer/</p>")

    def _serve_static(self, path, content_type):
        file_path = SCRIPT_DIR / path
        if file_path.exists():
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_css(self):
        self._serve_static("static/main.css", "text/css")

    def _serve_js(self):
        self._serve_static("static/app.js", "application/javascript")

    def _handle_summary(self):
        conn = get_connection()
        try:
            total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            success = conn.execute("SELECT COUNT(*) FROM entries WHERE status = 200").fetchone()[0]
            errors = conn.execute("SELECT COUNT(*) FROM entries WHERE status != 200 AND status IS NOT NULL").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM entries WHERE status IS NULL").fetchone()[0]
            models = conn.execute("SELECT model, COUNT(*) as c FROM entries GROUP BY model ORDER BY c DESC").fetchall()
            hours = conn.execute(
                "SELECT hour_key, COUNT(*) as c FROM entries GROUP BY hour_key ORDER BY hour_key DESC LIMIT 30"
            ).fetchall()

            body_total = conn.execute("SELECT COALESCE(SUM(body_len),0) FROM entries").fetchone()[0]
            resp_total = conn.execute("SELECT COALESCE(SUM(length(response)),0) FROM entries").fetchone()[0]
        finally:
            conn.close()

        def fmt_size(n):
            if n < 1024: return f"{n} B"
            if n < 1024**2: return f"{n/1024:.1f} KB"
            if n < 1024**3: return f"{n/(1024**2):.1f} MB"
            return f"{n/(1024**3):.1f} GB"

        self._json_response({
            "total": total,
            "success": success,
            "errors": errors,
            "pending": pending,
            "body_total": body_total,
            "body_total_fmt": fmt_size(body_total),
            "resp_total": resp_total,
            "resp_total_fmt": fmt_size(resp_total),
            "models": [{"model": r[0], "count": r[1]} for r in models],
            "recent_hours": [{"hour_key": r[0], "count": r[1]} for r in hours],
        })

    def _handle_hours(self):
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT hour_key, COUNT(*) as cnt FROM entries GROUP BY hour_key ORDER BY hour_key DESC"
            ).fetchall()
        finally:
            conn.close()
        self._json_response({
            "hours": [
                {
                    "hour_key": r[0],
                    "date": r[0][:10],
                    "hour": r[0].split("_")[1] if "_" in r[0] else "",
                    "label": f"{r[0][:10]} {r[0].split('_')[1]}:00" if "_" in r[0] else r[0],
                    "count": r[1],
                }
                for r in rows
            ]
        })

    def _handle_requests(self, params):
        hour_key = params.get("hour_key", [None])[0]
        date = params.get("date", [None])[0]
        model = params.get("model", [None])[0]
        status = params.get("status", [None])[0]
        page = int(params.get("page", ["1"])[0])
        limit = min(int(params.get("limit", ["50"])[0]), 200)
        offset = (page - 1) * limit

        conditions = []
        binds = []
        time_cond, time_binds = _build_time_clause(hour_key, date)
        if time_cond:
            conditions.append(time_cond.lstrip("AND ").strip())
            binds.extend(time_binds)
        if model:
            conditions.append("model = ?")
            binds.append(model)
        if status:
            conditions.append("status = ?")
            binds.append(int(status))

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        conn = get_connection()
        try:
            count_sql = f"SELECT COUNT(*) FROM entries{where}"
            total = conn.execute(count_sql, binds).fetchone()[0]

            sql = f"""SELECT seq_id, ts, model, method, path, status, duration_ms,
                       body_len, COALESCE(length(response),0) as resp_size, body
                       FROM entries{where}
                       ORDER BY ts DESC LIMIT ? OFFSET ?"""
            rows = conn.execute(sql, binds + [limit, offset]).fetchall()
        finally:
            conn.close()

        items = []
        for r in rows:
            ts = datetime.fromtimestamp(r[1]).strftime("%H:%M:%S")
            hk = ""
            if date:
                hk = date
            elif hour_key:
                hk = hour_key.split("_")[0]

            # Quick message count from body
            msg_count = 0
            try:
                body_blob = r[9]  # body BLOB
                if body_blob:
                    body_str = decompress_body(body_blob)
                    if body_str:
                        bd = json.loads(body_str)
                        msgs = bd.get("messages", [])
                        msg_count = len([m for m in msgs if m.get("role") != "system"])
            except Exception:
                pass

            items.append({
                "seq_id": r[0],
                "time": ts,
                "date": hk,
                "model": r[2],
                "method": r[3],
                "path": r[4],
                "status": r[5],
                "duration_ms": r[6],
                "body_len": r[7],
                "resp_size": r[8],
                "msg_count": msg_count,
            })

        self._json_response({
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit,
            "items": items,
        })

    def _handle_request_detail(self, seq_id, params):
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM entries WHERE seq_id = ?", (seq_id,)).fetchone()
        finally:
            conn.close()

        if not row:
            self._json_response({"error": "not found"}, 404)
            return

        row = dict(row)
        body_str = decompress_body(row.get("body") or b"")
        resp_str = decompress_response(row.get("response") or b"")

        # Parse body
        try:
            body_dict = json.loads(body_str) if body_str else {}
        except json.JSONDecodeError:
            body_dict = {"_raw": body_str[:2000]}

        # Parse response
        is_post = row.get("method") == "POST" and "/v1/messages" in (row.get("path") or "")
        if is_post and resp_str:
            parsed_resp = reconstruct_sse(resp_str)
            if parsed_resp is None:
                parsed_resp = parse_non_streaming(resp_str)
        else:
            parsed_resp = {"is_streaming": False, "text": resp_str[:1000], "thinking": ""} if resp_str else {"text": "", "thinking": ""}

        # Convert to OpenAI format
        openai_req = convert_anthropic_to_openai(body_dict)

        # Get system prompt separately
        system_text = ""
        if openai_req["messages"] and openai_req["messages"][0]["role"] == "system":
            system_text = openai_req["messages"].pop(0)["content"]

        # Trim for summary view (full content available on flag)
        full = params.get("full", ["0"])[0] == "1"
        if not full:
            openai_req = _trim_request(openai_req)
            parsed_resp["text"] = parsed_resp.get("text", "")[:3000]
            parsed_resp["thinking"] = parsed_resp.get("thinking", "")[:2000]
            if system_text:
                system_text = system_text[:2000]

        result = {
            "seq_id": row["seq_id"],
            "ts": datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "hour_key": row.get("hour_key", ""),
            "method": row["method"],
            "path": row["path"],
            "model": row.get("model", ""),
            "status": row.get("status"),
            "duration_ms": row.get("duration_ms"),
            "body_len": row.get("body_len", 0),
            "resp_size": len(row.get("response") or b""),
            "error": row.get("error"),
            "request": openai_req,
            "system_prompt": system_text,
            "response": parsed_resp,
        }

        # Always include full raw response text when available
        if resp_str:
            result["response"]["raw_text"] = resp_str

        self._json_response(result)

    def _handle_date_requests(self, params):
        """List unique dates."""
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT DISTINCT substr(hour_key, 1, 10) as date FROM entries ORDER BY date DESC"
            ).fetchall()
        finally:
            conn.close()
        self._json_response({
            "dates": [{"date": r[0]} for r in rows]
        })

    def _handle_search(self, params):
        q = params.get("q", [""])[0].strip()
        if not q:
            self._json_response({"items": [], "total": 0})
            return

        scope = params.get("scope", ["all"])[0]  # all | body | response
        hour_key = params.get("hour_key", [None])[0]
        date = params.get("date", [None])[0]
        page = int(params.get("page", ["1"])[0])
        limit = min(int(params.get("limit", ["30"])[0]), 100)
        offset = (page - 1) * limit

        time_cond, time_binds = _build_time_clause(hour_key, date)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT seq_id, ts, model, status, duration_ms, body_len, COALESCE(length(response),0) as resp_size, body, response "
                "FROM entries WHERE status = 200 AND method = 'POST' AND path LIKE '%/v1/messages%' "
                f"{time_cond} ORDER BY ts DESC",
                time_binds,
            ).fetchall()

            q_lower = q.lower()
            matches = []

            for r in rows:
                seq_id, ts = r[0], r[1]
                dt = datetime.fromtimestamp(ts)
                item = {
                    "seq_id": seq_id,
                    "date": dt.strftime("%Y-%m-%d"),
                    "time": dt.strftime("%H:%M:%S"),
                    "model": r[2], "status": r[3], "duration_ms": r[4],
                    "body_len": r[5], "resp_size": r[6],
                }

                if scope in ("all", "body"):
                    body_str = decompress_body(r[7] or b"")
                    body_lower = body_str.lower()
                    if q_lower in body_lower:
                        idx = body_lower.index(q_lower)
                        snippet = body_str[max(0, idx - 80):idx + len(q) + 80]
                        item["scope"] = "body"
                        item["snippet"] = self._esc(snippet)
                        matches.append(item)
                        continue

                if scope in ("all", "response"):
                    resp_raw = decompress_response(r[8] or b"")
                    # Parse SSE to get the reconstructed text — raw SSE fragments
                    # won't match multi-word queries split across frames
                    parsed = reconstruct_sse(resp_raw)
                    if parsed is None:
                        parsed = parse_non_streaming(resp_raw)
                    if parsed:
                        full_text = (parsed.get("text") or "") + " " + (parsed.get("thinking") or "")
                        if q_lower in full_text.lower():
                            idx = full_text.lower().index(q_lower)
                            snippet = full_text[max(0, idx - 80):idx + len(q) + 80]
                            item["scope"] = "response"
                            item["snippet"] = self._esc(snippet)
                            matches.append(item)

            total = len(matches)
            paged = matches[offset:offset + limit]
        finally:
            conn.close()

        self._json_response({"items": paged, "total": total})

    @staticmethod
    def _esc(s: str) -> str:
        """Minimal escape for snippet — flatten control chars, cap length."""
        return s.replace("\t", " ").replace("\r", " ").replace("\n", " ")[:300]


def _build_time_clause(hour_key, date):
    """Return (SQL clause, bind_args) for time filtering."""
    if hour_key:
        return "AND hour_key = ?", (hour_key,)
    elif date:
        return "AND hour_key LIKE ?", (f"{date}_%",)
    else:
        # Default to last 24 hours
        cutoff = time.time() - 86400
        return "AND ts >= ?", (cutoff,)


def _trim_request(openai_req: dict) -> dict:
    """Trim request for summary display."""
    trimmed = dict(openai_req)
    msgs = []
    for msg in openai_req.get("messages", []):
        tm = dict(msg)
        content = tm.get("content")
        if isinstance(content, str) and len(content) > 600:
            tm["content"] = content[:600] + f"...\n\n[{len(content)} chars total]"
        elif isinstance(content, list):
            tc = []
            for block in content:
                tb = dict(block)
                if tb.get("type") == "text" and len(tb.get("text", "")) > 300:
                    tb["text"] = tb["text"][:300] + f"...\n[{len(tb['text'])} chars total]"
                tc.append(tb)
            tm["content"] = tc
        if msg.get("reasoning") and len(msg["reasoning"]) > 500:
            tm["reasoning"] = msg["reasoning"][:500] + f"...\n[{len(msg['reasoning'])} chars total]"
        msgs.append(tm)
    trimmed["messages"] = msgs

    if openai_req.get("tools"):
        trimmed["tools"] = [
            {"type": "function", "function": {"name": t["function"]["name"], "description": t["function"].get("description", "")[:100]}}
            for t in openai_req["tools"]
        ]

    return trimmed


# ── Main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Chat Log Web Viewer API")
    parser.add_argument("--port", "-p", type=int, default=8002)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"Web Viewer API: http://{args.host}:{args.port}/")
    print(f"Database: {DB_PATH}")

    server = HTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
