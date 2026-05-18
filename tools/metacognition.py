#!/usr/bin/env python3
"""
MetaCognition Layer — 对标 SE-Agent (NeurIPS 2025) + DPA 双过程框架

核心能力:
  1. 自我评估: 输出前评估推理质量
  2. 策略选择: 根据任务特征选 CoT / Tree-of-Thought / 直接回答
  3. 置信度校准: 不确定时请求澄清
  4. 资源分配: 决定计算量投入

对标:
  - SE-Agent: 自进化轨迹优化，对多步推理进行评分和剪枝
  - DPA (Dual Process): 快系统1(检索) + 慢系统2(反思写入) 双层
  - Google 元认知: 自我评估 + 策略选择 + 资源分配

架构:
  ~/.hermes/metacognition/
    state.json        — 当前会话的元认知状态
    strategy_log.jsonl — 策略选择历史

用法:
  python3 tools/metacognition.py assess --task "..."       # 自我评估
  python3 tools/metacognition.py select-strategy --task "..." # 选策略
  python3 tools/metacognition.py confidence --answer "..." # 置信度校准
  python3 tools/metacognition.py allocate --task "..."     # 资源分配
  python3 tools/metacognition.py session-report            # 会话报告
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
META_DIR = HERMES_HOME / "metacognition"
STATE_FILE = META_DIR / "state.json"
STRATEGY_LOG = HERMES_HOME / "logs" / "metacognition_strategy.jsonl"
SESSION_MEMORY = HERMES_HOME / "session_memory.md"

# LLM
FINNA_URL = "https://www.finna.com.cn/v1/chat/completions"
FINNA_KEY = "app-6OzRGg93TfuDOny9NUnKMvQU"  # Qwen3-32b (轻量足够)

# 策略定义
STRATEGIES = {
    "direct":    "简单直接回答，不展开推理",
    "cot":       "Chain-of-Thought: 逐步推理，暴露思维过程",
    "tot":       "Tree-of-Thought: 探索多条路径，选最优",
    "replay":    "回放式: 先检索类似案例，再推理",
    "delegate":  "委派: 拆分任务给子 agent",
    "clarify":   "澄清: 先向用户确认关键信息",
}

# 任务特征 → 策略映射 (启发式)
TASK_PATTERNS = {
    "debug":    {"strategy": "cot",      "depth": 3, "tools": 5},
    "feature":  {"strategy": "replay",   "depth": 4, "tools": 8},
    "refactor": {"strategy": "tot",      "depth": 3, "tools": 6},
    "config":   {"strategy": "direct",   "depth": 2, "tools": 3},
    "optimize": {"strategy": "cot",      "depth": 3, "tools": 4},
    "data":     {"strategy": "delegate", "depth": 4, "tools": 7},
    "research": {"strategy": "replay",   "depth": 5, "tools": 10},
    "review":   {"strategy": "tot",      "depth": 2, "tools": 3},
}


def load_state() -> dict:
    default = {
        "session_id": "",
        "confidence_threshold": 0.7,
        "strategy": "direct",
        "strategy_overrides": 0,
        "total_assessments": 0,
        "low_confidence_events": 0,
        "clarify_requests": 0,
        "resources": {"total_tokens": 0, "tool_calls": 0},
        "history": [],
    }
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            return {**default, **s}
        except Exception:
            pass
    return default


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def classify_task(task_text: str) -> str:
    """简单规则判断任务类型"""
    kw = {
        "debug":    ["bug", "debug", "修复", "fix", "error", "报错", "fail", "traceback"],
        "feature":  ["实现", "新增", "添加", "feature", "implement", "add"],
        "refactor": ["重构", "refactor", "整理", "清理", "clean", "优化结构"],
        "config":   ["配置", "config", "setup", "install", "部署", "deploy"],
        "optimize": ["优化", "optimize", "性能", "performance", "加速"],
        "data":     ["数据", "data", "分析", "analysis", "extract", "采集", "搜索"],
        "research": ["调研", "研究", "research", "survey", "论文", "paper"],
        "review":   ["review", "审查", "review", "检查", "审计"],
    }
    text_lower = task_text.lower()
    scores = {k: sum(1 for w in v if w in text_lower) for k, v in kw.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "unknown"


def select_strategy(task_text: str, state: dict | None = None) -> dict:
    """根据任务特征选择最佳推理策略"""
    task_type = classify_task(task_text)
    config = TASK_PATTERNS.get(task_type, {"strategy": "cot", "depth": 3, "tools": 5})
    
    if state and state.get("strategy_overrides", 0) > 0:
        # 已有 override，检查是否需要调整
        recent = state.get("history", [])[-3:] if state.get("history") else []
        if recent and all(r.get("outcome") == "good" for r in recent):
            pass  # 当前策略有效，不调整
        else:
            # 降级到更保守的策略
            config["depth"] = max(1, config["depth"] - 1)
    
    strategy_name = config["strategy"]
    return {
        "task_type": task_type,
        "strategy": strategy_name,
        "strategy_desc": STRATEGIES.get(strategy_name, ""),
        "depth": config["depth"],
        "tool_quota": config["tools"],
        "selected_at": datetime.now(timezone.utc).isoformat(),
    }


def assess_confidence(answer_text: str, context: str = "") -> dict:
    """评估回答置信度"""
    # 轻量级规则评估
    uncertainty_markers = [
        r"可能", r"也许", r"不确定", r"不太确定",
        r"maybe", r"perhaps", r"uncertain", r"not sure",
        r"需要[更到]多[的]?(信息|细节|上下文)",
    ]
    low_conf_count = sum(1 for m in uncertainty_markers if re.search(m, answer_text, re.IGNORECASE))
    
    # 检查是否有实质性内容
    has_content = len(answer_text) > 100
    has_sources = bool(re.search(r"http|arxiv|github", answer_text))
    has_structure = bool(re.search(r"#{1,3}\s|^\d+\.\s|\*\*", answer_text, re.MULTILINE))
    
    # 计算置信度
    base = 0.6
    if has_content:     base += 0.1
    if has_sources:     base += 0.15
    if has_structure:   base += 0.1
    base -= low_conf_count * 0.08
    
    confidence = round(min(0.95, max(0.1, base)), 2)
    
    # 标记是否需要澄清
    needs_clarify = low_conf_count > 2 or confidence < 0.4
    
    return {
        "confidence": confidence,
        "level": "high" if confidence > 0.8 else "medium" if confidence > 0.5 else "low",
        "uncertainty_markers": low_conf_count,
        "needs_clarification": needs_clarify,
        "assessed_at": datetime.now(timezone.utc).isoformat(),
    }


def allocate_resources(task_type: str, budget_remaining: int = 12000) -> dict:
    """资源分配：决定投入多少计算"""
    config = TASK_PATTERNS.get(task_type, {"depth": 3, "tools": 5})
    
    # 基于任务类型分配 token 预算
    allocations = {
        "debug":    {"reasoning": 0.35, "search": 0.15, "tools": 0.30, "response": 0.20},
        "feature":  {"reasoning": 0.25, "search": 0.15, "tools": 0.40, "response": 0.20},
        "refactor": {"reasoning": 0.30, "search": 0.10, "tools": 0.35, "response": 0.25},
        "config":   {"reasoning": 0.15, "search": 0.20, "tools": 0.25, "response": 0.40},
        "optimize": {"reasoning": 0.35, "search": 0.15, "tools": 0.25, "response": 0.25},
        "data":     {"reasoning": 0.20, "search": 0.30, "tools": 0.30, "response": 0.20},
        "research": {"reasoning": 0.15, "search": 0.45, "tools": 0.15, "response": 0.25},
        "review":   {"reasoning": 0.40, "search": 0.10, "tools": 0.10, "response": 0.40},
    }
    
    alloc = allocations.get(task_type, allocations["debug"])
    
    return {
        "budget": budget_remaining,
        "allocations": {k: int(v * budget_remaining) for k, v in alloc.items()},
        "max_tool_calls": config["tools"],
        "reasoning_depth": config["depth"],
    }


def run_pre_assessment(task_text: str, state: dict) -> dict:
    """执行前置评估 (PreToolUse / SessionStart)"""
    # 1. 选策略
    strategy = select_strategy(task_text, state)
    
    # 2. 资源分配
    resources = allocate_resources(strategy["task_type"])
    
    # 3. 更新状态
    state["strategy"] = strategy["strategy"]
    state["history"].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "task": task_text[:200],
        "task_type": strategy["task_type"],
        "strategy": strategy["strategy"],
        "outcome": "pending",
    })
    # Keep last 50
    state["history"] = state["history"][-50:]
    save_state(state)
    
    # Log
    STRATEGY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(STRATEGY_LOG, "a") as f:
        f.write(json.dumps(strategy, ensure_ascii=False) + "\n")
    
    return {
        **strategy,
        **resources,
    }


def run_post_assessment(result_text: str, state: dict, tool_calls: int = 0) -> dict:
    """执行后置评估 (PostToolUse / Stop)"""
    conf = assess_confidence(result_text)
    
    state["total_assessments"] = (state.get("total_assessments", 0) + 1)
    state["resources"]["tool_calls"] += tool_calls
    
    if conf["level"] == "low":
        state["low_confidence_events"] += 1
    if conf["needs_clarification"]:
        state["clarify_requests"] += 1
    
    # 更新最后一个 history 的 outcome
    if state.get("history"):
        last = state["history"][-1]
        last["outcome"] = "good" if conf["confidence"] > 0.6 else "uncertain"
        last["confidence"] = conf["confidence"]
    
    save_state(state)
    
    return conf


def session_report() -> str:
    """生成会话元认知报告"""
    state = load_state()
    hist = state.get("history", [])
    
    if not hist:
        return "📊 元认知报告: 暂无数据"
    
    good = sum(1 for h in hist if h.get("outcome") == "good")
    uncertain = sum(1 for h in hist if h.get("outcome") == "uncertain")
    pending = sum(1 for h in hist if h.get("outcome") == "pending")
    
    # 策略分布
    strategy_counts = {}
    for h in hist:
        s = h.get("strategy", "unknown")
        strategy_counts[s] = strategy_counts.get(s, 0) + 1
    
    lines = [
        "🧠 元认知会话报告",
        f"   总决策点: {len(hist)}",
        f"   成功: {good} | 不确定: {uncertain} | 待定: {pending}",
        f"   低置信度事件: {state.get('low_confidence_events', 0)}",
        f"   澄清请求: {state.get('clarify_requests', 0)}",
        f"",
        f"   策略分布:",
    ]
    for s, c in sorted(strategy_counts.items(), key=lambda x: x[1], reverse=True):
        pct = c / len(hist) * 100
        lines.append(f"     - {s}: {c} 次 ({pct:.0f}%)")
    
    lines.append(f"")
    lines.append(f"   最近决策:")
    for h in hist[-5:]:
        emoji = "✅" if h.get("outcome") == "good" else "❓" if h.get("outcome") == "uncertain" else "⏳"
        lines.append(f"     {emoji} [{h.get('task_type','?')}] {h.get('task','')[:50]}")
    
    return "\n".join(lines)


def get_injection_prompt(state: dict | None = None) -> str:
    """
    生成元认知注入 prompt (用于 session_start hook)
    告诉 agent 当前应采取什么策略、使用多少资源
    """
    if state is None:
        state = load_state()
    
    strategy = state.get("strategy", "direct")
    strategy_desc = STRATEGIES.get(strategy, "")
    
    low_conf_count = state.get("low_confidence_events", 0)
    conf_warning = ""
    if low_conf_count > 3:
        conf_warning = f"\n⚠️  近期有 {low_conf_count} 次低置信度输出，建议更谨慎地推理或请求澄清。"
    
    return f"""## 🧠 MetaCognition Layer

当前策略: **{strategy}** — {strategy_desc}
累计评估: {state.get('total_assessments', 0)} 次
工具使用: {state.get('resources', {}).get('tool_calls', 0)} 次{conf_warning}

策略指南:
- direct: 简洁回答，不需要深度推理
- cot: 逐步推理，在 thinking 块中展示过程
- tot: 探索多个方案，比较后选最优
- replay: 用 session_search 检索类似过往案例，参考后回答
- delegate: 把子任务委派给 delegate_task
- clarify: 先向用户确认关键信息，确保不假设
"""[:1000]


def main():
    parser = argparse.ArgumentParser(description="MetaCognition Layer")
    sub = parser.add_subparsers(dest="command")
    
    # assess — 前置评估
    p = sub.add_parser("assess", help="执行前置评估")
    p.add_argument("--task", required=True, help="任务描述")
    
    # confidence — 置信度评估
    p = sub.add_parser("confidence", help="评估回答置信度")
    p.add_argument("--answer", required=True, help="回答文本")
    p.add_argument("--context", default="", help="上下文")
    
    # allocate — 资源分配
    p = sub.add_parser("allocate", help="分配资源")
    p.add_argument("--task", required=True, help="任务描述")
    p.add_argument("--budget", type=int, default=12000)
    
    # inject — 生成注入 prompt
    sub.add_parser("inject", help="生成元认知注入 prompt")
    
    # record — 记录结果
    p = sub.add_parser("record", help="记录执行结果")
    p.add_argument("--result", default="", help="执行结果文本")
    p.add_argument("--tool-calls", type=int, default=0)
    
    # report
    sub.add_parser("report", help="会话元认知报告")
    
    # reset
    p = sub.add_parser("reset", help="重置元认知状态")
    p.add_argument("--session-id", default="")
    
    args = parser.parse_args()
    state = load_state()
    
    if args.command == "assess":
        result = run_pre_assessment(args.task, state)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "confidence":
        result = assess_confidence(args.answer, args.context)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "allocate":
        task_type = classify_task(args.task)
        result = allocate_resources(task_type, args.budget)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "inject":
        print(get_injection_prompt(state))
    
    elif args.command == "record":
        result = run_post_assessment(args.result, state, args.tool_calls)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "report":
        print(session_report())
    
    elif args.command == "reset":
        state = load_state()
        state["session_id"] = args.session_id or ""
        state["strategy"] = "direct"
        state["total_assessments"] = 0
        state["low_confidence_events"] = 0
        state["clarify_requests"] = 0
        state["resources"] = {"total_tokens": 0, "tool_calls": 0}
        state["history"] = []
        save_state(state)
        print("🧹 元认知状态已重置")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()