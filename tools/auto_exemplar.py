#!/usr/bin/env python3
"""
P0-18: Auto Exemplar Generator — DSPy Bootstrap-inspired

对标:
  - DSPy Bootstrap: 从端到端标注数据自动生成中间步骤的 few-shot 示例
  - MIPROv2 exemplar selection: 贝叶斯搜索最优示例组合

核心思想:
  从任务日志中自动挖掘高质量 (input → output) 对，
  作为 few-shot 示例注入 Agent 上下文。

三步:
  1. 挖掘: 从 task_journal 中找高评分成功任务
  2. 清洗: 去噪、去重、格式标准化
  3. 组合: 多样性优先 + 贝叶斯选择最佳组合
"""

import json
import os
import re
import time
import hashlib
from dataclasses import dataclass, field
from collections import Counter, defaultdict
from typing import Optional

from task_journal import load_all_entries, load_recent_entries

# ─── 示例挖掘 ────────────────────────────────────────────


@dataclass
class Exemplar:
    """一个 few-shot 示例"""
    id: str
    input_text: str       # 用户输入/任务描述
    output_text: str      # Agent 输出/解决方案
    task_type: str        # 任务类型标签
    score: float          # 来源任务的评分
    tools_used: list[str] # 使用的工具
    timestamp: float
    source_task_id: str


def mine_exemplars(
    min_score: float = 7.0,
    min_input_length: int = 10,
    max_input_length: int = 500,
    max_exemplars: int = 50,
) -> list[Exemplar]:
    """
    从任务日志中挖掘高质量示例。

    筛选标准:
    - score >= min_score (默认 7/10)
    - outcome == "success"
    - 输入长度合适
    - 有明确的工具使用记录
    """
    entries = load_all_entries()
    exemplars = []

    for entry in entries:
        if entry.get("outcome") != "success":
            continue
        if entry.get("score", 0) < min_score:
            continue

        goal = entry.get("goal", "").strip()
        if len(goal) < min_input_length or len(goal) > max_input_length:
            continue

        tools = entry.get("tools_used", [])
        if not tools:
            continue

        # 用 errors+fixes 或 lessons 作为 output
        output_parts = []
        if entry.get("fixes"):
            output_parts.append("修复方法: " + "; ".join(entry["fixes"]))
        if entry.get("lessons"):
            output_parts.append("经验: " + "; ".join(entry["lessons"]))
        if entry.get("approach"):
            output_parts.append("方法: " + entry["approach"])

        output = "\n".join(output_parts) if output_parts else f"使用工具 {', '.join(tools)} 完成任务"

        tags = entry.get("tags", [])
        task_type = tags[0] if tags else "general"

        exemplars.append(Exemplar(
            id=entry.get("task_id", hashlib.md5(goal.encode()).hexdigest()[:8]),
            input_text=goal,
            output_text=output,
            task_type=task_type,
            score=entry.get("score", 0),
            tools_used=tools,
            timestamp=entry.get("timestamp", 0),
            source_task_id=entry.get("task_id", ""),
        ))

    # 按评分排序
    exemplars.sort(key=lambda e: e.score, reverse=True)
    return exemplars[:max_exemplars]


def deduplicate_exemplars(exemplars: list[Exemplar]) -> list[Exemplar]:
    """
    去重：移除输入高度相似的重复示例。
    使用 Jaccard 相似度（基于字符 2-gram）。
    """
    if len(exemplars) <= 1:
        return exemplars

    def _char_bigrams(text: str) -> set:
        text = text.lower()
        return set(text[i : i + 2] for i in range(len(text) - 1))

    result = [exemplars[0]]
    seen_sigs = [_char_bigrams(exemplars[0].input_text)]

    for ex in exemplars[1:]:
        sig = _char_bigrams(ex.input_text)
        is_dup = False
        for seen in seen_sigs:
            if not sig or not seen:
                continue
            intersection = len(sig & seen)
            union = len(sig | seen)
            if union == 0:
                continue
            jaccard = intersection / union
            if jaccard > 0.6:  # 60% 相似就是重复
                is_dup = True
                break

        if not is_dup:
            result.append(ex)
            seen_sigs.append(sig)

    return result


def ensure_diversity(
    exemplars: list[Exemplar], target_count: int = 3
) -> list[Exemplar]:
    """
    确保多样性：每种任务类型至少一个示例，最大化工具组合多样性。
    对标 DSPy 的多样性优先采样。
    """
    if len(exemplars) <= target_count:
        return exemplars

    # 按任务类型分组
    by_type = defaultdict(list)
    for ex in exemplars:
        by_type[ex.task_type].append(ex)

    selected = []
    # 第一轮：每个类型选最好的
    for ttype in sorted(by_type.keys()):
        if len(selected) >= target_count:
            break
        best = max(by_type[ttype], key=lambda e: e.score)
        selected.append(best)
        by_type[ttype].remove(best)

    # 第二轮：如果还不够，按工具多样性补充
    if len(selected) < target_count:
        used_tool_sets = {tuple(sorted(e.tools_used)) for e in selected}
        remaining = []
        for exs in by_type.values():
            remaining.extend(exs)
        remaining.sort(key=lambda e: e.score, reverse=True)

        for ex in remaining:
            if len(selected) >= target_count:
                break
            tool_set = tuple(sorted(ex.tools_used))
            if tool_set not in used_tool_sets:
                selected.append(ex)
                used_tool_sets.add(tool_set)

    return selected[:target_count]


# ─── 格式化 ───────────────────────────────────────────────


def format_exemplar(ex: Exemplar, style: str = "markdown") -> str:
    """
    格式化一个示例。

    style:
      - "markdown": Markdown 格式，适合注入 Agent 上下文
      - "json": JSON 格式，适合程序化处理
      - "compact": 紧凑文本格式
    """
    if style == "markdown":
        lines = [
            f"**示例**: {ex.input_text[:120]}",
            f"→ 方法: {ex.output_text[:150]}",
            f"(标签: {ex.task_type}, 评分: {ex.score:.0f}/10, 工具: {', '.join(ex.tools_used[:4])})",
        ]
        return "\n".join(lines)

    elif style == "json":
        return json.dumps({
            "input": ex.input_text,
            "output": ex.output_text,
            "task_type": ex.task_type,
            "score": ex.score,
            "tools": ex.tools_used,
        }, ensure_ascii=False)

    elif style == "compact":
        return (
            f"Q: {ex.input_text[:80]}"
            f" → A: {ex.output_text[:80]}"
            f" [{ex.task_type}]"
        )

    return str(ex)


def format_exemplars_batch(
    exemplars: list[Exemplar],
    style: str = "markdown",
    max_examples: int = 5,
) -> str:
    """批量格式化多个示例"""
    formatted = []
    for i, ex in enumerate(exemplars[:max_examples]):
        formatted.append(f"{i + 1}. {format_exemplar(ex, style)}")
    return "\n\n".join(formatted)


# ─── 上下文注入 ───────────────────────────────────────────


def generate_few_shot_block(
    task_type: str = "",
    max_examples: int = 3,
    min_score: float = 7.0,
) -> str:
    """
    生成可注入 Agent 上下文的 few-shot 示例块。

    Args:
        task_type: 过滤特定任务类型（空字符串=所有类型）
        max_examples: 最大示例数
        min_score: 最低评分

    Returns: Markdown 格式的 few-shot 示例块
    """
    exemplars = mine_exemplars(min_score=min_score)
    exemplars = deduplicate_exemplars(exemplars)

    if task_type:
        exemplars = [e for e in exemplars if e.task_type == task_type]

    exemplars = ensure_diversity(exemplars, target_count=max_examples)

    if not exemplars:
        return "暂无足够的高质量示例"

    lines = [
        "## Few-Shot 示例（从历史成功任务中提取）",
        "",
        "以下示例展示了类似任务的成功处理方式：",
        "",
    ]

    for i, ex in enumerate(exemplars[:max_examples]):
        lines.append(f"### 示例 {i + 1}")
        lines.append(f"**问题**: {ex.input_text}")
        lines.append(f"**解决方案**: {ex.output_text}")
        lines.append(
            f"**使用工具**: {', '.join(ex.tools_used[:5])} "
            f"| 评分: {ex.score:.0f}/10"
        )
        lines.append("")

    return "\n".join(lines)


# ─── 示例库管理 ───────────────────────────────────────────


EXEMPLAR_CACHE = os.path.expanduser("~/.hermes/journal/exemplars_cache.json")


def cache_exemplars():
    """缓存当前最佳示例"""
    exemplars = mine_exemplars(min_score=7.0, max_exemplars=50)
    exemplars = deduplicate_exemplars(exemplars)

    os.makedirs(os.path.dirname(EXEMPLAR_CACHE), exist_ok=True)

    data = []
    for ex in exemplars:
        data.append({
            "id": ex.id,
            "input": ex.input_text,
            "output": ex.output_text,
            "task_type": ex.task_type,
            "score": ex.score,
            "tools": ex.tools_used,
            "timestamp": ex.timestamp,
        })

    with open(EXEMPLAR_CACHE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return len(data)


def load_cached_exemplars() -> list[Exemplar]:
    """加载缓存的示例"""
    if not os.path.exists(EXEMPLAR_CACHE):
        return []

    with open(EXEMPLAR_CACHE) as f:
        data = json.load(f)

    return [
        Exemplar(
            id=d["id"],
            input_text=d["input"],
            output_text=d["output"],
            task_type=d["task_type"],
            score=d["score"],
            tools_used=d["tools"],
            timestamp=d["timestamp"],
            source_task_id="",
        )
        for d in data
    ]


# ─── CLI ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  auto_exemplar.py mine [min_score] [max]")
        print("  auto_exemplar.py show [task_type] [count]")
        print("  auto_exemplar.py few-shot [task_type]")
        print("  auto_exemplar.py cache")
        print("  auto_exemplar.py stats")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "mine":
        min_score = float(sys.argv[2]) if len(sys.argv) > 2 else 7.0
        max_n = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        exemplars = mine_exemplars(min_score=min_score, max_exemplars=max_n)
        exemplars = deduplicate_exemplars(exemplars)
        print(f"Found {len(exemplars)} unique high-quality exemplars:\n")
        for ex in exemplars:
            print(format_exemplar(ex, "compact"))

    elif cmd == "show":
        task_type = sys.argv[2] if len(sys.argv) > 2 else ""
        count = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        exemplars = mine_exemplars(max_exemplars=100)
        exemplars = deduplicate_exemplars(exemplars)
        if task_type:
            exemplars = [e for e in exemplars if e.task_type == task_type]
        exemplars = ensure_diversity(exemplars, count)
        for ex in exemplars:
            print(format_exemplar(ex, "markdown"))
            print()

    elif cmd == "few-shot":
        task_type = sys.argv[2] if len(sys.argv) > 2 else ""
        print(generate_few_shot_block(task_type=task_type))

    elif cmd == "cache":
        count = cache_exemplars()
        print(f"Cached {count} exemplars to {EXEMPLAR_CACHE}")

    elif cmd == "stats":
        exemplars = mine_exemplars(max_exemplars=500)
        print(f"Total exemplars: {len(exemplars)}")
        print(f"\nBy task type:")
        by_type = Counter(e.task_type for e in exemplars)
        for ttype, count in by_type.most_common(10):
            avg_score = sum(e.score for e in exemplars if e.task_type == ttype) / max(count, 1)
            print(f"  {ttype}: {count} (avg score: {avg_score:.1f})")

        print(f"\nBy tool:")
        by_tool = Counter()
        for e in exemplars:
            for t in e.tools_used:
                by_tool[t] += 1
        for tool, count in by_tool.most_common(10):
            print(f"  {tool}: {count}")