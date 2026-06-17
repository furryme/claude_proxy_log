# Anthropic → OpenAI → Qwen3 模型输入：完整转换管线

## 背景

Claude Code（或兼容 Anthropic API 的客户端）通过 vLLM 的 Anthropic 协议端点发送请求，
vLLM 内部把 Anthropic 格式转成 OpenAI 格式，再走 chat template 渲染为模型可读的 prompt。

本文件夹复现这条完整管线，把每一步的输入输出和转换逻辑都记录下来。

## 目录结构

```
prompt-pipeline/
├── README.md                  ← 本文件
├── code/                      ← 转换脚本
│   ├── convert_to_openai.py   ← Step 1: Anthropic → OpenAI
│   └── render_chat_template.py ← Step 2: OpenAI → prompt / token ids
├── data/                      ← 数据文件（按管线顺序）
│   ├── t.json                 ← 原始 Anthropic 抓包 (输入)
│   ├── openai_converted.json  ← OpenAI 格式 (中间产物)
│   └── rendered_tokens.json   ← Token ids (最终输出)
├── model-deps/                ← Qwen3.6-27B 模型依赖（从模型目录复制）
│   ├── chat_template.jinja    ← Jinja2 chat template
│   ├── config.json            ← 模型配置
│   ├── tokenizer_config.json  ← Tokenizer 配置
│   ├── vocab.json             ← BPE 词表
│   ├── merges.txt             ← BPE merges
│   └── generation_config.json ← 生成配置
└── docs/                      ← 分析报告
    ├── analysis_report.md            ← patch-vllm-inline-system.py 分析
    └── chat_template_explanation.md  ← Chat template 渲染流程详解
```

## 管线流程

```
data/t.json (Anthropic 格式)
    │
    ▼
code/convert_to_openai.py
    │  合并 system (top-level + messages 内嵌)
    │  转换 content blocks → 字符串 / 数组
    │  转换 tools (Anthropic → OpenAI function format)
    │  过滤 cache_control、billing header 等
    ▼
data/openai_converted.json (OpenAI ChatCompletionRequest 格式)
    │
    ▼
code/render_chat_template.py
    │  tokenizer.apply_chat_template(messages, tools=...)
    │  tokenize=False → 纯文本 prompt
    │  tokenize=True  → token ids
    ▼
纯文本 prompt (110K chars)  /  token ids (28,177 tokens)
```

## 快速开始

### 前置条件

```bash
# 需要 transformers (tokenizer)
pip install transformers

# convert_to_openai.py 还能用 vLLM 原生函数（可选）
pip install vllm
```

### 运行管线

```bash
cd prompt-pipeline

# Step 1: Anthropic → OpenAI
python3 code/convert_to_openai.py data/t.json data/openai_converted.json

# Step 2: 渲染为纯文本 prompt
python3 code/render_chat_template.py data/openai_converted.json -o data/rendered_prompt.txt

# Step 2 (替代): 渲染为 token ids
python3 code/render_chat_template.py data/openai_converted.json --tokenize -o data/rendered_tokens.json

# 结构分析
python3 code/render_chat_template.py data/openai_converted.json --show-structure

# 预览
python3 code/render_chat_template.py data/openai_converted.json --preview 10
```

## 数据文件说明

### `data/t.json` (131K, 原始输入)

从 Claude Code 会话抓包的 Anthropic 格式请求，结构：

```json
{
  "request": {
    "model": "Qwen3.6-27B",
    "messages": [
      { "role": "user",    "content": [{"type":"text","text":"..."}, ...] },
      { "role": "system",  "content": "skills list ..." }
    ],
    "system": [                          // top-level system
      { "type": "text", "text": "x-anthropic-billing-header: ..." },
      { "type": "text", "text": "You are Claude Code..." }
    ],
    "max_tokens": 32000,
    "tools": [...],                      // 38 个工具 (Anthropic 格式)
    "thinking": { "type": "adaptive" }
  },
  "response": { "text": "...", "thinking": "...", ... },
  "metadata": { "seq_id": 1, ... }
}
```

### `data/openai_converted.json` (136K, 中间产物)

Step 1 转换后的 OpenAI `ChatCompletionRequest` 格式：

```json
{
  "openai_request": {
    "model": "Qwen3.6-27B",
    "messages": [
      { "role": "system", "content": "合并后的 system prompt (11848 chars)" },
      { "role": "user",   "content": [{"type":"text","text":"..."}, ...] }
    ],
    "max_tokens": 32000,
    "tools": [
      { "type": "function", "function": { "name": "Agent", ... } }
      // ... 38 个工具
    ],
    "tool_choice": "auto"
  },
  "_source": { /* 转换统计 */ }
}
```

**关键变化：**
- top-level `system` + messages 内嵌 system → 合并为 `messages[0]` (role: system)
- Anthropic tools `{"name","input_schema"}` → OpenAI `{"type":"function","function":{"name","parameters"}}`
- 丢弃 `cache_control`、`thinking`、billing header 等 Anthropic 特有字段

### `data/rendered_tokens.json` (156K, 最终输出)

Step 2 的 tokenization 结果：

```json
{
  "token_ids": [248045, 8678, 198, ...],
  "num_tokens": 28177,
  "model": "Qwen3.6-27B"
}
```

**token 级结构：**

| Section | Token 范围 | 大小 | 内容 |
|---------|-----------|------|------|
| `▌system` | 0 – 27016 | 27,017 | 工具定义 + system prompt |
| `▌user` | 27017 – 28171 | 1,155 | 用户消息 |
| `▌assistant\n▌thinking\n` | 28172 – 28176 | 5 | 生成起点 |

## 脚本说明

### `code/convert_to_openai.py`

将 Anthropic 格式请求转换为 OpenAI ChatCompletionRequest 格式。

**转换逻辑**（与 vLLM `serving.py` 中的 `_convert_anthropic_to_openai_request` 一致）：

1. `_convert_system_message()` — 合并 top-level `system` + messages 内嵌 system，过滤 billing header
2. `_convert_messages()` — 跳过内嵌 system，转换 text/image/thinking/tool_use/tool_result blocks
3. `_convert_tools()` — Anthropic `{"name","input_schema"}` → OpenAI `{"type":"function","function":{...}}`
4. `_convert_tool_choice()` — `auto/any/none/tool` 映射

**运行方式：** 优先使用 vLLM 原生函数（已安装时自动导入），否则使用内联复刻的转换逻辑。

### `code/render_chat_template.py`

调用 `tokenizer.apply_chat_template()` 将 OpenAI 格式消息渲染为模型可读的 prompt。

**核心就一行：**

```python
tokenizer.apply_chat_template(
    conversation=messages,
    tools=tools,
    add_generation_prompt=True,
    enable_thinking=True,
    tokenize=False,  # 或 True 返回 token ids
)
```

**命令行参数：**

| 参数 | 说明 |
|------|------|
| `INPUT` | 输入 JSON（默认 `data/openai_converted.json`） |
| `-o OUTPUT` | 输出文件 |
| `--tokenize / -t` | 输出 token ids 而非纯文本 |
| `--show-structure / -s` | 显示 section 结构分析 |
| `--model-dir DIR` | 模型目录（默认 `/var/gfs/public-models/Qwen/Qwen3.6-27B`） |
| `--preview N / -p N` | 预览前 N 行和后 N 行 |
| `--no-generation-prompt` | 不加 `▌assistant\n▌thinking\n` 结尾 |
| `--no-thinking` | 禁用 thinking mode |

## 模型依赖 (`model-deps/`)

从 `/var/gfs/public-models/Qwen/Qwen3.6-27B/` 复制的文件，供本地参考和离线使用。
脚本运行时直接从模型目录加载 tokenizer，不需要这些文件的本地副本。

## 报告 (`docs/`)

- **`analysis_report.md`** — `patch-vllm-inline-system.py` 的三处修改分析（protocol.py role 扩展、
  `_convert_system_message` 合并逻辑、`_convert_messages` 跳过逻辑）
- **`chat_template_explanation.md`** — Qwen3 chat template 的完整渲染流程，
  包括特殊 token、有/无 tools 时的模板行为、section 拼接规则

## 环境

- **模型**: Qwen3.6-27B (`/var/gfs/public-models/Qwen/Qwen3.6-27B`)
- **Tokenizer**: Qwen2Tokenizer (transformers)
- **vLLM**: 0.20.0 (已安装，`convert_to_openai.py` 优先使用其原生转换函数)
- **Python**: 3.12
