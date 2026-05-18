#!/usr/bin/env python3
"""意图消歧工具 — 对标人类对话模式

模糊指令不猜测，用最小代价（1个精准问题）澄清。

用法:
    python3 intent_disambiguator.py detect --command "..."    # 检测是否模糊
    python3 intent_disambiguator.py question --command "..."  # 生成精准澄清问题
    python3 intent_disambiguator.py inject                    # 注入消歧提醒
"""

import argparse
import json
import re
import sys
import textwrap
from typing import Optional


# ---------------------------------------------------------------------------
# 模糊检测规则
# ---------------------------------------------------------------------------

# 多义动词 — 需要上下文才能确定具体操作
AMBIGUOUS_VERBS = [
    "改一下", "改下", "改改",
    "处理一下", "处理下", "处理处理",
    "优化", "优化一下",
    "弄一下", "弄弄",
    "调整一下", "调整下",
    "修一下", "修修",
    "搞一下", "搞搞",
    "整一下", "整整",
    "设置一下",
    "配置一下",
]

# 模糊指代 — "那个"、"这个" 等
VAGUE_REFERENCES = [
    "那个文件", "这个文件", "那个目录", "这个目录",
    "那个", "这个", "那玩意儿", "这玩意儿",
    "上面那个", "前面那个",
    "刚才那个", "之前那个",
]

# 多选一触发模式
MULTI_CHOICE_PATTERNS = [
    r"(?:(?:是|要|想).{0,5}(?:A|B|C|D|1|2|3|4)\b.{0,20}(?:还是|或者|或))",
    r"(?:.{0,5}(?:方案|选项|方式|方法).{0,10}(?:一|二|三|1|2|3))",
]

# 命令型模糊 — 只有动词没有宾语
CMD_VAGUE_PATTERNS = [
    r"^(?:运行|执行|启动|打开)\s*$",
    r"^(?:删除|移除|清理)\s*$",
    r"^(?:创建|新建|添加)\s*$",
]


def detect_ambiguity(command: str) -> dict:
    """
    检测命令是否模糊，返回分析结果。

    检测维度：
    1. 多义动词
    2. 模糊指代
    3. 多选一场景
    4. 缺少宾语
    """
    command_stripped = command.strip()
    issues: list[dict] = []

    # ---- 1. 多义动词 ----
    for verb in AMBIGUOUS_VERBS:
        if verb in command_stripped:
            issues.append({
                "type": "ambiguous_verb",
                "matched": verb,
                "detail": f"动词 '{verb}' 含义模糊，可能表示修改、删除、重命名、调整参数等多种操作",
            })
            break  # 只记录第一个匹配

    # ---- 2. 模糊指代 ----
    for ref in VAGUE_REFERENCES:
        if ref in command_stripped:
            issues.append({
                "type": "vague_reference",
                "matched": ref,
                "detail": f"'{ref}' 指代不明，缺少具体路径或名称",
            })
            break

    # ---- 3. 多选一场景 ----
    for pattern in MULTI_CHOICE_PATTERNS:
        match = re.search(pattern, command_stripped)
        if match:
            issues.append({
                "type": "multiple_choice",
                "matched": match.group(),
                "detail": "存在多个可能选项但未明确选择",
            })
            break

    # ---- 4. 缺少宾语 ----
    for pattern in CMD_VAGUE_PATTERNS:
        match = re.search(pattern, command_stripped)
        if match:
            issues.append({
                "type": "missing_object",
                "matched": match.group(),
                "detail": "命令缺少操作对象（文件/路径/目标）",
            })
            break

    is_ambiguous = len(issues) > 0
    confidence = (
        "high" if len(issues) >= 2
        else "medium" if len(issues) == 1
        else "low"
    )

    return {
        "is_ambiguous": is_ambiguous,
        "confidence": confidence,
        "issues": issues,
        "command": command,
    }


def cmd_detect(command: str) -> None:
    """检测命令是否模糊。"""
    result = detect_ambiguity(command)
    result["action"] = "detect"
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 澄清问题生成
# ---------------------------------------------------------------------------

def generate_clarification_question(command: str, issues: list[dict]) -> str:
    """根据检测到的问题生成精准澄清问题。"""
    if not issues:
        return f"请确认：你说「{command}」具体是指什么？"

    first = issues[0]

    if first["type"] == "ambiguous_verb":
        verb = first["matched"]
        # 提取动词后的宾语
        after_verb = command.split(verb)[-1].strip() if verb in command else command
        return (
            f"你说「{verb}{after_verb}」，具体是想：\n"
            f"(1) 修改 {after_verb} 的内容\n"
            f"(2) 重命名 {after_verb}\n"
            f"(3) 删除 {after_verb}\n"
            f"(4) 调整 {after_verb} 的配置/参数？"
        )

    if first["type"] == "vague_reference":
        return f"「{first['matched']}」指的是哪个具体的文件或目录？请提供路径。"

    if first["type"] == "multiple_choice":
        return (
            f"你的指令包含多个可能选项（{first['matched']}）。"
            f"请明确选择其中一个方案。"
        )

    if first["type"] == "missing_object":
        return f"你要「{first['matched'].strip()}」什么？请指定具体的文件、目录或目标。"

    return f"请进一步说明你的意图。"


def cmd_question(command: str) -> None:
    """生成精准澄清问题。"""
    detection = detect_ambiguity(command)

    if not detection["is_ambiguous"]:
        result = {
            "action": "question",
            "is_ambiguous": False,
            "question": None,
            "message": "该命令足够清晰，无需澄清",
            "command": command,
        }
    else:
        question = generate_clarification_question(command, detection["issues"])
        result = {
            "action": "question",
            "is_ambiguous": True,
            "question": question,
            "issues": detection["issues"],
            "command": command,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))


INJECTION_TEXT = textwrap.dedent("""\
## 🔍 意图消歧
遇到模糊指令时，不要猜测，用 1 个精准问题澄清：
- 多义动词：「改一下X」→ 问具体操作（修改/删除/重命名？）
- 模糊指代：「那个文件」→ 问具体路径
- 多项选择：列出选项，让用户选一个
- 缺少宾语：追问操作目标""")


def cmd_inject() -> None:
    """注入消歧提醒。"""
    result = {
        "action": "inject",
        "injection": INJECTION_TEXT.strip(),
        "usage": "将以上内容追加到系统 prompt 中以启用意图消歧能力",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="意图消歧工具 — 模糊指令不猜测，用最小代价澄清",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s detect --command "改一下那个文件"
  %(prog)s question --command "优化一下"
  %(prog)s inject
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    detect_parser = sub.add_parser("detect", help="检测命令是否模糊")
    detect_parser.add_argument(
        "--command",
        required=True,
        help="要检测的命令文本",
    )

    question_parser = sub.add_parser("question", help="生成精准澄清问题")
    question_parser.add_argument(
        "--command",
        required=True,
        help="模糊命令文本",
    )

    sub.add_parser("inject", help="生成消歧提醒注入文本")

    args = parser.parse_args()

    if args.command == "detect":
        cmd_detect(args.command)
    elif args.command == "question":
        cmd_question(args.command)
    elif args.command == "inject":
        cmd_inject()


if __name__ == "__main__":
    main()