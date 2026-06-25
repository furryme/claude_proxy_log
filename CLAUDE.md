# CLAUDE.md — 项目注意事项（AI 开发指南）

## ⚠️ 绝对禁止

- **绝对不要杀掉或干扰端口 8001 上的原始代理**——那是正在运行的生产服务
- 所有测试必须在非 8001 端口上进行（推荐 8002 或更高）
- 不要修改 `ROUTES` 字典中的生产路由配置

## 项目概览

Claude API 反向代理，新增抓包 + 数据归档功能，用于构建训练数据集。数据存储于 SQLite（`logs/raw.db`），WAL mode 支持并发读写。

## 文件职责与 API 接口

| 文件 | 职责 | 关键 API |
|------|------|----------|
| `claude_api_proxy.py` | 代理主程序，`ThreadingHTTPServer`，透明转发 | `forward(request)` — 创建 `RequestLogger`，转发请求/响应，`_patch_sse_lines()` 修复非 Anthropic SSE 格式，调用 `logger.finish()` |
| `request_logger.py` | 请求/响应缓冲，`finish()` 非阻塞投递 | `RequestLogger.__init__(method, path, body_bytes, upstream_url, model)` / `add_response_chunk(raw_bytes)` / `finish(status, error)` |
| `log_store.py` | SQLite 存储，后台 writer 线程 | `enqueue_entry(entry_dict)` / `get_connection()` / `DB_PATH` / `_next_seq_id()` / `_make_hour_key(ts)` |
| `dataset_cleaner.py` | 从 SQLite 读数据 → 解压 body + response → SSE 重组 → 去重 → 输出 dataset.jsonl | `clean_time_window(db_path, hour_key, output_dir)` / `clean_directory(input_dir, output_dir)`（兼容旧调用） |
| `dataset_archiver.py` | 按 `hour_key` 查询 SQLite → 清洗 → 压缩 tar.gz → `DELETE FROM entries` | `archive_window(date_str, hour_str, before, dry_run, keep_source, ...)` |
| `archive_cron.py` | cron 安装/管理，仅调用 archiver | 不直接交互数据库 |
| `log_viewer.py` | 交互式查看 SQLite 日志 | `list_requests(db_path, date, hour)` / `view_request(seq_id, ...)` / `get_request(db_path, seq_id)` |
| `web_viewer/api.py` | 浏览器端日志查看器后端 API | `GET /api/summary`, `GET /api/hours`, `GET /api/requests`, `GET /api/requests/:seq_id` |
| `web_viewer/index.html` | 浏览器端日志查看器前端（SPA） | Dashboard / 请求列表 / 对话视图（含 Markdown 渲染、Thinking 折叠、Tool 面板） |
| `web_viewer/static/app.js` | 前端逻辑 | 视图切换、API 调用、Markdown 渲染 |
| `web_viewer/static/main.css` | 前端样式 | 深色/浅色主题（可切换），响应式布局 |

## 代理透明原则

**代理是透明的管道，默认绝不修改请求或响应：**
- 不修改或剥离客户端请求的任何 Header（包括 `Accept-Encoding`）
- 不修改请求 body
- 不修改响应内容、不修改响应 Header
- 客户端主动断开（`BrokenPipeError`）= 正常行为，不记录错误
- 唯一职责：原样转发 + 记录日志

**唯一例外 — `patch_sse: true` 路由：** 当上游 SSE 流不符合 Anthropic 格式时，`_patch_sse_lines()` 会在响应流中做两处修复：
1. **注入 `event:` 行** — `data:` 前面没有 `event:` 时，从 `data` 的 `type` 字段提取并补上
2. **注入 `input_tokens`** — `message_delta` 的 `usage` 缺少 `input_tokens` 时补上 `"input_tokens": 1`
日志记录的是**原始未修改**的响应字节。

## 存储架构

```
代理线程 (RequestLogger)
  └── queue.put_nowait() ← 1ms，满则丢弃
        │
后台 writer 线程 (log_store.py)
  ├── body: gzip.compress(body_bytes, 6) → BLOB (压缩比 ~35%)
  ├── response: raw bytes → BLOB (brotli/gzip/plaintext 原样)
  └── batch INSERT (2s 或 50 条)
        │
logs/raw.db (SQLite, WAL mode)
```

### 数据库表结构

```sql
CREATE TABLE entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    seq_id      INTEGER UNIQUE NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    model       TEXT NOT NULL,
    upstream    TEXT,
    body        BLOB,          -- gzip(body_json_utf8_bytes)
    body_len    INTEGER,
    response    BLOB,          -- 原始字节 (brotli/gzip/plaintext)
    status      INTEGER,
    duration_ms REAL,
    error       TEXT,
    hour_key    TEXT NOT NULL  -- '2026-06-17_11'
);
CREATE INDEX idx_hour ON entries(hour_key);
CREATE INDEX idx_ts ON entries(ts);
```

**连接方式**：`from log_store import get_connection, DB_PATH`，`get_connection()` 返回带 `Row` factory 的只读连接。

### 响应压缩检测

上游可能返回三种格式，清洗器和 log_viewer 均用 `_decompress()` 自动检测：
1. gzip — 首字节 `1f 8b` → `gzip.decompress()`
2. brotli — `brotli.decompress()` 直接尝试（不依赖首字节，brotli 库对 `5b` 开头也兼容）
3. 纯文本 — 以上都失败 → `utf-8 decode`

## 关键约定

1. **日志绝不能影响代理** — `try/except` 包裹，`queue.put_nowait()` 零阻塞，queue 满则丢弃
2. **`DISABLE_LOGGING=1`** 完全关闭抓包，不加载 `request_logger`
3. **`PROXY_PORT`** 环境变量切换监听端口
4. **`RESPONSE_CHUNK_SIZE`** 环境变量，默认 65536（64KB），控制 `resp.read()` 块大小
5. **body 压缩**：gzip level 6，纯文本 JSON 压缩比 35%（实测 113KB → 40KB）
6. **response 不二次压缩**：上游已压缩（brotli 30x），再压缩增大 1%
7. **清洗规则**：保留 `POST /v1/messages` + `status=200` + 有意义的 text 或 thinking
8. **归档文件名**：`dataset_YYYY-MM-DD_HH.tar.gz`，含 `dataset.jsonl`
9. **VACUUM 策略**：仅删除 >1000 行时执行，避免频繁 VACUUM
10. **测试代理常驻 8002 端口**（注意：`web_viewer` 默认也是 8002，启动时指定不同端口避免冲突）

## 历史 Bug 记录

详见 [[docs/bugs.md]] — 包含连接泄漏和 CPU 空转的根因分析。关键教训：
- 客户端断开时**必须关闭上游连接**（`resp.close()`），否则 vllm-server 持续生成
- 后台线程**禁止无超时空转**，用 `queue.get(timeout=N)` 阻塞等待
- 不要 kill 生产进程，用独立端口测试

## 清洗后数据格式（dataset.jsonl 每行）

```json
{
  "request": {"model": "...", "messages": [...], "max_tokens": ...},
  "response": {
    "text": "完整回复文本",
    "thinking": "thinking block（可能为空）",
    "is_streaming": true,
    "usage": {"input_tokens": N, "output_tokens": N},
    "stop_reason": "end_turn"
  },
  "metadata": {"seq_id": 1, "model": "...", "duration_ms": 1234, "status": 200}
}
```
