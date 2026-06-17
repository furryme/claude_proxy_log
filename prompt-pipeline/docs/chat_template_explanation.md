# 从 OpenAI 格式到模型输入：Chat Template 完整渲染流程

## 1. 完整数据流

```
Anthropic 格式请求 (t.json)
    │
    ▼
_convert_anthropic_to_openai_request()    ←  vllm/entrypoints/anthropic/serving.py
    │
    ▼
OpenAI ChatCompletionRequest (openai_converted.json)
    {
      "model": "Qwen3.6-27B",
      "messages": [
        {"role": "system", "content": "You are Claude Code..."},
        {"role": "user",   "content": [{"type":"text","text":"..."}, ...]}
      ],
      "tools": [...],
      "max_tokens": 32000,
      ...
    }
    │
    ▼
create_chat_completion() → render_chat_request()    ←  vLLM 引擎
    │
    ▼
parse_chat_messages()                                ←  处理 content parts, reasoning, tool_calls
    │                                                   → 解析 image/video/多模态 placeholder
    ▼
apply_chat_template()                                ←  Jinja2 模板渲染
    │
    ▼
"<|system|>\n# Tools\n...\n<|end_of_turn|>\n<|user|>\n...\n<|end_of_turn|>\n<|assistant|>\n<|thinking|>\n"
    │
    ▼
tokenizer.encode() → token ids
    │
    ▼
模型前向推理
```

## 2. 关键代码路径

### 2.1 `_convert_anthropic_to_openai_request()` → serving.py L121-133

把 Anthropic 请求转为 OpenAI 格式（上一份报告已详述）。

### 2.2 `parse_chat_messages()` → chat_utils.py L1609-1642

处理 content parts（图片/视频多模态 placeholder），纯文本场景下几乎透传。

### 2.3 Jinja2 模板渲染

使用 `/var/gfs/public-models/Qwen/Qwen3.6-27B/chat_template.jinja`。

---

## 3. Qwen3 Chat Template 格式详解

### 3.1 核心特殊 Token

| Token | 作用 |
|-------|------|
| `<|system|>` | system 消息开始 |
| `<|user|>` | user 消息开始 |
| `<|assistant|>` | assistant 消息开始 |
| `<|thinking|>` | 推理/思考开始 |
| `<|end_of_turn|>` | 消息结束的 EOS 标记 |

> 注意：你在 chat_template.jinja 中看到的是 `▌` 符号，它对应 tokenizer 中的控制 token id。
> `▌system` = `<|system|>`，单独的 `▌` = `<|end_of_turn|>`。

### 3.2 无 Tools 时的渲染（最小示例）

输入：
```json
[
  {"role": "system", "content": "You are a helpful assistant."},
  {"role": "user", "content": "Hello"}
]
```

渲染结果：
```
▌system
You are a helpful assistant.
▌
▌user
Hello
▌
▌assistant
▌thinking
```

### 3.3 有 Tools 时的渲染（你的实际场景）

当 `tools` 非空时，模板会**改写 system 消息**，把 tool 定义插入最前面：

```
▌system
# Tools

You have access to the following functions:

<tools>
{"function": {"description": "...", "name": "Agent", "parameters": {...}}, "type": "function"}
{"function": {"description": "...", "name": "Bash", "parameters": {...}}, "type": "function"}
... (共 38 个工具，每个一行 JSON)
</tools>

If you choose to call a function ONLY reply in the following format with NO suffix:

[tool call format instructions]

<IMPORTANT>
Reminder:
- Function calls MUST follow the specified format
- Required parameters MUST be specified
- You may provide optional reasoning for your function call in natural language BEFORE the function call
- If there is no function call available, answer the question like normal
</IMPORTANT>

You are Claude Code, Anthropic's official CLI for Claude.
You are an interactive agent that helps users with software engineering tasks.
... (合并后的所有 system 内容，11848 字符)
▌
▌user
<system-reminder>...</system-reminder>
... (5 个 text block 拼接的内容)
阅读一下当前文件夹，告诉我下面的文件都是干什么的
▌
▌assistant
▌thinking
```

---

## 4. System 消息拼接规则（有 tools 时）

这是模板的核心逻辑（chat_template.jinja 第 45-60 行）：

```jinja
{%- if tools and tools is iterable and tools is not mapping %}
    {{- '▌system\n' }}
    {{- "# Tools\n\nYou have access to the following functions:\n\n<tools>" }}
    {%- for tool in tools %}
        {{- "\n" }}
        {{- tool | tojson }}
    {%- endfor %}
    {{- "\n</tools>" }}
    {{- '\n\n[tool call format instructions]...' }}
    {%- if messages[0].role == 'system' %}
        {%- set content = render_content(messages[0].content, false, true)|trim %}
        {%- if content %}
            {{- '\n\n' + content }}
        {%- endif %}
    {%- endif %}
    {{- '▌\n' }}
{%- else %}
    {%- if messages[0].role == 'system' %}
        {%- set content = render_content(messages[0].content, false, true)|trim %}
        {{- '▌system\n' + content + '▌\n' }}
    {%- endif %}
{%- endif %}
```

**关键点**：当有 tools 时，system message 的内容被追加到 tool 定义之后，用 `\n\n` 分隔。
也就是说：`_convert_system_message` 合并出来的 11848 字符的 system 文本，会被放在 tool 定义的后面。

---

## 5. User 消息的 content blocks 渲染

你的转换结果中 user 消息的 content 是一个 block 数组：
```json
"content": [
  {"type": "text", "text": "<system-reminder>...</system-reminder>\n"},
  {"type": "text", "text": "<local-command-caveat>...</local-command-caveat>\n"},
  {"type": "text", "text": "<command-name>/clear</command-name>..."},
  {"type": "text", "text": "<local-command-stdout></local-command-stdout>\n"},
  {"type": "text", "text": "阅读一下当前文件夹..."}
]
```

模板的 `render_content` macro（第 3-41 行）会遍历这些 block，提取 `item.text` 并直接拼接：

```jinja
{%- for item in content %}
    {%- elif 'text' in item %}
        {{- item.text }}
    {%- endif %}
{%- endfor %}
```

所以最终 user 部分就是 5 段 text 直接连在一起。

---

## 6. Generation Prompt

模板末尾（第 147-153 行）：

```jinja
{%- if add_generation_prompt %}
    {{- '▌assistant\n' }}
    {%- if enable_thinking is defined and enable_thinking is false %}
        {{- '▌thinking\n\n▌end_of_thought\n\n' }}
    {%- else %}
        {{- '▌thinking\n' }}
    {%- endif %}
{%- endif %}
```

`add_generation_prompt=True` 时，末尾追加 `<|assistant|>\n<|thinking|>\n`，
模型从这个位置开始生成（先输出思考内容，然后是 `<|end_of_thought|>`，再输出回答）。

---

## 7. 实际渲染结果统计

字段 | 字符数（约）
----|------
tools JSON (38 个工具) | ~60,000
system prompt 合并后 | ~11,800
user message | ~7,000
tool call format instructions | ~800
**总计 prompt** | **~80,000 字符**

经过 tokenizer 编码后，预计约 20,000 - 25,000 个 token。
