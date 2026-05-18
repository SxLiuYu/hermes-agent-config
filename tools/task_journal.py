#!/usr/bin/env python3
"""
P0-16: Agent Task Journal — AutoSkill/EvolveR-style self-evolution

对标:
  - AutoSkill (华东师范大学/上海AI Lab, 2026): Agent 通过反思自动生成新技能
  - EvolveR (arxiv 2603.01145): Agent 在交互驱动下持续改进策略
  - DSPy MIPROv2: 多阶段程序的指令+示例联合优化

核心循环:
  记录任务 → 评分结果 → 识别模式 → 提取经验 → 建议改进

三大功能:
  1. 任务日志 (journal): 记录每次任务的目标、方法、结果
  2. 模式分析 (analyze): 识别成功/失败模式、高频任务类型
  3. 改进建议 (suggest): 基于历史模式自动生成优化建议
"""

import json
import os
import time
import hashlib
from dataclasses import dataclass, field
from collections import defaultdict, Counter
from typing import Optional
from datetime import datetime

# ─── 数据存储 ────────────────────────────────────────────

HERMES_DIR = os.path.expanduser("~/.hermes")
JOURNAL_DIR = os.path.join(HERMES_DIR, "journal")
os.makedirs(JOURNAL_DIR, exist_ok=True)

JOURNAL_FILE = os.path.join(JOURNAL_DIR, "task_log.jsonl")
INSIGHTS_FILE = os.path.join(JOURNAL_DIR, "insights.json")
STATS_FILE = os.path.join(JOURNAL_DIR, "stats.json")


# ─── 任务日志 ────────────────────────────────────────────


@dataclass
class TaskEntry:
    """单次任务记录"""
    task_id: str
    timestamp: float
    session_id: str = ""
    goal: str = ""                    # 任务目标
    approach: str = ""                # 采用的方法
    tools_used: list[str] = field(default_factory=list)  # 使用的工具
    outcome: str = ""                 # "success", "partial", "failure", "interrupted"
    score: float = 0.0               # 0-10 评分
    duration_seconds: float = 0.0    # 耗时
    errors: list[str] = field(default_factory=list)  # 遇到的错误
    fixes: list[str] = field(default_factory=list)    # 修复方法
    lessons: list[str] = field(default_factory=list)  # 经验教训
    tags: list[str] = field(default_factory=list)     # 自动标签
    iteration_count: int = 0         # 尝试次数
    parent_task_id: str = ""         # 上游任务（如由哪个任务触发）


def generate_task_id() -> str:
    """生成任务 ID"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rand = hashlib.md5(str(time.time()).encode()).hexdigest()[:6]
    return f"task_{ts}_{rand}"


def _auto_tag(goal: str) -> list[str]:
    """从任务目标自动提取标签"""
    tags = []
    tag_patterns = {
        "fix": ["修复", "fix", "bug", "错误", "debug"],
        "feature": ["功能", "feature", "添加", "新增", "实现", "开发"],
        "refactor": ["重构", "refactor", "优化", "改进"],
        "deploy": ["部署", "deploy", "发布", "上线"],
        "research": ["研究", "调研", "搜索", "查找", "search"],
        "code_review": ["review", "审查", "检查代码"],
        "test": ["测试", "test", "验证"],
        "data": ["数据", "data", "分析", "统计"],
        "config": ["配置", "config", "设置", "安装"],
        "doc": ["文档", "doc", "写文章", "记录"],
        "agent_task": ["agent", "子任务", "subagent", "派发"],
        "context": ["上下文", "context", "压缩", "compression"],
    }
    goal_lower = goal.lower()
    for tag, patterns in tag_patterns.items():
        if any(p.lower() in goal_lower for p in patterns):
            tags.append(tag)
    return tags or ["general"]


def log_task(goal: str, approach: str = "",
             tools_used: list[str] = None,
             outcome: str = "unknown",
             score: float = 0.0,
             duration_seconds: float = 0.0,
             errors: list[str] = None,
             fixes: list[str] = None,
             lessons: list[str] = None,
             session_id: str = "",
             iteration_count: int = 1,
             parent_task_id: str = "",
             ) -> str:
    """记录一次任务"""
    task_id = generate_task_id()
    entry = TaskEntry(
        task_id=task_id,
        timestamp=time.time(),
        session_id=session_id,
        goal=goal,
        approach=approach,
        tools_used=tools_used or [],
        outcome=outcome,
        score=score,
        duration_seconds=duration_seconds,
        errors=errors or [],
        fixes=fixes or [],
        lessons=lessons or [],
        tags=_auto_tag(goal),
        iteration_count=iteration_count,
        parent_task_id=parent_task_id,
    )

    # 写入 JSONL
    with open(JOURNAL_FILE, "a") as f:
        f.write(json.dumps(_serialize_entry(entry), ensure_ascii=False) + "\n")

    return task_id


def _serialize_entry(entry: TaskEntry) -> dict:
    return {
        "task_id": entry.task_id,
        "timestamp": entry.timestamp,
        "session_id": entry.session_id,
        "goal": entry.goal,
        "approach": entry.approach,
        "tools_used": entry.tools_used,
        "outcome": entry.outcome,
        "score": entry.score,
        "duration_seconds": entry.duration_seconds,
        "errors": entry.errors,
        "fixes": entry.fixes,
        "lessons": entry.lessons,
        "tags": entry.tags,
        "iteration_count": entry.iteration_count,
        "parent_task_id": entry.parent_task_id,
    }


def load_all_entries() -> list[dict]:
    """加载所有任务日志"""
    if not os.path.exists(JOURNAL_FILE):
        return []
    entries = []
    with open(JOURNAL_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def load_recent_entries(limit: int = 50) -> list[dict]:
    """加载最近的 N 条任务"""
    entries = load_all_entries()
    entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return entries[:limit]


# ─── 模式分析 ────────────────────────────────────────────


def analyze_success_patterns(entries: list[dict]) -> dict:
    """
    分析成功模式:
    哪些工具组合/方法/标签最常成功？
    """
    success_entries = [e for e in entries if e.get("outcome") == "success"]
    fail_entries = [e for e in entries if e.get("outcome") == "failure"]

    # 成功标签
    success_tags = Counter()
    fail_tags = Counter()
    for e in success_entries:
        for tag in e.get("tags", []):
            success_tags[tag] += 1
    for e in fail_entries:
        for tag in e.get("tags", []):
            fail_tags[tag] += 1

    # 成功工具组合
    success_tools = Counter()
    for e in success_entries:
        tools = tuple(sorted(e.get("tools_used", [])))
        if tools:
            success_tools[tools] += 1

    # 高成功率的迭代次数
    iteration_scores = defaultdict(list)
    for e in entries:
        n = e.get("iteration_count", 0)
        if n > 0:
            iteration_scores[n].append(e.get("score", 0))

    avg_iterations = {
        k: sum(v) / len(v)
        for k, v in sorted(iteration_scores.items())
        if len(v) >= 3
    }

    return {
        "total_tasks": len(entries),
        "success_rate": len(success_entries) / max(len(entries), 1),
        "avg_score": sum(e.get("score", 0) for e in entries) / max(len(entries), 1),
        "top_success_tags": success_tags.most_common(10),
        "top_fail_tags": fail_tags.most_common(5),
        "top_success_tools": success_tools.most_common(5),
        "avg_score_by_iterations": avg_iterations,
        "success_count": len(success_entries),
        "fail_count": len(fail_entries),
    }


def identify_common_errors(entries: list[dict]) -> list[dict]:
    """识别高频错误模式"""
    error_counts = defaultdict(list)
    for e in entries:
        for err in e.get("errors", []):
            # 归一化错误信息
            normalized = err.split(":")[0].strip()[:80]
            error_counts[normalized].append({
                "full": err,
                "fix": e.get("fixes", [])[0] if e.get("fixes") else "",
                "outcome": e.get("outcome"),
            })

    results = []
    for err, cases in sorted(error_counts.items(),
                              key=lambda x: len(x[1]), reverse=True):
        if len(cases) >= 2:  # 至少出现 2 次
            successful_fixes = [c["fix"] for c in cases
                                if c["outcome"] == "success" and c["fix"]]
            results.append({
                "error_pattern": err,
                "occurrences": len(cases),
                "most_successful_fix": successful_fixes[0] if successful_fixes else "",
            })

    return results[:15]


# ─── 改进建议 ────────────────────────────────────────────


def generate_suggestions(entries: list[dict]) -> list[dict]:
    """
    基于历史模式生成改进建议。
    对标 AutoSkill: 自动识别可以固化为 skill 的经验。

    建议类型:
    - new_skill: 高频成功任务类型 → 建议固化为 skill
    - fix_pattern: 重复出现的错误+修复 → 建议加入 skill 的 pitfalls
    - tool_optimization: 高频失败的工具 → 建议改进 tool schema
    - context_optimization: 需要大量上下文的任务 → 建议优化上下文管理
    """
    if len(entries) < 5:
        return [{"type": "info", "message": "需要至少 5 条任务记录才能生成有意义的建议"}]

    suggestions = []
    patterns = analyze_success_patterns(entries)
    errors = identify_common_errors(entries)

    # 1. 高频成功标签 → 建议固化为 skill
    for tag, count in patterns.get("top_success_tags", [])[:3]:
        if count >= 3:
            suggestions.append({
                "type": "new_skill",
                "priority": "high" if count >= 5 else "medium",
                "tag": tag,
                "count": count,
                "message": f"''{tag}'' 类任务完成了 {count} 次，成功率较高，"
                          f"建议固化为 skill 以标准化流程和减少重复探索",
            })

    # 2. 高频失败标签 → 建议优化
    for tag, count in patterns.get("top_fail_tags", [])[:3]:
        if count >= 2:
            fail_rate = count / max(patterns.get("total_tasks", 1), 1)
            if fail_rate > 0.3:
                suggestions.append({
                    "type": "fix_pattern",
                    "priority": "high",
                    "tag": tag,
                    "count": count,
                    "message": f"''{tag}'' 类任务失败 {count} 次，失败率 {fail_rate:.0%}，"
                              f"建议收集失败案例并改进流程",
                })

    # 3. 高频错误 → 建议加入 pitfalls
    for err in errors[:5]:
        if err.get("most_successful_fix"):
            suggestions.append({
                "type": "fix_pattern",
                "priority": "medium",
                "error": err["error_pattern"],
                "occurrences": err["occurrences"],
                "fix": err["most_successful_fix"],
                "message": f"错误 '{err['error_pattern'][:50]}' 出现 {err['occurrences']} 次，"
                          f"已知修复: {err['most_successful_fix'][:60]}。建议加入相关 skill 的 pitfalls",
            })

    # 4. 整体健康度
    success_rate = patterns.get("success_rate", 0)
    if success_rate < 0.5 and len(entries) >= 10:
        suggestions.append({
            "type": "health_alert",
            "priority": "critical",
            "message": f"近期成功率仅 {success_rate:.0%}，建议检查 Agent 配置、工具可靠性或模型选择",
        })
    elif success_rate > 0.8:
        suggestions.append({
            "type": "health_good",
            "priority": "info",
            "message": f"近期成功率 {success_rate:.0%}，Agent 运行良好",
        })

    return suggestions


# ─── 最佳实践提取 ──────────────────────────────────────────


def extract_best_practices(entries: list[dict]) -> str:
    """
    从成功任务中提取最佳实践摘要，
    可直接注入到 Agent 上下文中。
    """
    success_entries = [e for e in entries
                       if e.get("outcome") == "success" and e.get("score", 0) >= 7]
    if not success_entries:
        return "暂无足够的高分成功案例"

    lines = ["## 提取的最佳实践"]

    # 按标签分组
    by_tag = defaultdict(list)
    for e in success_entries:
        for tag in e.get("tags", []):
            by_tag[tag].append(e)

    for tag, tag_entries in sorted(by_tag.items(), key=lambda x: len(x[1]), reverse=True):
        if len(tag_entries) >= 2:
            # 提取共同的 lessons
            all_lessons = []
            for e in tag_entries[:5]:
                all_lessons.extend(e.get("lessons", []))

            # 去重
            unique_lessons = list(dict.fromkeys(all_lessons))[:3]

            if unique_lessons:
                lines.append(f"\n### {tag} ({len(tag_entries)} 次成功)")
                for lesson in unique_lessons:
                    lines.append(f"- {lesson}")

    return "\n".join(lines)


# ─── 洞察存储 ─────────────────────────────────────────────


def save_insights(entries: list[dict]):
    """保存分析结果到 insights.json"""
    insights = {
        "generated_at": time.time(),
        "generated_at_iso": datetime.now().isoformat(),
        "patterns": analyze_success_patterns(entries),
        "common_errors": identify_common_errors(entries),
        "suggestions": generate_suggestions(entries),
        "total_entries_analyzed": len(entries),
    }

    with open(INSIGHTS_FILE, "w") as f:
        json.dump(insights, f, indent=2, ensure_ascii=False)

    return insights


def load_insights() -> Optional[dict]:
    """加载最近的洞察"""
    if not os.path.exists(INSIGHTS_FILE):
        return None
    with open(INSIGHTS_FILE) as f:
        return json.load(f)


# ─── Hook 接口 ───────────────────────────────────────────


def on_task_complete(goal: str, outcome: str, score: float = 0.0,
                     tools_used: list[str] = None,
                     errors: list[str] = None,
                     fixes: list[str] = None,
                     lessons: list[str] = None,
                     session_id: str = "",
                     duration_seconds: float = 0.0,
                     iteration_count: int = 1) -> str:
    """
    任务完成时调用的 Hook。
    自动记录 + 每 10 条触发一次洞察生成。
    """
    task_id = log_task(
        goal=goal,
        outcome=outcome,
        score=score,
        tools_used=tools_used,
        errors=errors,
        fixes=fixes,
        lessons=lessons,
        session_id=session_id,
        duration_seconds=duration_seconds,
        iteration_count=iteration_count,
    )

    # 每 10 条记录生成一次洞察
    entries = load_all_entries()
    if len(entries) % 10 == 0:
        save_insights(entries)

    return task_id


# ─── CLI ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  task_journal.py log <goal> [outcome] [score] [tools...]")
        print("  task_journal.py list [limit]")
        print("  task_journal.py analyze")
        print("  task_journal.py errors")
        print("  task_journal.py suggest")
        print("  task_journal.py practices")
        print("  task_journal.py stats")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "log":
        goal = sys.argv[2] if len(sys.argv) > 2 else ""
        outcome = sys.argv[3] if len(sys.argv) > 3 else "unknown"
        score = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
        tools = sys.argv[5:] if len(sys.argv) > 5 else []
        task_id = log_task(goal=goal, outcome=outcome, score=score, tools_used=tools)
        print(f"Logged: {task_id}")

    elif cmd == "list":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        entries = load_recent_entries(limit)
        for e in entries:
            ts = datetime.fromtimestamp(e["timestamp"]).strftime("%m-%d %H:%M")
            icon = {"success": "✅", "partial": "⚠️", "failure": "❌", "interrupted": "🔄"}.get(
                e.get("outcome", ""), "❓")
            print(f"{icon} {ts} [{e.get('outcome','?')}] "
                  f"{e['goal'][:60]} "
                  f"(score={e.get('score',0)}, tools={e.get('tools_used',[])})")

    elif cmd == "analyze":
        entries = load_recent_entries(200)
        patterns = analyze_success_patterns(entries)
        print(json.dumps(patterns, indent=2, ensure_ascii=False))

    elif cmd == "errors":
        entries = load_recent_entries(200)
        errors = identify_common_errors(entries)
        for err in errors:
            print(f"x{err['occurrences']} | {err['error_pattern']}")
            if err.get("most_successful_fix"):
                print(f"  Fix: {err['most_successful_fix'][:80]}")

    elif cmd == "suggest":
        entries = load_recent_entries(200)
        suggestions = generate_suggestions(entries)
        for s in suggestions:
            priority_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "info": "🔵"}.get(
                s.get("priority", ""), "⚪")
            print(f"{priority_icon} [{s.get('type','')}] {s.get('message','')}")

    elif cmd == "practices":
        entries = load_recent_entries(200)
        print(extract_best_practices(entries))

    elif cmd == "stats":
        entries = load_all_entries()
        print(f"总任务数: {len(entries)}")
        outcomes = Counter(e.get("outcome") for e in entries)
        for outcome, count in outcomes.most_common():
            print(f"  {outcome}: {count}")
        avg_score = sum(e.get("score", 0) for e in entries) / max(len(entries), 1)
        print(f"平均评分: {avg_score:.1f}/10")

        tags = Counter()
        for e in entries:
            for tag in e.get("tags", []):
                tags[tag] += 1
        print(f"高频任务类型: {dict(tags.most_common(10))}")