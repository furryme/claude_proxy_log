#!/usr/bin/env python3
"""
将 Anthropic 格式请求（data/t.json）转换为 OpenAI chat completion 格式。

复用 vLLM 源码中 serving.py 的核心转换逻辑，尽量引用原始函数。
输出文件: data/openai_converted.json

用法:
    python3 code/convert_to_openai.py data/t.json data/openai_converted.json
"""

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 尝试从已安装的 vLLM 导入原始函数（优先路径）
# ---------------------------------------------------------------------------
try:
    from vllm.entrypoints.anthropic.serving import AnthropicServingMessages

    USE_VLLM_NATIVE = True
    print("[info] 成功导入 vLLM 原生 AnthropicServingMessages 类")
except ImportError:
    USE_VLLM_NATIVE = False
    print("[warn] 无法导入 vLLM，使用内联复刻的转换函数")


# ---------------------------------------------------------------------------
# 内联复刻：当 vLLM 不可用时的 fallback（与 serving.py 逻辑一致）
# ---------------------------------------------------------------------------

def _convert_system_message_from_dict(
    system: Any,
    messages: list[dict],
) -> str | None:
    """
    复刻 AnthropicServingMessages._convert_system_message 的逻辑。

    从 top-level system 和 messages 中内嵌的 system role 收集所有 system 文本，
    过滤 x-anthropic-billing-header，拼接后返回。
    """
    system_parts: list[str] = []

    # -- top-level system 字段 --
    if system:
        if isinstance(system, str):
            system_parts.append(system)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    text = block["text"]
                    if text.startswith("x-anthropic-billing-header"):
                        continue
                    system_parts.append(text)

    # -- messages 数组中内嵌的 system --
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            system_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    text = block["text"]
                    if text.startswith("x-anthropic-billing-header"):
                        continue
                    system_parts.append(text)

    return "".join(system_parts) if system_parts else None


def _convert_content_blocks(content: Any) -> tuple[str | list[dict] | None, str | None]:
    """
    将 Anthropic content blocks 转换为 OpenAI content 格式。

    返回 (content, reasoning_text)
    - 单一 text block → 字符串
    - 多个 block → 字典列表
    - thinking block → 提取到 reasoning
    """
    if isinstance(content, str):
        return content, None

    if not isinstance(content, list):
        return None, None

    content_parts: list[dict[str, Any]] = []
    reasoning_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            content_parts.append({"type": "text", "text": block["text"]})
        elif btype == "image" and block.get("source"):
            source = block["source"]
            if source.get("type") == "url":
                url = source.get("url", "")
            else:
                media_type = source.get("media_type", "image/jpeg")
                data = source.get("data", "")
                url = f"data:{media_type};base64,{data}"
            content_parts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "thinking" and block.get("thinking") is not None:
            reasoning_parts.append(block["thinking"])
        # 跳过 redacted_thinking, cache_control 等非内容字段

    reasoning = "".join(reasoning_parts) if reasoning_parts else None

    if len(content_parts) == 1 and content_parts[0]["type"] == "text":
        content_result = content_parts[0]["text"]
    elif content_parts:
        content_result = content_parts
    else:
        content_result = None

    return content_result, reasoning


def _convert_tools_anthropic_to_openai(tools: list[dict]) -> list[dict]:
    """
    复刻 AnthropicServingMessages._convert_tools 的逻辑。

    Anthropic: {"name", "description", "input_schema": {...}}
    OpenAI:    {"type": "function", "function": {"name", "description", "parameters"}}
    """
    openai_tools = []
    for tool in tools:
        openai_tool = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object"}),
            },
        }
        openai_tools.append(openai_tool)
    return openai_tools


def _convert_anthropic_request_to_openai(req: dict) -> dict:
    """
    将 Anthropic 格式的 request dict 转换为 OpenAI chat completion 格式。

    对应 serving.py 中的 _convert_anthropic_to_openai_request 方法。
    """
    openai_messages: list[dict[str, Any]] = []

    # Step 1: 转换 system（复用原始逻辑）
    system_text = _convert_system_message_from_dict(
        req.get("system"), req.get("messages", [])
    )
    if system_text:
        openai_messages.append({"role": "system", "content": system_text})

    # Step 2: 转换 messages（跳过内嵌 system）
    for msg in req.get("messages", []):
        if msg.get("role") == "system":
            continue

        content, reasoning = _convert_content_blocks(msg.get("content", ""))

        openai_msg: dict[str, Any] = {"role": msg["role"]}
        if content is not None:
            openai_msg["content"] = content
        if reasoning is not None:
            openai_msg["reasoning"] = reasoning

        # 过滤掉空 content 的 user 消息（与原逻辑一致）
        if msg["role"] == "user" and "content" not in openai_msg:
            continue

        openai_messages.append(openai_msg)

    # Step 3: 构建 base request
    openai_req = {
        "model": req.get("model", ""),
        "messages": openai_messages,
    }

    # 可选字段
    if req.get("max_tokens"):
        openai_req["max_tokens"] = req["max_tokens"]

    if req.get("temperature") is not None:
        openai_req["temperature"] = req["temperature"]

    if req.get("top_p") is not None:
        openai_req["top_p"] = req["top_p"]

    if req.get("stop_sequences"):
        openai_req["stop"] = req["stop_sequences"]

    # Step 4: 转换 tools
    if req.get("tools"):
        openai_req["tools"] = _convert_tools_anthropic_to_openai(req["tools"])
        if "tool_choice" not in openai_req:
            openai_req["tool_choice"] = "auto"

    # Step 5: 转换 tool_choice
    if req.get("tool_choice"):
        tc = req["tool_choice"]
        tc_type = tc.get("type") if isinstance(tc, dict) else str(tc)
        type_map = {"auto": "auto", "any": "required", "none": "none"}
        if tc_type in type_map:
            openai_req["tool_choice"] = type_map[tc_type]
        elif tc_type == "tool":
            openai_req["tool_choice"] = {
                "type": "function",
                "function": {"name": tc.get("name", "")},
            }

    return openai_req


# ---------------------------------------------------------------------------
# 使用 vLLM 原生函数进行转换（如果可用）
# ---------------------------------------------------------------------------

def _convert_via_vllm_native(req: dict) -> dict:
    """
    使用 vLLM 原生的 AnthropicServingMessages 类进行转换。
    先将 dict 序列化为 AnthropicMessagesRequest，再调用原生方法。
    """
    from vllm.entrypoints.anthropic.protocol import (
        AnthropicContentBlock,
        AnthropicMessage,
        AnthropicMessagesRequest,
        AnthropicTool,
    )

    # 反序列化 messages
    messages = []
    for m in req.get("messages", []):
        content = m["content"]
        if isinstance(content, list):
            blocks = [AnthropicContentBlock(**b) for b in content if "cache_control" not in b or True]
            # 清理 cache_control（vLLM 的 AnthropicContentBlock 不认这个字段）
            clean_blocks = []
            for b in content:
                cleaned = {k: v for k, v in b.items() if k != "cache_control"}
                clean_blocks.append(AnthropicContentBlock(**cleaned))
            messages.append(AnthropicMessage(role=m["role"], content=clean_blocks))
        else:
            messages.append(AnthropicMessage(role=m["role"], content=content))

    # 反序列化 system
    system = req.get("system")
    if isinstance(system, list):
        system = [
            AnthropicContentBlock(**{k: v for k, v in b.items() if k != "cache_control"})
            for b in system
        ]

    # 反序列化 tools
    tools = req.get("tools")
    if tools:
        tools = [AnthropicTool(**t) for t in tools]

    # 构建 AnthropicMessagesRequest
    anthropic_req = AnthropicMessagesRequest(
        model=req["model"],
        messages=messages,
        max_tokens=req["max_tokens"],
        system=system,
        tools=tools,
    )

    # 调用原生转换方法
    openai_req = AnthropicServingMessages._convert_anthropic_to_openai_request(anthropic_req)

    # 转换为普通 dict
    return json.loads(openai_req.model_dump_json(exclude_none=True))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    input_file = str(SCRIPT_DIR.parent / "data" / "t.json")
    output_file = str(SCRIPT_DIR.parent / "data" / "openai_converted.json")

    # 支持通过命令行参数覆盖
    if len(sys.argv) >= 2:
        input_file = sys.argv[1]
    if len(sys.argv) >= 3:
        output_file = sys.argv[2]

    print(f"[info] 读取输入文件: {input_file}")
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    req = data["request"]

    # 优先使用 vLLM 原生函数
    if USE_VLLM_NATIVE:
        print("[info] 使用 vLLM 原生 _convert_anthropic_to_openai_request 进行转换")
        try:
            openai_req = _convert_via_vllm_native(req)
        except Exception as e:
            print(f"[warn] vLLM 原生转换失败 ({e})，回退到内联复刻函数")
            openai_req = _convert_anthropic_request_to_openai(req)
    else:
        print("[info] 使用内联复刻的转换函数（与 serving.py 逻辑一致）")
        openai_req = _convert_anthropic_request_to_openai(req)

    # 附加响应信息（可选，方便调试）
    output = {
        "openai_request": openai_req,
        "_source": {
            "original_file": input_file,
            "original_model": req.get("model"),
            "original_messages_count": len(req.get("messages", [])),
            "original_system_blocks": len(req.get("system", [])) if isinstance(req.get("system"), list) else (1 if req.get("system") else 0),
            "original_tools_count": len(req.get("tools", [])),
            "converted_system_length": len(openai_req["messages"][0]["content"])
            if openai_req["messages"] and openai_req["messages"][0]["role"] == "system"
            else 0,
            "converted_messages_count": len(openai_req["messages"]),
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n[done] 转换完成！")
    print(f"  输出文件: {output_file}")
    print(f"  原始 messages: {output['_source']['original_messages_count']} 条")
    print(f"  原始 system blocks: {output['_source']['original_system_blocks']} 个")
    print(f"  转换后 messages: {output['_source']['converted_messages_count']} 条")
    print(f"  转换后 system 长度: {output['_source']['converted_system_length']} 字符")
    print(f"  转换后 tools: {len(openai_req.get('tools', []))} 个")


if __name__ == "__main__":
    main()
