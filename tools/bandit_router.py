#!/usr/bin/env python3
"""
Bandit Tool Router — 基于 Thompson Sampling 的自适应工具路由器
对标 Contextual Bandit（上下文老虎机）

核心思想：ML 自适应学习——哪种工具对哪种任务类型最有效。
用 Thompson Sampling 平衡探索（exploration）和利用（exploitation）。

CLI 用法：
  python3 bandit_router.py suggest --task-type "debug"
  python3 bandit_router.py record --task-type "debug" --tool "terminal" --success
  python3 bandit_router.py stats
  python3 bandit_router.py inject
  python3 bandit_router.py reset --task-type "debug" --tool "terminal"
  python3 bandit_router.py reset-all
"""

import argparse
import json
import os
import sys
import random
from collections import defaultdict

# ── 路径配置 ──────────────────────────────────────────────
HERMES_DIR = os.path.expanduser("~/.hermes")
BANDIT_DIR = os.path.join(HERMES_DIR, "bandit")
STATE_FILE = os.path.join(BANDIT_DIR, "bandit_state.json")

# ── 任务类型 & 工具 ────────────────────────────────────────
TASK_TYPES = ["debug", "feature", "refactor", "config", "optimize", "data", "research", "review"]
TOOLS = ["terminal", "file", "search", "delegate", "browser"]

# 冷启动探索次数（全局累计，达到前均匀随机选择）
COLD_START_EXPLORE = 10


# ── 工具函数 ──────────────────────────────────────────────

def ensure_dirs():
    """确保目录存在"""
    os.makedirs(BANDIT_DIR, exist_ok=True)


def load_state():
    """加载 bandit 状态"""
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return _default_state()


def save_state(state):
    """保存 bandit 状态"""
    ensure_dirs()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _default_state():
    """返回默认初始状态"""
    state = {
        "total_records": 0,
        "arms": {}   # key: "task_type::tool"
    }
    for tt in TASK_TYPES:
        for tool in TOOLS:
            key = f"{tt}::{tool}"
            state["arms"][key] = {"success": 0, "total": 0, "task_type": tt, "tool": tool}
    return state


def ensure_arms(state):
    """确保所有 task_type/tool 组合都在 state 中"""
    for tt in TASK_TYPES:
        for tool in TOOLS:
            key = f"{tt}::{tool}"
            if key not in state.get("arms", {}):
                if "arms" not in state:
                    state["arms"] = {}
                state["arms"][key] = {"success": 0, "total": 0, "task_type": tt, "tool": tool}
    return state


def thompson_sample(state, task_type):
    """
    Thompson Sampling: 为指定 task_type 的每种工具从 Beta 分布采样，
    返回得分最高的工具名。

    Beta(success + 1, total - success + 1) — 无信息先验 Beta(1, 1)
    """
    samples = []
    for tool in TOOLS:
        key = f"{task_type}::{tool}"
        arm = state["arms"].get(key, {"success": 0, "total": 0})
        alpha = arm["success"] + 1
        beta_val = arm["total"] - arm["success"] + 1
        # 从 Beta(alpha, beta) 采样
        sampled = random.betavariate(max(alpha, 0.01), max(beta_val, 0.01))
        samples.append((tool, sampled, arm))

    # 按采样得分降序排列
    samples.sort(key=lambda x: x[1], reverse=True)
    return samples


# ── suggest ───────────────────────────────────────────────

def cmd_suggest(args):
    """建议最佳工具"""
    task_type = args.task_type.lower()

    if task_type not in TASK_TYPES:
        print(json.dumps({
            "status": "error",
            "message": f"未知任务类型: {task_type}",
            "valid_types": TASK_TYPES
        }, ensure_ascii=False))
        sys.exit(1)

    state = load_state()
    state = ensure_arms(state)

    total_records = state.get("total_records", 0)

    # 冷启动：前 N 次均匀随机探索
    if total_records < COLD_START_EXPLORE:
        chosen = random.choice(TOOLS)
        output = {
            "status": "ok",
            "mode": "cold_start",
            "task_type": task_type,
            "suggested_tool": chosen,
            "reason": f"冷启动探索阶段（{total_records}/{COLD_START_EXPLORE}），均匀随机选择",
            "total_records": total_records
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # Thompson Sampling
    samples = thompson_sample(state, task_type)
    chosen_tool, best_score, best_arm = samples[0]

    # 构建排名
    rankings = []
    for tool, score, arm in samples:
        rankings.append({
            "tool": tool,
            "score": round(score, 6),
            "success": arm["success"],
            "total": arm["total"],
            "success_rate": round(arm["success"] / arm["total"], 4) if arm["total"] > 0 else 0.0
        })

    output = {
        "status": "ok",
        "mode": "thompson_sampling",
        "task_type": task_type,
        "suggested_tool": chosen_tool,
        "score": round(best_score, 6),
        "total_records": total_records,
        "arm": {
            "success": best_arm["success"],
            "total": best_arm["total"],
            "success_rate": round(best_arm["success"] / best_arm["total"], 4) if best_arm["total"] > 0 else 0.0
        },
        "rankings": rankings
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── record ────────────────────────────────────────────────

def cmd_record(args):
    """记录工具使用结果"""
    task_type = args.task_type.lower()
    tool = args.tool.lower()

    if task_type not in TASK_TYPES:
        print(json.dumps({
            "status": "error",
            "message": f"未知任务类型: {task_type}",
            "valid_types": TASK_TYPES
        }, ensure_ascii=False))
        sys.exit(1)

    if tool not in TOOLS:
        print(json.dumps({
            "status": "error",
            "message": f"未知工具: {tool}",
            "valid_tools": TOOLS
        }, ensure_ascii=False))
        sys.exit(1)

    state = load_state()
    state = ensure_arms(state)

    key = f"{task_type}::{tool}"
    arm = state["arms"].get(key, {"success": 0, "total": 0, "task_type": task_type, "tool": tool})

    arm["total"] += 1
    if args.success:
        arm["success"] += 1

    state["arms"][key] = arm
    state["total_records"] = state.get("total_records", 0) + 1

    save_state(state)

    new_rate = round(arm["success"] / arm["total"], 4) if arm["total"] > 0 else 0.0

    output = {
        "status": "recorded",
        "task_type": task_type,
        "tool": tool,
        "success": args.success,
        "arm": {
            "success": arm["success"],
            "total": arm["total"],
            "success_rate": new_rate
        },
        "total_records": state["total_records"]
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── stats ─────────────────────────────────────────────────

def cmd_stats(args):
    """显示工具-任务有效性矩阵"""
    state = load_state()
    state = ensure_arms(state)

    total_records = state.get("total_records", 0)

    # 构建矩阵
    matrix = {}
    for tt in TASK_TYPES:
        matrix[tt] = {}
        for tool in TOOLS:
            key = f"{tt}::{tool}"
            arm = state["arms"].get(key, {"success": 0, "total": 0})
            rate = round(arm["success"] / arm["total"], 4) if arm["total"] > 0 else None
            matrix[tt][tool] = {
                "success": arm["success"],
                "total": arm["total"],
                "success_rate": rate
            }

    # 全局工具统计
    tool_stats = defaultdict(lambda: {"success": 0, "total": 0})
    for tt in TASK_TYPES:
        for tool in TOOLS:
            m = matrix[tt][tool]
            tool_stats[tool]["success"] += m["success"]
            tool_stats[tool]["total"] += m["total"]

    tool_summary = {}
    for tool in TOOLS:
        ts = tool_stats[tool]
        tool_summary[tool] = {
            "success": ts["success"],
            "total": ts["total"],
            "success_rate": round(ts["success"] / ts["total"], 4) if ts["total"] > 0 else None
        }

    # 任务类型全局统计
    task_stats = {}
    for tt in TASK_TYPES:
        s = sum(matrix[tt][t]["success"] for t in TOOLS)
        t = sum(matrix[tt][t]["total"] for t in TOOLS)
        task_stats[tt] = {
            "success": s,
            "total": t,
            "success_rate": round(s / t, 4) if t > 0 else None
        }

    # 为每个任务类型推荐最佳工具
    best_tools = {}
    for tt in TASK_TYPES:
        if total_records >= COLD_START_EXPLORE:
            samples = thompson_sample(state, tt)
            best_tool, best_score, _ = samples[0]
            best_tools[tt] = {
                "tool": best_tool,
                "thompson_score": round(best_score, 6)
            }
        else:
            best_tools[tt] = {"tool": None, "reason": "冷启动阶段，无推荐"}

    output = {
        "status": "ok",
        "total_records": total_records,
        "cold_start_threshold": COLD_START_EXPLORE,
        "phase": "cold_start" if total_records < COLD_START_EXPLORE else "thompson_sampling",
        "task_types": TASK_TYPES,
        "tools": TOOLS,
        "matrix": matrix,
        "tool_summary": tool_summary,
        "task_summary": task_stats,
        "best_tools": best_tools
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── inject ────────────────────────────────────────────────

def cmd_inject(args):
    """注入当前最佳工具建议（所有任务类型）"""
    state = load_state()
    state = ensure_arms(state)

    total_records = state.get("total_records", 0)

    suggestions = {}
    for tt in TASK_TYPES:
        if total_records < COLD_START_EXPLORE:
            suggestions[tt] = {
                "tool": random.choice(TOOLS),
                "mode": "cold_start",
                "confidence": "low"
            }
        else:
            samples = thompson_sample(state, tt)
            best_tool, best_score, best_arm = samples[0]
            rate = round(best_arm["success"] / best_arm["total"], 4) if best_arm["total"] > 0 else 0.0
            suggestions[tt] = {
                "tool": best_tool,
                "mode": "thompson_sampling",
                "thompson_score": round(best_score, 6),
                "success_rate": rate,
                "samples": best_arm["total"],
                "confidence": "high" if best_arm["total"] >= 5 else "medium" if best_arm["total"] >= 2 else "low"
            }

    output = {
        "status": "ok",
        "total_records": total_records,
        "cold_start_threshold": COLD_START_EXPLORE,
        "phase": "cold_start" if total_records < COLD_START_EXPLORE else "thompson_sampling",
        "suggestions": suggestions
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── reset ─────────────────────────────────────────────────

def cmd_reset(args):
    """重置指定 (task_type, tool) 的统计数据"""
    task_type = args.task_type.lower()
    tool = args.tool.lower()

    if task_type not in TASK_TYPES:
        print(json.dumps({"status": "error", "message": f"未知任务类型: {task_type}", "valid_types": TASK_TYPES}, ensure_ascii=False))
        sys.exit(1)
    if tool not in TOOLS:
        print(json.dumps({"status": "error", "message": f"未知工具: {tool}", "valid_tools": TOOLS}, ensure_ascii=False))
        sys.exit(1)

    state = load_state()
    state = ensure_arms(state)

    key = f"{task_type}::{tool}"
    old = state["arms"].get(key, {"success": 0, "total": 0})
    state["arms"][key] = {"success": 0, "total": 0, "task_type": task_type, "tool": tool}

    # 调整 total_records
    state["total_records"] = max(0, state.get("total_records", 0) - old["total"])

    save_state(state)

    print(json.dumps({
        "status": "reset",
        "task_type": task_type,
        "tool": tool,
        "previous_records": old["total"],
        "new_total_records": state["total_records"]
    }, ensure_ascii=False, indent=2))


# ── reset-all ─────────────────────────────────────────────

def cmd_reset_all(args):
    """完全重置所有统计数据"""
    state = _default_state()
    save_state(state)
    print(json.dumps({"status": "reset_all", "message": "所有 bandit 统计数据已重置", "state": state}, ensure_ascii=False, indent=2))


# ── 入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bandit 工具路由器 — 基于 Thompson Sampling 的自适应工具选择",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s suggest --task-type debug
  %(prog)s record --task-type debug --tool terminal --success
  %(prog)s stats
  %(prog)s inject
  %(prog)s reset --task-type debug --tool terminal
  %(prog)s reset-all
        """
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    # suggest
    p_suggest = sub.add_parser("suggest", help="建议最佳工具")
    p_suggest.add_argument("--task-type", "-t", required=True, help=f"任务类型: {', '.join(TASK_TYPES)}")

    # record
    p_record = sub.add_parser("record", help="记录工具使用结果")
    p_record.add_argument("--task-type", "-t", required=True, help=f"任务类型: {', '.join(TASK_TYPES)}")
    p_record.add_argument("--tool", required=True, help=f"工具名称: {', '.join(TOOLS)}")
    p_record.add_argument("--success", action="store_true", help="标记本次使用成功")
    p_record.add_argument("--failure", action="store_true", dest="failure", help="标记本次使用失败（默认即为失败）")

    # stats
    sub.add_parser("stats", help="显示工具-任务有效性矩阵")

    # inject
    sub.add_parser("inject", help="注入当前最佳工具建议（所有任务类型）")

    # reset
    p_reset = sub.add_parser("reset", help="重置指定任务-工具组合的统计数据")
    p_reset.add_argument("--task-type", "-t", required=True, help=f"任务类型: {', '.join(TASK_TYPES)}")
    p_reset.add_argument("--tool", required=True, help=f"工具名称: {', '.join(TOOLS)}")

    # reset-all
    sub.add_parser("reset-all", help="完全重置所有统计数据")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    ensure_dirs()

    # 处理 success/failure 互斥逻辑（record 命令）
    if args.command == "record":
        # --failure 不改变 success 标志，默认 success=False
        # 如果同时给了 --success 和 --failure，--success 生效
        pass

    commands = {
        "suggest": cmd_suggest,
        "record": cmd_record,
        "stats": cmd_stats,
        "inject": cmd_inject,
        "reset": cmd_reset,
        "reset-all": cmd_reset_all,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()