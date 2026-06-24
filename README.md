# Claude API Proxy with Dataset Capture

HTTP 代理服务器，在转发 Claude API 请求的同时，自动捕获并归档每条请求/响应，用于构建训练数据集。

## 快速开始

```bash
# 启动代理（默认监听 127.0.0.1:8001）
python claude_api_proxy.py

# 配置 Claude Code 使用此代理
export ANTHROPIC_BASE_URL=http://127.0.0.1:8001
```

## 工作流程

```
Claude Code ──▶ 代理 (8001) ──▶ 上游 LLM (Qwen/Claude)
                      │
                  SQLite 实时存储
                      │
              python dataset_archiver.py
                      │
              logs/archives/dataset_YYYY-MM-DD_HH.tar.gz
```

所有请求/响应存入 `logs/raw.db`（SQLite），按小时归档为压缩数据集。

## 架构

```
Claude Code ──▶ 代理 (claude_api_proxy.py)
                    │
                    ├─ 透明转发 ──▶ 上游 LLM
                    │
                    └─ RequestLogger (request_logger.py)
                         │  缓冲请求 body + 响应 chunks
                         │  finish() → queue.put_nowait()  ← 满则丢弃，零阻塞
                         ▼
                    Writer 线程 (log_store.py)
                         │  body → gzip(6) 压缩
                         │  response → 原样存储 (brotli/gzip/plaintext)
                         │  batch INSERT (2s 或 50 条)
                         ▼
                    logs/raw.db (SQLite, WAL mode)
```

### 文件职责

| 文件 | 职责 |
|------|------|
| `claude_api_proxy.py` | 代理主程序，`ThreadingHTTPServer`，透明转发 + 触发日志记录 |
| `request_logger.py` | 请求/响应缓冲器，`finish()` 非阻塞投递到 writer queue |
| `log_store.py` | SQLite 存储层，后台 writer 线程，gzip 压缩 body，批量写入 |
| `dataset_cleaner.py` | 数据清洗：解压 → SSE 重组 → 去重 → 输出 `dataset.jsonl` |
| `dataset_archiver.py` | 数据归档：按 `hour_key` 清洗 → 打包 `tar.gz` → 删除已归档记录 |
| `archive_cron.py` | 定时归档管理：cron 安装/卸载/执行 |
| `log_viewer.py` | 日志查看器：终端交互式浏览 SQLite 日志 |
| `web_viewer/api.py` | 浏览器端日志查看器后端 API（推荐，功能更丰富） |
| `web_viewer/index.html` | 浏览器端日志查看器前端（SPA） |
| `claude_api_proxy.py.pure_bk` | 代理主程序原始备份（修改前的纯净版本） |

## 目录结构

```
├── claude_api_proxy.py              # 代理主程序
├── request_logger.py                # 请求/响应缓冲
├── log_store.py                     # SQLite 存储层
├── dataset_cleaner.py               # 数据清洗
├── dataset_archiver.py              # 数据归档
├── archive_cron.py                  # 定时归档
├── log_viewer.py                    # 日志查看（终端版）
├── web_viewer/                      # 浏览器端日志查看器（推荐）
│   ├── api.py                       #   后端 API
│   ├── index.html                   #   前端 SPA
│   └── static/                      #   CSS / JS
├── prompt-pipeline/                 # 格式转换管线（详见 prompt-pipeline/README.md）
│   ├── code/                        #   转换脚本
│   ├── data/                        #   数据文件
│   ├── docs/                        #   分析报告
│   └── model-deps/                  #   Qwen3 tokenizer 依赖
└── logs/                            # 日志输出
    ├── raw.db                       #   SQLite 数据库
    ├── cleaned/YYYY-MM-DD_HH/       #   清洗后数据集
    └── archives/                    #   归档压缩包
```

## 数据格式

### 归档数据集 (dataset.jsonl)

每行一个完整的训练样本：

```json
{
  "request": {
    "model": "Qwen3.6-27B",
    "messages": [{"role": "user", "content": "..."}, ...],
    "system": "...",
    "max_tokens": 8192
  },
  "response": {
    "text": "完整的 AI 回复文本",
    "thinking": "thinking block（可选）",
    "is_streaming": true,
    "usage": {"input_tokens": 1500, "output_tokens": 800},
    "stop_reason": "end_turn"
  },
  "metadata": {"seq_id": 1, "model": "Qwen3.6-27B", "duration_ms": 2000}
}
```

### 数据清洗规则

1. 仅保留 `POST /v1/messages` 且 `status=200` 的请求
2. 自动检测并解压 gzip/brotli 响应
3. SSE 流式响应重组为完整 text + thinking
4. 删除响应为空或过短的记录
5. 同一 (model, messages) 组合去重

## prompt-pipeline — 格式转换管线

Anthropic API 格式 → OpenAI 格式 → tokenizer → token ids 的完整转换链路，用于分析每一步的输入输出。详见 `prompt-pipeline/README.md`。

## 操作指南

### 查看日志

```bash
# 浏览器端查看器（推荐，功能最全）
python3 web_viewer/api.py --port 8002
# 打开 http://127.0.0.1:8002/

# 终端查看器
python log_viewer.py              # 列出请求
python log_viewer.py --latest     # 最新一条
python log_viewer.py --seq-id 42  # 指定记录
python log_viewer.py --latest --summary   # 仅摘要
python log_viewer.py --latest --raw       # 原始 SSE
python log_viewer.py --date 2026-06-16 --hour 14  # 按日期筛选
```

### 数据清洗

```bash
# 清洗指定小时的数据
python dataset_cleaner.py --hour-key 2026-06-16_14

# 设置最小输出长度（默认 2）
python dataset_cleaner.py --hour-key 2026-06-16_14 --min-output 20

# 清洗所有时间窗口
python dataset_cleaner.py
```

### 归档

```bash
# 归档最旧的时间窗口
python dataset_archiver.py

# 归档指定时间窗口
python dataset_archiver.py --date 2026-06-16 --hour 14

# 归档指定时间点以前的所有数据
python dataset_archiver.py --before 2026-06-20_14

# 归档所有未归档的窗口
python dataset_archiver.py --archive-all

# 保留原始数据（不删除）
python dataset_archiver.py --keep-source

# 预演（不执行）
python dataset_archiver.py --dry-run
```

归档模式对比：

| 参数 | 效果 |
|------|------|
| 无参数 | 归档最旧的**一个**未归档窗口 |
| `--date D --hour H` | 归档指定的**一个**窗口 |
| `--before D_H` | 归档该时间点以前的**所有**窗口 |
| `--archive-all` | 归档数据库中**全部**窗口 |

### 定时归档

```bash
# 查看将安装的 cron 命令
python archive_cron.py --show

# 安装定时任务（默认每 4 小时）
python archive_cron.py --install

# 自定义频率（每天凌晨 2 点）
python archive_cron.py --install --minute 0 --hour 2

# 立即执行一次归档（测试用）
python archive_cron.py --run-now

# 移除定时任务
python archive_cron.py --remove
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | `8001` | 代理监听端口 |
| `UPSTREAM_TIMEOUT` | `1200` | 上游超时（秒） |
| `LOGS_ROOT` | `logs` | 日志根目录 |
| `DISABLE_LOGGING` | `false` | 设为 `1` 关闭日志捕获 |
| `KEEP_SOURCE` | `false` | 设为 `1` 归档后保留原始数据 |
| `RETAIN_DAYS` | `0` | 归档保留天数（0=永久） |
| `RESPONSE_CHUNK_SIZE` | `65536` | 响应读取块大小（字节） |
| `CONFIG_PATH` | `config/routes.json` | 路由配置文件路径 |

## 路由配置

编辑 `config/routes.json`，按 model 路由到不同上游。配置文件由代理启动时自动加载，无需重启。

### 配置文件格式 (`config/routes.json`)

```json
{
  "Qwen3.6-27B": {
    "url": "http://upstream-host:port/path",
    "cookie_file": "/path/to/cookies.txt"  // 可选，支持 Netscape 格式和 key=value 格式
  },
  "SimpleModel": "http://another-upstream:port",  // 简写：仅 URL
  "__default_upstream__": "http://fallback-host:port"  // 未匹配 model 时的兜底
}
```

### 说明

- `cookie_file` 支持两种格式：Netscape cookie 格式和 `key=value` 格式（每行一个）
- Cookie 文件加载后缓存，代理运行期间不会重新读取
- **安全提示**：`config/routes.json` 包含内网地址和 cookie 路径，应加入 `.gitignore`；提交时使用 `config/routes.example.json` 作为模板参考

## 依赖

- Python 3.10+
- `brotli` — 解压 brotli 压缩的响应（`pip install brotli`，非必须，缺失时跳过 brotli 数据）
- 其余组件仅使用 Python 标准库

```bash
pip install brotli
```
