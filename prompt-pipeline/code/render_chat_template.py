#!/usr/bin/env python3
"""
把 OpenAI 格式 messages 送入 tokenizer.apply_chat_template()，
得到模型最终看到的 prompt 纯文本或 token ids。

核心就一行:  tokenizer.apply_chat_template(messages, tools=..., tokenize=False)
vLLM 内部也是这条路。

用法:
    cd temp

    # 输出纯文本 prompt（默认）
    python3 code/render_chat_template.py

    # 输出 token ids（JSON）
    python3 code/render_chat_template.py --tokenize

    # 结构分析 + 预览
    python3 code/render_chat_template.py -s -p 5

    # 保存文件
    python3 code/render_chat_template.py -o data/rendered_prompt.txt
"""

import argparse
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description="Qwen3 chat template renderer")
    p.add_argument("input", nargs="?", default=str(SCRIPT_DIR.parent / "data" / "openai_converted.json"))
    p.add_argument("-o", "--output", help="输出文件")
    p.add_argument("--tokenize", "-t", action="store_true", help="输出 token ids")
    p.add_argument("--show-structure", "-s", action="store_true")
    p.add_argument("--model-dir", default="/var/gfs/public-models/Qwen/Qwen3.6-27B")
    p.add_argument("--no-generation-prompt", action="store_true")
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--preview", "-p", type=int, default=0)
    args = p.parse_args()

    # 1. load input
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    req = data.get("openai_request", data)

    # 2. load tokenizer
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, trust_remote_code=True)

    # 3. apply_chat_template — 核心就这一行，复用 tokenizer 内置函数
    kwargs = dict(
        conversation=req["messages"],
        tools=req.get("tools"),
        add_generation_prompt=not args.no_generation_prompt,
        enable_thinking=not args.no_thinking,
    )

    prompt = tokenizer.apply_chat_template(tokenize=False, **kwargs)
    enc = tokenizer.apply_chat_template(tokenize=True, **kwargs)
    token_ids = enc["input_ids"]
    if hasattr(token_ids, "tolist"):
        token_ids = token_ids.tolist()

    print(f"[info] {len(prompt):,} chars, {len(token_ids):,} tokens")

    # 4. structure analysis
    if args.show_structure:
        # Qwen3 的 ▌(248045) 是通用前缀，后面跟 system/user/assistant 普通 token
        # 所以我们扫描 token_ids 来标注 section
        PREFIX_ID = 248045  # ▌
        END_TURN_ID = 248046  # ▌\n

        # 找出 ▌ 出现的位置，看后面跟什么词
        sections = []
        for i, tid in enumerate(token_ids):
            if tid == PREFIX_ID and i + 1 < len(token_ids):
                next_tok = tokenizer.decode([token_ids[i + 1]])
                label = f"▌{next_tok.strip()}"
                sections.append((i, label))

        print(f"\n{'=' * 60}")
        print(f"  Section 结构 ({len(token_ids):,} tokens)")
        print(f"{'=' * 60}")

        prev = 0
        for idx, label in sections:
            delta = idx - prev
            print(f"  token {idx:>6}  (delta {delta:>6})  → {label}")
            prev = idx

        tail = len(token_ids) - prev
        print(f"  (end)     (delta {tail:>6})  → generation suffix")

        # ▌\n (end_of_turn) 统计
        eot_count = token_ids.count(END_TURN_ID)
        think_id = 248068
        think_count = token_ids.count(think_id)
        tool_id = 248058
        tool_count = token_ids.count(tool_id)
        print(f"\n  ▌\\n(end_of_turn): {eot_count}  ×")
        print(f"  <think>(thinking):  {think_count}  ×")
        print(f"  <tool_call>(tool_code):    {tool_count}  ×")
        print()

    # 5. output
    if args.tokenize:
        out = json.dumps(
            {"token_ids": token_ids, "num_tokens": len(token_ids), "model": req.get("model")},
            ensure_ascii=False,
        )
    else:
        out = prompt

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"[done] → {args.output}")
    elif args.preview > 0:
        N = args.preview
        lines = prompt.split("\n")
        total = len(lines)
        for line in lines[:N]:
            print(line)
        if 2 * N < total:
            print(f"... ({total - 2 * N} 行省略) ...")
        for line in lines[-N:]:
            print(line)
    else:
        print(out, end="" if not args.tokenize else "\n")


if __name__ == "__main__":
    main()
