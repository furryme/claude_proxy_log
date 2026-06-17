# Patch 分析报告：vLLM Anthropic → OpenAI 请求格式重构

## 1. 概述

`patch-vllm-inline-system.py` 对已安装的 vLLM 0.20.0 包进行了**字符串替换级别的热补丁**，目的是让 vLLM 的 Anthropic API 入口支持 **messages 数组内嵌 system role** 的消息（原始 vLLM 只接受 top-level `system` 字段）。

### 被补丁的原始文件

| 补丁目标 | 原始路径 |
|----------|----------|
| `protocol.py` | `/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/anthropic/protocol.py` |
| `serving.py` | `/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/anthropic/serving.py` |

## 2. Patch 的三处修改

### Patch 1：protocol.py — 允许 `system` role

```python
# 修改前
role: Literal["user", "assistant"]

# 修改后
role: Literal["user", "assistant", "system"]
```

**影响**：`AnthropicMessage` 的 Pydantic 模型原本只接受 `user` 和 `assistant` role。
补丁后增加了 `system`，使得反序列化时不会报错。

### Patch 2：serving.py `_convert_system_message` — 收集内嵌 system

**原始逻辑**：只从 `anthropic_request.system`（top-level 字段）构建 system message。

**补丁后逻辑**：
1. 先收集 top-level `system` 字段的内容（字符串或 content block 数组）
2. 再遍历 `anthropic_request.messages`，找出 `role == "system"` 的消息
3. 将两者的文本拼接成一个 `system_parts` 列表
4. 用 `"".join(system_parts)` 构造最终 OpenAI system message

**特殊处理**：跳过以 `x-anthropic-billing-header` 开头的文本块（Claude Code 的请求头 hash，破坏 prefix caching）。

### Patch 3：serving.py `_convert_messages` — 跳过内嵌 system

**原始逻辑**：遍历所有消息，直接转换为 OpenAI 格式。

**补丁后逻辑**：在循环开头增加：
```python
if msg.role == "system":
    continue
```
这样内嵌的 system 消息不会被重复添加到 OpenAI messages 中（它们已经被 `_convert_system_message` 处理了）。

## 3. 完整的 Anthropic → OpenAI 转换流程

`_convert_anthropic_to_openai_request` 方法调用了以下步骤：

```
┌─────────────────────────────────────────────┐
│  Anthropic Messages Request                  │
│  - system: str | list[ContentBlock]          │
│  - messages: list[Msg] (含内嵌 system)       │
│  - tools: list[AnthropicTool]                │
│  - tool_choice: AnthropicToolChoice          │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│ Step 1: _convert_system_message              │
│  - 收集 top-level system + 内嵌 system       │
│  - 过滤 x-anthropic-billing-header           │
│  - 拼接成 {"role":"system", "content":"..."} │
│  - 放入 openai_messages[0]                   │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│ Step 2: _convert_messages                    │
│  - 跳过 role=="system" 的消息                │
│  - 对每个消息调用 _convert_message_content    │
│    - text → {"type":"text","text":"..."}     │
│    - image → {"type":"image_url",...}        │
│    - thinking → openai_msg["reasoning"]      │
│    - tool_use → openai_msg["tool_calls"]     │
│    - tool_result → {"role":"tool",...}       │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│ Step 3: _build_base_request                  │
│  - 构造 ChatCompletionRequest                │
│  - 映射 max_tokens, stop, temp, top_p, top_k │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│ Step 4: _convert_tools                       │
│  AnthropicTool → OpenAI ChatCompletionTools  │
│  {"name","description","input_schema"}        │
│  → {"type":"function","function":{            │
│       "name","description","parameters"}}     │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│ Step 5: _convert_tool_choice                 │
│  auto→auto, any→required, none→none          │
│  tool→ChatCompletionNamedToolChoiceParam     │
└─────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│  OpenAI ChatCompletionRequest                │
└─────────────────────────────────────────────┘
```

## 4. t.json 数据结构分析

`data/t.json` 是一个抓包日志文件，结构如下：

```
{
  "request": {                      # Anthropic 格式的原始请求
    "model": "Qwen3.6-27B",
    "messages": [                   # 消息数组
      {"role": "user", "content": [...]},    # 5 个 text content blocks
      {"role": "system", "content": "..."}   # 内嵌 system (skills 列表)
    ],
    "system": [...],                # top-level system (3 个 content blocks)
    "max_tokens": 32000,
    "tools": [...],                 # 38 个工具定义
    "thinking": {"type": "adaptive"}
  },
  "response": {...},                # 响应（非转换目标）
  "metadata": {...}                 # 元数据（非转换目标）
}
```

### 需要转换的关键点

| Anthropic 字段 | OpenAI 目标 | 转换规则 |
|---------------|-------------|---------|
| `system` (top-level) + `messages` 中 `role:system` | `messages[0].role: "system"` | 拼接所有 system 文本，过滤 billing header |
| `messages[].content[]` (block 数组) | `messages[].content` (字符串或数组) | 提取 text，合并为单一字符串或内容数组 |
| `tools[]` (Anthropic 格式) | `tools[]` (OpenAI function 格式) | `input_schema` → `parameters`，包裹 `function` 对象 |
| `thinking` | 无直接对应 | 可忽略或作为 metadata |
| `cache_control` | 无对应 | 丢弃 |

## 5. 结论

补丁的核心思想是 **"合并 system + 跳过重复"**：
1. `_convert_system_message` 负责把分散在 top-level 和 messages 里的 system 内容合并
2. `_convert_messages` 负责跳过已经被处理的内嵌 system，避免重复

这种设计保证了无论 system 出现在哪个位置，最终都会被正确地转换成 OpenAI 格式的第一个 message。
