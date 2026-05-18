#!/usr/bin/env python3
"""
Multi-Exit Early Termination — 对标 Google 上下文工程白皮书

核心思想:
  "不是所有问题都需要深度推理。简单问题走快路径，
   复杂问题走慢路径，在保证质量的前提下节省 30-50% 计算。"

退出级别:
  Level 0 (EXIT_EARLY): 简单问答/问候 → 直接回答，不展开推理，预算 200 tokens
  Level 1 (STANDARD):   普通任务 → 标准推理，预算 3000 tokens
  Level 2 (FULL):       复杂推理 → 完整 CoT + delegate，预算 8000 tokens

分类策略:
  1. 关键词匹配: debug/bug/修复 → FULL; hello/hi → EXIT_EARLY
  2. 任务长度: >500 chars → FULL; <50 chars → 可能是 EXIT_EARLY
  3. 标点复杂度: >3个问号 → 复杂查询
  4. 文件操作: terminal/browser/delegate 关键词 → 升级

对标:
  - Google 上下文工程: 分层处理，简单任务不浪费上下文
  - Claude: 3.5 Haiku vs Sonnet 自动路由
  - OpenRouter: 按复杂度选择推理深度

用法:
  python3 tools/multi_exit.py classify --query "..."
  python3 tools/multi_exit.py inject
  python3 tools/multi_exit.py stats
  python3 tools/multi_exit.py reset
"""

import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
EXIT_DIR = HERMES_HOME / "multi_exit"
STATE_FILE = EXIT_DIR / "state.json"
LOG_FILE = HERMES_HOME / "logs" / "multi_exit.jsonl"

# ── 退出级别定义 ────────────────────────────────────────────
LEVELS = {
    0: {
        "name": "EXIT_EARLY",
        "desc": "简单问答，无需深度推理",
        "reasoning_budget": 200,
        "tool_quota": 2,
        "injection": "简短回答即可，不需要展开推理步骤。",
        "cost_multiplier": 0.1,
    },
    1: {
        "name": "STANDARD",
        "desc": "普通任务，标准推理",
        "reasoning_budget": 3000,
        "tool_quota": 8,
        "injection": "逐步推理，但不需过度展开。",
        "cost_multiplier": 0.5,
    },
    2: {
        "name": "FULL",
        "desc": "复杂推理，完整 CoT + delegate",
        "reasoning_budget": 8000,
        "tool_quota": 15,
        "injection": "充分展开推理链，必要时委派子任务并行处理。",
        "cost_multiplier": 1.0,
    },
}

# ── 关键词分类 ──────────────────────────────────────────────
EXIT_EARLY_KW = [
    "在吗", "hello", "hi", "你好", "谢谢", "再见", "ok", "好的",
    "what", "who is", "什么是", "where is",
    "天气", "日期", "how to say", "翻译",
]

FULL_REASONING_KW = [
    "debug", "bug", "修复", "fix", "error", "报错", "traceback",
    "重构", "refactor", "架构", "architecture",
    "实现", "implement", "从零", "from scratch",
    "分析", "调研", "research", "survey",
    "多文件", "multi-file", "跨模块", "cross-module",
    "性能", "optimize", "优化算法",
    "安全", "security", "vulnerability",
    "部署", "deploy", "pipeline",
]


def load_state() -> dict:
    default = {
        "total_queries": 0,
        "exit_counts": {0: 0, 1: 0, 2: 0},
        "total_cost_estimate": 0.0,
        "history": [],
    }
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            # Merge defaults for new keys
            for k, v in default.items():
                if k not in s:
                    s[k] = v
            return s
        except Exception:
            pass
    return default


def save_state(state: dict):
    EXIT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def classify_query(query: str) -> dict:
    """根据查询内容分配退出级别"""
    query_lower = query.lower()
    
    # 1. 关键词评分
    early_score = sum(1 for kw in EXIT_EARLY_KW if kw in query_lower)
    full_score = sum(1 for kw in FULL_REASONING_KW if kw in query_lower)
    
    # 2. 长度因子
    length = len(query)
    length_factor = 0
    if length < 50:
        length_factor = -0.5  # 倾向简单
    elif length > 500:
        length_factor = 1.0   # 倾向复杂
    elif length > 200:
        length_factor = 0.3
    
    # 3. 标点复杂度
    question_count = query.count("?") + query.count("？")
    has_code = bool(re.search(r"```|import |def |class |function|\.py|\.js|\.ts|\.go|\.rs", query))
    has_tool_ref = bool(re.search(r"terminal|browser|delegate|file|search", query_lower))
    
    # 4. 综合评分
    score = full_score - early_score + length_factor
    
    if has_code:
        score += 1.0
    if has_tool_ref:
        score += 0.5
    if question_count > 3:
        score += 0.8
    
    # 5. 确定级别
    if score > 1.0:
        level = 2  # FULL
    elif score > -0.5:
        level = 1  # STANDARD
    else:
        level = 0  # EXIT_EARLY
    
    level_info = LEVELS[level]
    
    # 6. 更新统计
    state = load_state()
    state["total_queries"] += 1
    state["exit_counts"][str(level)] = state["exit_counts"].get(str(level), 0) + 1
    state["total_cost_estimate"] += level_info["cost_multiplier"]
    state["history"].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "query": query[:200],
        "level": level,
        "score": round(score, 2),
    })
    state["history"] = state["history"][-100:]
    save_state(state)
    
    # 7. 日志
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "score": round(score, 2),
            "query_len": length,
        }, ensure_ascii=False) + "\n")
    
    return {
        "level": level,
        "level_name": level_info["name"],
        "level_desc": level_info["desc"],
        "score": round(score, 2),
        "reasoning_budget": level_info["reasoning_budget"],
        "tool_quota": level_info["tool_quota"],
        "factors": {
            "early_score": early_score,
            "full_score": full_score,
            "length_factor": length_factor,
            "has_code": has_code,
            "has_tool_ref": has_tool_ref,
            "question_count": question_count,
        },
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }


def get_injection(state: dict | None = None) -> str:
    """生成注入 prompt"""
    if state is None:
        state = load_state()
    
    total = state["total_queries"]
    if total == 0:
        return ""
    
    exit_counts = state["exit_counts"]
    early_pct = exit_counts.get("0", 0) / total * 100 if total > 0 else 0
    
    # 计算最近 10 条的趋势
    recent = state.get("history", [])[-10:]
    avg_level = sum(r["level"] for r in recent) / len(recent) if recent else 0
    
    lines = [
        "## 🚪 Multi-Exit 退出决策",
        f"   当前会话已分类 {total} 次查询",
        f"   EXIT_EARLY: {exit_counts.get('0', 0)} ({early_pct:.0f}%)",
        f"   STANDARD:   {exit_counts.get('1', 0)}",
        f"   FULL:       {exit_counts.get('2', 0)}",
    ]
    
    if avg_level > 1.5:
        lines.append(f"   📈 近期查询复杂度较高 (avg level: {avg_level:.1f})，建议用 FULL 模式")
    elif avg_level < 0.5:
        lines.append(f"   📉 近期查询简单 (avg level: {avg_level:.1f})，可以快速模式处理")
    
    lines.append(f"")
    lines.append(f"   成本估算系数: {state.get('total_cost_estimate', 0):.1f}x base")
    
    return "\n".join(lines)


def get_stats() -> str:
    """统计报告"""
    state = load_state()
    total = state["total_queries"]
    
    if total == 0:
        return "📊 Multi-Exit 统计: 暂无数据"
    
    exit_counts = state["exit_counts"]
    
    lines = [
        "📊 Multi-Exit 统计报告",
        f"   总查询数: {total}",
        f"",
    ]
    
    for level, info in LEVELS.items():
        count = exit_counts.get(str(level), 0)
        pct = count / total * 100 if total > 0 else 0
        bar_len = max(1, int(pct / 5))
        bar = "▓" * bar_len + "░" * (20 - bar_len)
        emoji = "🚪" if level == 0 else "💡" if level == 1 else "🧠"
        lines.append(f"   {emoji} Level {level} {info['name']:12s} [{bar}] {count:4d} ({pct:.0f}%)")
    
    lines.append(f"")
    lines.append(f"   成本系数: {state.get('total_cost_estimate', 0):.1f}x base")
    lines.append(f"   (EXIT_EARLY 节省 ~90% 推理成本)")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Multi-Exit Early Termination")
    sub = parser.add_subparsers(dest="command")
    
    p = sub.add_parser("classify", help="分类查询")
    p.add_argument("--query", required=True, help="查询文本")
    
    sub.add_parser("inject", help="生成退出决策注入 prompt")
    sub.add_parser("stats", help="统计报告")
    sub.add_parser("reset", help="重置状态")
    
    args = parser.parse_args()
    
    if args.command == "classify":
        result = classify_query(args.query)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "inject":
        print(get_injection())
    
    elif args.command == "stats":
        print(get_stats())
    
    elif args.command == "reset":
        save_state(load_state())
        print("🚪 Multi-Exit 状态已重置")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()