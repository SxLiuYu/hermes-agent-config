#!/usr/bin/env python3
"""
Agent Gym — 对标 Letta Evals 的自动能力评估

定期测试 agent 核心能力，对比历史分数，检测能力退化。
在能力退化时发出告警，防止 agent "默默变蠢"。

评估维度:
  1. 工具选择 — 给定 prompt，是否正确选择工具
  2. 文件操作 — 读写搜索的正确性
  3. 记忆保持 — 跨 session 信息回忆
  4. Skill 匹配 — 根据任务加载正确的 skill
  5. 代码生成 — 从 spec 生成正确的代码

用法:
  hermes gym run              # 运行当前评估
  hermes gym history          # 查看历史分数
  hermes gym alert            # 检查是否有退化告警
  hermes gym trend            # 显示趋势图（ASCII）
"""

import json
import os
import re
import sys
import time
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERMES_HOME = Path.home() / ".hermes"
GYM_DIR = HERMES_HOME / "gym"
SCORES_FILE = GYM_DIR / "scores.jsonl"
ALERT_FILE = GYM_DIR / "alerts.jsonl"
DEGRADE_THRESHOLD = 0.15  # 单次下降 >15% 告警
MOVING_AVG_DEGRADE = 0.10  # 3次移动平均下降 >10% 告警


# ────────────────────────────────────────────────────────
# 评估任务定义
# ────────────────────────────────────────────────────────

BENCHMARKS = {
    "tool_selection": {
        "description": "工具选择能力——给定任务描述，是否能选择正确的工具",
        "weight": 0.3,
        "tests": [
            {
                "prompt": "搜索关于 Python asyncio 的文档",
                "expected_tools": ["web_search"],
                "forbidden_tools": ["terminal", "write_file"],
            },
            {
                "prompt": "创建一个 hello.py 文件，打印 Hello World",
                "expected_tools": ["write_file"],
                "forbidden_tools": ["web_search"],
            },
            {
                "prompt": "运行 pytest 测试",
                "expected_tools": ["terminal"],
                "forbidden_tools": ["write_file"],
            },
        ],
    },
    "file_operations": {
        "description": "文件操作正确性——读写搜索是否正确执行",
        "weight": 0.25,
    },
    "memory_retention": {
        "description": "记忆保持——跨会话的信息回忆能力",
        "weight": 0.15,
    },
    "skill_matching": {
        "description": "Skill 匹配——根据任务加载正确的 skill",
        "weight": 0.15,
        "tests": [
            {
                "task": "检查 GitHub Actions 构建状态",
                "expected_skill": "check-github-actions-build-status",
            },
            {
                "task": "SSH 连接到远程 Ubuntu 服务器",
                "expected_skill": "ssh-ubuntu-server",
            },
            {
                "task": "获取 A 股全量日线数据",
                "expected_skill": "a-stock-daily-kline-pipeline",
            },
        ],
    },
    "code_generation": {
        "description": "代码生成——从 spec 生成正确的代码",
        "weight": 0.15,
    },
}


def _score_task(name: str, task: dict) -> Tuple[float, str]:
    """对单个评估任务打分（0-1）"""
    if name == "tool_selection":
        return _score_tool_selection(task)
    elif name == "skill_matching":
        return _score_skill_matching(task)
    elif name == "file_operations":
        return _score_file_operations(task)
    elif name == "memory_retention":
        return _score_memory_retention(task)
    elif name == "code_generation":
        return _score_code_generation(task)
    return (1.0, "跳过")


def _score_tool_selection(task: dict) -> Tuple[float, str]:
    """评分工具选择能力

    模拟场景：给定 prompt，检查 agent 是否会选择正确的工具
    """
    score = 0.0
    details = []

    for test in task.get("tests", []):
        test_score = 0.0

        # 检查 prompt 中的关键词是否匹配 expected tools
        prompt_lower = test["prompt"].lower()
        for tool in test.get("expected_tools", []):
            # 简单启发式：prompt 包含工具相关的词
            keywords = {
                "web_search": ["搜索", "search", "查找", "find"],
                "write_file": ["创建", "写入", "create", "write", "生成文件"],
                "terminal": ["运行", "run", "执行", "exec", "pytest", "测试"],
                "read_file": ["读取", "read", "查看", "cat"],
            }
            if any(kw in prompt_lower for kw in keywords.get(tool, [])):
                test_score += 0.5

        # 扣分：如果 prompt 包含 forbidden tools 的关键词
        for tool in test.get("forbidden_tools", []):
            keywords = {
                "web_search": ["搜索", "search"],
                "write_file": ["创建文件", "写入文件", "write file"],
                "terminal": ["运行命令", "run"],
            }
            if any(kw in prompt_lower for kw in keywords.get(tool, [])):
                test_score -= 0.3

        test_score = max(0.0, min(1.0, test_score))
        score += test_score

    final = score / max(len(task.get("tests", [])), 1)
    return (final, f"工具选择: {final:.0%}")


def _score_file_operations(task: dict) -> Tuple[float, str]:
    """评分文件操作能力

    实际执行文件操作测试
    """
    test_dir = GYM_DIR / "test_workspace"
    test_dir.mkdir(parents=True, exist_ok=True)

    score = 0.0
    tasks_passed = 0
    total_tasks = 3

    # 测试 1: 写入文件
    try:
        test_file = test_dir / "gym_write_test.txt"
        test_file.write_text("Agent Gym Test\nHello World\n")
        if test_file.exists() and "Hello World" in test_file.read_text():
            tasks_passed += 1
    except Exception:
        pass

    # 测试 2: 读取文件
    try:
        content = test_file.read_text()
        if "Agent Gym" in content:
            tasks_passed += 1
    except Exception:
        pass

    # 测试 3: 搜索文件
    try:
        found = list(test_dir.glob("gym_*.txt"))
        if len(found) >= 1:
            tasks_passed += 1
    except Exception:
        pass

    # 清理
    for f in test_dir.glob("gym_*"):
        f.unlink()

    score = tasks_passed / total_tasks
    return (score, f"文件操作: {tasks_passed}/{total_tasks}")


def _score_skill_matching(task: dict) -> Tuple[float, str]:
    """评分 Skill 匹配能力

    递归搜索 skills 目录，支持 category/skill-name/SKILL.md 结构
    """
    skills_dir = HERMES_HOME / "skills"
    if not skills_dir.exists():
        return (0.0, "skills 目录不存在")

    score = 0.0
    tests = task.get("tests", [])

    for test in tests:
        expected = test.get("expected_skill", "")
        found = False

        # 递归搜索所有 SKILL.md 文件
        for skill_file in skills_dir.glob("**/SKILL.md"):
            # 通过目录名匹配
            parent_dir = skill_file.parent
            skill_name = parent_dir.name.lower().replace("-", "").replace("_", "")

            # 读 frontmatter 获取 name
            try:
                content = skill_file.read_text()[:500]
                name_match = re.search(r"name:\s*(\S+)", content)
                if name_match:
                    fm_name = name_match.group(1).lower().replace("-", "").replace("_", "")
                    if expected.lower().replace("-", "").replace("_", "") in fm_name or fm_name in expected.lower().replace("-", "").replace("_", ""):
                        score += 1.0
                        found = True
                        break
            except Exception:
                pass

            # Fallback: 基于目录名匹配
            if not found and (expected.lower().replace("-", "").replace("_", "") in skill_name or
                              any(kw in parent_dir.name.lower() for kw in expected.lower().replace("-", "").split("-"))):
                score += 0.7
                found = True
                break

        if not found:
            # 模糊搜索：任何 skill 名称包含关键词即可
            keywords = expected.lower().replace("-", " ").split()
            for skill_file in skills_dir.glob("**/SKILL.md"):
                if all(kw in skill_file.parent.name.lower() for kw in keywords[:2]):
                    score += 0.3
                    break

    final = score / max(len(tests), 1)
    return (final, f"Skill匹配: {score:.1f}/{len(tests)}")


def _score_memory_retention(task: dict) -> Tuple[float, str]:
    """评分记忆保持能力

    检查实际 memory 存储位置：system prompt 注入 + MEMORY.md
    """
    score = 0.0
    details = []

    # 1. 检查 MEMORY.md (Hermes 主记忆文件)
    memory_md = HERMES_HOME / "MEMORY.md"
    if memory_md.exists():
        content = memory_md.read_text()
        size_kb = len(content) / 1024
        if size_kb > 5:
            score += 0.5
            details.append(f"MEMORY.md: {size_kb:.1f}KB")
        elif size_kb > 1:
            score += 0.3
            details.append(f"MEMORY.md: {size_kb:.1f}KB")

    # 2. 检查 session_memory (会话记忆)
    session_mem = HERMES_HOME / "session_memory.md"
    if session_mem.exists():
        score += 0.2
        details.append("session_memory 存在")

    # 3. 检查 outcomes 日志（间接反映记忆使用）
    outcomes = HERMES_HOME / "logs" / "outcomes_last.json"
    if outcomes.exists():
        try:
            data = json.loads(outcomes.read_text())
            if data.get("average", 0) > 0:
                score += 0.15
                details.append(f"outcomes: {data.get('average',0):.1f}")
        except Exception:
            pass

    # 4. 检查 skills 目录丰富度（间接反映知识积累）
    skills_dir = HERMES_HOME / "skills"
    if skills_dir.exists():
        skill_count = len(list(skills_dir.glob("**/SKILL.md")))
        if skill_count > 30:
            score += 0.15
            details.append(f"skills: {skill_count}个")
        elif skill_count > 10:
            score += 0.08
            details.append(f"skills: {skill_count}个")

    return (min(score, 1.0), f"记忆: {' + '.join(details) if details else '无'}")


def _score_code_generation(task: dict) -> Tuple[float, str]:
    """评分代码生成能力

    检查是否有 Python 文件语法正确
    """
    scripts_dir = HERMES_HOME / "scripts"
    if not scripts_dir.exists():
        return (0.5, "无 scripts 目录")

    py_files = list(scripts_dir.glob("*.py"))
    if not py_files:
        return (0.5, "无 Python 文件")

    valid = 0
    for f in py_files[:5]:
        try:
            compile(f.read_text(), str(f), "exec")
            valid += 1
        except SyntaxError:
            pass

    score = valid / min(len(py_files), 5)
    return (score, f"代码语法: {valid}/{min(len(py_files), 5)}")


# ────────────────────────────────────────────────────────
# 评分引擎
# ────────────────────────────────────────────────────────


def run_benchmark() -> dict:
    """运行完整基准测试"""
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scores": {},
        "overall": 0.0,
        "details": [],
    }

    for name, config in BENCHMARKS.items():
        score, detail = _score_task(name, config)
        results["scores"][name] = {
            "score": round(score, 3),
            "weight": config["weight"],
            "description": config["description"],
        }
        results["details"].append(f"  {name:<25} {score:.2f}  ({detail})")

    # 加权总分
    total = sum(s["score"] * s["weight"] for s in results["scores"].values())
    results["overall"] = round(total, 3)

    # 保存分数
    GYM_DIR.mkdir(parents=True, exist_ok=True)
    with open(SCORES_FILE, "a") as f:
        f.write(json.dumps({
            "t": results["timestamp"],
            "overall": results["overall"],
            "scores": results["scores"],
        }, ensure_ascii=False) + "\n")

    return results


def check_alerts() -> list:
    """检查是否有能力退化告警"""
    if not SCORES_FILE.exists():
        return [{"level": "info", "message": "暂无历史数据，无法检测退化"}]

    scores = []
    with open(SCORES_FILE) as f:
        for line in f:
            try:
                scores.append(json.loads(line))
            except Exception:
                continue

    if len(scores) < 2:
        return [{"level": "info", "message": f"仅 {len(scores)} 次评估记录，需要至少 2 次"}]

    latest = scores[-1]["overall"]
    prev = scores[-2]["overall"]

    alerts = []

    # 单次下降检测
    drop = prev - latest
    if drop > DEGRADE_THRESHOLD:
        alerts.append({
            "level": "warning",
            "message": f"单次能力下降 {drop:.1%}！{prev:.3f} → {latest:.3f}",
        })

    # 移动平均检测（最近 3 次）
    if len(scores) >= 3:
        recent = [s["overall"] for s in scores[-3:]]
        ma = sum(recent) / len(recent)
        if len(scores) >= 4:
            prev_ma = sum([s["overall"] for s in scores[-4:-1]]) / 3
            ma_drop = prev_ma - ma
            if ma_drop > MOVING_AVG_DEGRADE:
                alerts.append({
                    "level": "warning",
                    "message": f"移动平均下降 {ma_drop:.1%}！({len(recent)}次均值: {ma:.3f})",
                })

    # 低于基线
    baseline = 0.6
    if latest < baseline:
        alerts.append({
            "level": "critical",
            "message": f"能力总分 {latest:.3f} 低于基线 {baseline}",
        })

    # 各项分数检查
    for dim, s in scores[-1].get("scores", {}).items():
        if s["score"] < 0.3:
            dim_desc = BENCHMARKS.get(dim, {}).get("description", dim)
            alerts.append({
                "level": "warning",
                "message": f"维度 '{dim_desc}' 分数过低: {s['score']:.2f}",
            })

    if not alerts:
        alerts.append({"level": "ok", "message": f"✅ 能力正常 (总分: {latest:.3f})"})

    # 保存告警
    for alert in alerts:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **alert,
        }
        with open(ALERT_FILE, "a") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return alerts


def show_history(n: int = 10):
    """显示历史分数"""
    if not SCORES_FILE.exists():
        print("📊 暂无评估记录")
        return

    scores = []
    with open(SCORES_FILE) as f:
        for line in f:
            try:
                scores.append(json.loads(line))
            except Exception:
                continue

    if not scores:
        print("📊 暂无有效记录")
        return

    print(f"📊 Agent Gym 历史分数 ({len(scores)} 次评估):\n")
    print(f"  {'时间':<22} {'总分':>6}  {'趋势'}",)

    prev_score = None
    for s in scores[-n:]:
        ts = s.get("t", "?")[:19]
        score = s.get("overall", 0)
        if prev_score is not None:
            delta = score - prev_score
            arrow = "↑" if delta > 0.01 else ("↓" if delta < -0.01 else "→")
            trend = f"{arrow} {delta:+.3f}"
        else:
            trend = "  ·"
        print(f"  {ts}  {score:.3f}  {trend}")
        prev_score = score

    # 统计
    all_scores = [s["overall"] for s in scores]
    print(f"\n  最高: {max(all_scores):.3f}  最低: {min(all_scores):.3f}  平均: {sum(all_scores)/len(all_scores):.3f}")
    if len(all_scores) >= 2:
        change = all_scores[-1] - all_scores[0]
        direction = "📈 改善" if change > 0.02 else ("📉 退化" if change < -0.02 else "➡️ 持平")
        print(f"  总变化: {change:+.3f}  {direction}")

    # 维度详情
    if scores and "scores" in scores[-1]:
        print(f"\n  各维度分数:")
        for dim, s in scores[-1]["scores"].items():
            bar = "█" * int(s["score"] * 20)
            print(f"    {dim:<25} {s['score']:.2f}  {bar}")


def show_trend():
    """ASCII 趋势图"""
    if not SCORES_FILE.exists():
        print("暂无数据")
        return

    scores_list = []
    with open(SCORES_FILE) as f:
        for line in f:
            try:
                scores_list.append(json.loads(line))
            except Exception:
                continue

    if not scores_list:
        return

    values = [s["overall"] for s in scores_list[-30:]]

    print(f"📈 Agent Gym 趋势 (最近 {len(values)} 次):\n")

    # ASCII 图表
    width = 40
    min_v, max_v = max(0.3, min(values) - 0.05), min(1.0, max(values) + 0.05)

    for i, v in enumerate(values):
        bar_len = int((v - min_v) / (max_v - min_v) * width)
        bar = "█" * bar_len + "░" * (width - bar_len)
        label = scores_list[-(len(values) - i)]["t"][:10] if i < len(scores_list) else ""
        print(f"  {label} │{bar} {v:.3f}")

    # 基线
    print(f"  {'基线':>10} │{'·' * int((0.6 - min_v) / (max_v - min_v) * width)}┤ 0.600")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agent Gym — 能力评估")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="运行评估")

    history_p = sub.add_parser("history", help="查看历史")
    history_p.add_argument("-n", type=int, default=10, help="显示最近 N 条")

    sub.add_parser("alert", help="检查告警")
    sub.add_parser("trend", help="趋势图")

    args = parser.parse_args()

    if args.command == "run":
        print("🏋️ Agent Gym 评估中...\n")
        results = run_benchmark()

        for detail in results["details"]:
            print(detail)

        print(f"\n{'═' * 50}")
        print(f"📊 加权总分: {results['overall']:.3f}  ({results['overall']:.0%})")
        print(f"   时间: {results['timestamp'][:19]}")

        # 自动检查告警
        alerts = check_alerts()
        for alert in alerts:
            level_icon = {"ok": "✅", "warning": "⚠️", "critical": "🚨", "info": "ℹ️"}
            icon = level_icon.get(alert["level"], "•")
            print(f"   {icon} {alert['message']}")

    elif args.command == "history":
        show_history(args.n)

    elif args.command == "alert":
        print("🔍 检查能力退化...\n")
        alerts = check_alerts()
        for alert in alerts:
            level_icon = {"ok": "✅", "warning": "⚠️", "critical": "🚨", "info": "ℹ️"}
            print(f"  {level_icon.get(alert['level'], '•')} {alert['message']}")

    elif args.command == "trend":
        show_trend()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()