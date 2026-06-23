#!/usr/bin/env python3
"""
Claude API 代理服务器

根据请求体中的 model 字段将请求转发到不同的上游服务。
启动后监听 127.0.0.1:8001（端口可通过 PROXY_PORT 环境变量修改）。

用法:
    python claude_api_proxy.py
    PROXY_PORT=9000 python claude_api_proxy.py

路由规则:
    - 请求体 model 字段匹配 ROUTES 字典时，转发到对应的上游地址
    - 未匹配的 model 使用 DEFAULT_UPSTREAM（默认 localhost:8000）

配置 Claude Code 使用此代理:
    方式一 — 环境变量:
        export ANTHROPIC_BASE_URL=http://127.0.0.1:8001

    方式二 — 写入 ~/.bashrc 或项目 .env 文件:
        echo 'export ANTHROPIC_BASE_URL=http://127.0.0.1:8001' >> ~/.bashrc

抓包日志:
    每条请求/响应会自动写入 logs/raw/YYYY-MM-DD/HH/*.jsonl
    响应体以 base64 存储（可能是 gzip 压缩），归档时解压。
    可通过 LOGS_ROOT 环境变量修改日志根目录。

归档:
    python dataset_archiver.py              # 归档最旧的时间窗口
    python dataset_archiver.py --keep-source # 归档但保留原始日志
    python archive_cron.py --install         # 安装定时归档 (每4小时)
"""

import gzip
import io
import json
import os
import shutil
import sys
import threading
import time
import http.server
import urllib.request
import urllib.error

UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", 1200))
RESPONSE_CHUNK_SIZE = int(os.environ.get("RESPONSE_CHUNK_SIZE", 65536))

LISTEN_PORT = int(os.environ.get("PROXY_PORT", 8001))

# Cookie 缓存：file_path -> "key1=val1; key2=val2"
_COOKIE_CACHE: dict[str, str] = {}

# 日志开关：默认开启，可通过环境变量关闭
_ENABLE_LOGGING = os.environ.get("DISABLE_LOGGING", "").lower() not in ("1", "true", "yes")

# Lazy-import logger so the proxy still works if request_logger has issues
if _ENABLE_LOGGING:
    try:
        from request_logger import RequestLogger
    except ImportError:
        print("  [warning] request_logger not available, logging disabled", file=sys.stderr)
        _ENABLE_LOGGING = False


def _load_routes_config() -> tuple[dict, str]:
    """从 JSON 配置文件加载路由。

    文件格式 (config/routes.json):
    {
        "Qwen3.6-27B": { "url": "...", "cookie_file": "..." },
        "OtherModel": "http://simple-url",
        "__default_upstream__": "https://fallback-url"
    }

    返回 (ROUTES, DEFAULT_UPSTREAM)
    """
    config_path = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "routes.json"))

    if not os.path.isfile(config_path):
        print(f"  [error] routes config not found: {config_path}", file=sys.stderr)
        print(f"  [error] Create config/routes.json or set CONFIG_PATH env var", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    default_upstream = data.pop("__default_upstream__", "http://localhost:8000")
    routes: dict[str, dict | str] = data

    # 验证格式
    for model, entry in routes.items():
        if isinstance(entry, str):
            # 简单字符串 = 纯 URL
            continue
        if isinstance(entry, dict):
            if "url" not in entry:
                print(f"  [error] route {model!r} missing 'url' key", file=sys.stderr)
                sys.exit(1)
            continue
        print(f"  [error] route {model!r} must be string or object with 'url'", file=sys.stderr)
        sys.exit(1)

    return routes, default_upstream


ROUTES, DEFAULT_UPSTREAM = _load_routes_config()


def _resolve_route(model: str) -> tuple[str, str | None]:
    """返回 (upstream_url, cookie_header_or_None)"""
    entry = ROUTES.get(model)
    if isinstance(entry, dict):
        url = entry["url"]
        cookie_file = entry.get("cookie_file")
    else:
        url = entry
        cookie_file = None

    cookie_header = None
    if cookie_file and os.path.isfile(cookie_file):
        if cookie_file not in _COOKIE_CACHE:
            cookies: list[str] = []
            with open(cookie_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ";" in line:
                        for part in line.split(";"):
                            part = part.strip()
                            if "=" in part:
                                cookies.append(part)
                        continue
                    parts = line.split()
                    if len(parts) >= 7:
                        name, value = parts[5], parts[6]
                        cookies.append(f"{name}={value}")
                    elif "=" in parts[0]:
                        cookies.append(parts[0])
            _COOKIE_CACHE[cookie_file] = "; ".join(cookies) if cookies else ""
        cookie_header = _COOKIE_CACHE[cookie_file]

    return url, cookie_header


def forward(request: http.server.BaseHTTPRequestHandler) -> None:
    path = request.path
    method = request.command
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "connection", "transfer-encoding")
    }

    # Read body
    content_length = int(request.headers.get("Content-Length", 0))
    body = request.rfile.read(content_length) if content_length > 0 else b""

    # Handle gzip encoding (client may send compressed body)
    if body and (request.headers.get("Content-Encoding") or "").lower() == "gzip":
        body = gzip.decompress(body)
        headers["Content-Length"] = str(len(body))

    # Determine model for routing
    model = None
    if body:
        try:
            data = json.loads(body)
            model = data.get("model", "")
        except json.JSONDecodeError:
            print(f"  body not JSON, {len(body)} bytes", file=sys.stderr)

    upstream, cookie_header = _resolve_route(model) if model else (DEFAULT_UPSTREAM, None)
    if not upstream:
        upstream = DEFAULT_UPSTREAM
    target = upstream.rstrip("/") + path

    # Inject cookie header if configured
    if cookie_header:
        headers["Cookie"] = cookie_header

    cookie_info = ""
    if cookie_header:
        cookie_keys = [c.split("=")[0] for c in cookie_header.split("; ")]
        cookie_info = f" (cookie={len(cookie_keys)} keys)"
    print(f"  model={model!r}  ->  {method} {path}  ->  {target}{cookie_info}", file=sys.stderr)

    # ── Start logging ──────────────────────────────────────
    logger = None
    if _ENABLE_LOGGING:
        try:
            logger = RequestLogger(
                method=method,
                path=path,
                body_bytes=body,
                upstream_url=target,
                model=model or "",
            )
        except Exception as e:
            print(f"  [warning] RequestLogger init failed: {e}", file=sys.stderr)
            logger = None

    def _finish_log(status: int | None = None, error: str = ""):
        if logger:
            try:
                logger.finish(status=status, error=error)
            except Exception:
                pass

    # Forward to upstream
    req = urllib.request.Request(target, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
        print(f"  <- {resp.status}", file=sys.stderr)

        request.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in ("transfer-encoding", "connection"):
                request.send_header(k, v)
        request.end_headers()

        # Stream response back to client
        try:
            if logger:
                # Logging enabled: accumulate into single BytesIO + write
                buf = io.BytesIO()
                while True:
                    chunk = resp.read(RESPONSE_CHUNK_SIZE)
                    if not chunk:
                        break
                    buf.write(chunk)
                    request.wfile.write(chunk)
                buf.seek(0)
                raw = buf.read()
                if raw:
                    logger.add_response_chunk(raw)
            else:
                # No logging: zero-Python-loop, C-level copy
                shutil.copyfileobj(resp, request.wfile, length=RESPONSE_CHUNK_SIZE)
        except BrokenPipeError:
            # Client disconnected — close upstream immediately so
            # vllm-server stops generating (no resource leak).
            resp.close()
            _finish_log(status=resp.status, error="broken_pipe")

        _finish_log(status=resp.status)

    except urllib.error.HTTPError as e:
        err_body = e.read()
        e.close()  # close upstream connection to avoid leaving it open
        print(f"  <- {e.code} ERROR: {err_body.decode('utf-8', errors='replace')[:200]}", file=sys.stderr)
        if logger:
            try:
                logger.add_response_chunk(err_body, tag=f"http_{e.code}")
            except Exception:
                pass
        _finish_log(status=e.code)

        request.send_response(e.code)
        for k, v in e.headers.items():
            request.send_header(k, v)
        request.end_headers()
        try:
            request.wfile.write(err_body)
        except BrokenPipeError:
            pass

    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, TimeoutError):
            print(f"  upstream timeout ({UPSTREAM_TIMEOUT}s): {reason}", file=sys.stderr)
            _finish_log(error=f"timeout:{reason}")
            request.send_response(504)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            request.wfile.write(b"Upstream timeout")
        else:
            print(f"  URL error: {e}", file=sys.stderr)
            _finish_log(error=f"url_error:{reason}")
            request.send_response(502)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            request.wfile.write(str(e).encode())


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        forward(self)

    def do_POST(self):
        forward(self)

    def do_OPTIONS(self):
        forward(self)

    def do_DELETE(self):
        forward(self)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}", file=sys.stderr)


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    print(f"Proxy listening on 127.0.0.1:{LISTEN_PORT}")
    print(f"Routes:")
    for model, upstream in ROUTES.items():
        print(f"  {model} -> {upstream}")
    print(f"  * (default) -> {DEFAULT_UPSTREAM}")
    if _ENABLE_LOGGING:
        print(f"Logging enabled -> {os.environ.get('LOGS_ROOT', 'logs')}/raw/")
    else:
        print(f"Logging disabled")
    server.serve_forever()


if __name__ == "__main__":
    main()
