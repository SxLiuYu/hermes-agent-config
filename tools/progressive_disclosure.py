#!/usr/bin/env python3
"""渐进式信息披露工具 — 对标 Apple 设计哲学

回复分三层次，用户自己控制信息深度。

用法:
    python3 progressive_disclosure.py format --content "..."   # 将内容格式化为三层
    python3 progressive_disclosure.py inject                   # 注入写作风格指导
"""

import argparse
import json
import sys
import textwrap
from typing import Optional


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

def split_content(content: str) -> dict:
    """
    将输入内容按语义拆分为三层结构。

    策略：
    - TL;DR:     取首句或提炼核心结论，≤50 字
    - 关键步骤:   提取列表/步骤性语句，≤300 字
    - 详细分析:   完整原文
    """
    stripped = content.strip()

    # --- TL;DR: 取第一句或前 50 字 ---
    sentences = stripped.replace("\n", " ").split("。")
    tldr = sentences[0].strip()
    if tldr.endswith("."):
        tldr = tldr[:-1]
    if len(tldr) > 50:
        tldr = tldr[:47] + "..."

    # --- 关键步骤: 尝试提取列表 ---
    lines = stripped.splitlines()
    steps_list: list[str] = []

    for line in lines:
        s = line.strip()
        if not s:
            continue
        # 数字列表 "1." "2、" 或 破折号/星号
        for prefix in ("- ", "* ", "• "):
            if s.startswith(prefix):
                steps_list.append(s[len(prefix):].strip())
                break

    if not steps_list:
        # 检查编号列表
        for line in lines:
            s = line.strip()
            if not s:
                continue
            dot_pos = s.find(".")
            comma_pos = s.find("、")
            if dot_pos in (1, 2) and s[:dot_pos].isdigit():
                steps_list.append(s[dot_pos + 1:].strip())
            elif comma_pos in (1, 2) and s[:comma_pos].isdigit():
                steps_list.append(s[comma_pos + 1:].strip())

    if not steps_list:
        # 没有显式列表，尝试按句拆分
        steps_list = [s.strip() for s in sentences[1:5] if s.strip()]

    key_steps = "\n".join(f"- {s}" for s in steps_list[:5])
    if len(key_steps) > 300:
        key_steps = key_steps[:297] + "..."

    return {
        "tldr": tldr,
        "key_steps": key_steps if key_steps else "(无显式步骤)",
        "detailed_analysis": stripped,
    }


def cmd_format(content: str) -> None:
    """将内容格式化为三层结构并输出 JSON。"""
    layers = split_content(content)

    result = {
        "action": "format",
        "structure": {
            "layer_1": {
                "name": "TL;DR",
                "max_chars": 50,
                "content": layers["tldr"],
            },
            "layer_2": {
                "name": "关键步骤",
                "max_chars": 300,
                "content": layers["key_steps"],
            },
            "layer_3": {
                "name": "详细分析",
                "max_chars": None,
                "content": layers["detailed_analysis"],
            },
        },
        "formatted_output": (
            f"**TL;DR:** {layers['tldr']}\n\n"
            f"**关键步骤:**\n{layers['key_steps']}\n\n"
            f"**详细分析:**\n{layers['detailed_analysis']}"
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


INJECTION_TEXT = textwrap.dedent("""\
## 📐 渐进式信息披露
回复结构:
- TL;DR: 一句话结论
- 关键步骤: 核心操作列表
- 详细分析: 完整过程
简单问题可只用第一层。""")


def cmd_inject() -> None:
    """注入写作风格指导。"""
    result = {
        "action": "inject",
        "injection": INJECTION_TEXT.strip(),
        "usage": "将以上内容追加到系统 prompt 中以启用渐进式信息披露风格",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="渐进式信息披露工具 — 回复分三层次，用户自己控制信息深度",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s format --content "Python 是一门高级编程语言。它支持多种编程范式..." 
  %(prog)s inject
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fmt_parser = sub.add_parser("format", help="将内容格式化为三层结构")
    fmt_parser.add_argument(
        "--content",
        required=True,
        help="要格式化的文本内容",
    )

    sub.add_parser("inject", help="生成渐进式信息披露写作风格注入文本")

    args = parser.parse_args()

    if args.command == "format":
        cmd_format(args.content)
    elif args.command == "inject":
        cmd_inject()


if __name__ == "__main__":
    main()