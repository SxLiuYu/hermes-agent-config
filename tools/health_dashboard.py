#!/usr/bin/env python3
"""
Agent Health Dashboard — 对标 LangSmith / W&B

聚合来自各子系统的健康指标，生成 JSON 仪表盘。
支持告警检测 (坍塌预警、低置信度峰值等)。

对标:
  - LangSmith: LLM 调用追踪 + 成本 + 延迟监控
  - W&B (Weights & Biases): 实验追踪 + 可视化仪表盘
  - Datadog LLM Observability: token 用量 + 性能趋势

数据源:
  - metacognition:   total_assessments, low_confidence_events, strategy_distribution
  - context_budget:  usage_rate, collapse_warnings, layer_utilization
  - model_router:    token usage, cost by model
  - milestones:      plan completion rate
  - multi_exit:      early exit rate (if available)

存储:
  ~/.hermes/health/
    dashboard.json   — 聚合仪表盘数据

用法:
  python3 health_dashboard.py collect   — 收集所有指标
  python3 health_dashboard.py report    — 美观打印摘要
  python3 health_dashboard.py alert     — 检查告警条件
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HEALTH_DIR = HERMES_HOME / "health"
DASHBOARD_FILE = HEALTH_DIR / "dashboard.json"

# 各数据源路径
META_STATE = HERMES_HOME / "metacognition" / "state.json"
META_STRATEGY_LOG = HERMES_HOME / "logs" / "metacognition_strategy.jsonl"
BUDGET_STATE = HERMES_HOME / "context_budget" / "state.json"
BUDGET_LOG = HERMES_HOME / "logs" / "context_budget.jsonl"
MODEL_USAGE_LOG = HERMES_HOME / "logs" / "model_usage.jsonl"
MILESTONES_DIR = HERMES_HOME / "milestones"
MULTI_EXIT_LOG = HERMES_HOME / "logs" / "multi_exit.jsonl"


# ── 收集器 ────────────────────────────────────────────────────────────────

def safe_read_json(path: Path) -> dict:
    """安全读取 JSON 文件，不存在或损坏返回空字典。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, PermissionError):
        return {}


def safe_read_jsonl(path: Path) -> list:
    """安全读取 JSONL 文件，返回条目列表。"""
    if not path.exists():
        return []
    entries = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except (IOError, PermissionError):
        pass
    return entries


def collect_metacognition() -> dict:
    """收集元认知指标。"""
    state = safe_read_json(META_STATE)
    strategy_log = safe_read_jsonl(META_STRATEGY_LOG)

    # 策略分布
    strategy_counts = {}
    for entry in strategy_log:
        s = entry.get("strategy", "unknown")
        strategy_counts[s] = strategy_counts.get(s, 0) + 1

    return {
        "total_assessments": state.get("total_assessments", 0),
        "low_confidence_events": state.get("low_confidence_events", 0),
        "clarify_requests": state.get("clarify_requests", 0),
        "current_strategy": state.get("strategy", "unknown"),
        "strategy_overrides": state.get("strategy_overrides", 0),
        "strategy_distribution": strategy_counts,
        "history_count": len(state.get("history", [])),
        "available": bool(state),
    }


def collect_context_budget() -> dict:
    """收集上下文预算指标。"""
    state = safe_read_json(BUDGET_STATE)
    budget_log = safe_read_jsonl(BUDGET_LOG)

    # 计算使用率
    consumed = state.get("consumed", {})
    allocations = state.get("allocations", {})
    layer_utilization = {}
    for layer in set(list(consumed.keys()) + list(allocations.keys())):
        alloc = allocations.get(layer, 1)
        cons = consumed.get(layer, 0)
        layer_utilization[layer] = {
            "allocated": alloc,
            "consumed": cons,
            "usage_rate": round(cons / alloc, 4) if alloc > 0 else 0.0,
        }

    # 全局使用率
    total_consumed = state.get("total_tokens", sum(consumed.values()))
    total_allocated = sum(allocations.values())
    global_usage_rate = round(total_consumed / total_allocated, 4) if total_allocated > 0 else 0.0

    # 最新日志条目
    latest_rates = []
    for entry in budget_log[-10:]:
        latest_rates.append({
            "ts": entry.get("ts", ""),
            "layer": entry.get("layer", ""),
            "rate": entry.get("rate", 0),
        })

    return {
        "session_id": state.get("session_id", ""),
        "total_allocated": total_allocated,
        "total_consumed": total_consumed,
        "global_usage_rate": global_usage_rate,
        "collapse_warnings": state.get("collapse_warnings", 0),
        "summaries_triggered": len(state.get("summaries", [])),
        "layer_utilization": layer_utilization,
        "recent_rates": latest_rates,
        "available": bool(state),
    }


def collect_model_router() -> dict:
    """收集模型路由指标。"""
    entries = safe_read_jsonl(MODEL_USAGE_LOG)

    by_model = {}
    total_tokens = 0
    total_cost = 0.0
    for entry in entries:
        model = entry.get("model", "unknown")
        tokens = entry.get("tokens", 0)
        cost = entry.get("cost", 0)

        if model not in by_model:
            by_model[model] = {"tokens": 0, "cost": 0.0, "calls": 0}
        by_model[model]["tokens"] += tokens
        by_model[model]["cost"] += cost
        by_model[model]["calls"] += 1

        total_tokens += tokens
        total_cost += cost

    return {
        "total_calls": len(entries),
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 6),
        "by_model": by_model,
        "available": len(entries) > 0,
    }


def collect_planning() -> dict:
    """收集规划（里程碑）指标。"""
    if not MILESTONES_DIR.exists():
        return {"available": False, "total_tasks": 0, "completion_rate": 0, "milestones": {}}

    task_files = sorted(MILESTONES_DIR.glob("*.json"))
    task_files = [f for f in task_files if f.name != "_reflection_buffer.json"]

    total_tasks = len(task_files)
    total_milestones = 0
    completed = 0
    failed = 0
    in_progress = 0
    pending = 0

    for tf in task_files:
        data = safe_read_json(tf)
        if not data:
            continue
        for ms in data.get("milestones", []):
            total_milestones += 1
            status = ms.get("status", "unknown")
            if status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
            elif status == "in_progress":
                in_progress += 1
            elif status == "pending":
                pending += 1

    completion_rate = round(completed / total_milestones, 4) if total_milestones > 0 else 0.0

    return {
        "total_tasks": total_tasks,
        "total_milestones": total_milestones,
        "completed": completed,
        "failed": failed,
        "in_progress": in_progress,
        "pending": pending,
        "completion_rate": completion_rate,
        "available": total_tasks > 0,
    }


def collect_multi_exit() -> dict:
    """收集多出口（early exit）指标。"""
    entries = safe_read_jsonl(MULTI_EXIT_LOG)

    total_exits = len(entries)
    early_exits = sum(1 for e in entries if e.get("type") == "early_exit" or e.get("early", False))
    early_exit_rate = round(early_exits / total_exits, 4) if total_exits > 0 else 0.0

    return {
        "total_exits": total_exits,
        "early_exits": early_exits,
        "early_exit_rate": early_exit_rate,
        "available": total_exits > 0,
    }


def collect_all() -> dict:
    """收集全部指标，生成完整仪表盘。"""
    now = datetime.now(timezone.utc).isoformat()

    metacog = collect_metacognition()
    budget = collect_context_budget()
    router = collect_model_router()
    planning = collect_planning()
    multi_exit = collect_multi_exit()

    dashboard = {
        "generated_at": now,
        "hermes_home": str(HERMES_HOME),
        "metacognition": metacog,
        "context_budget": budget,
        "model_router": router,
        "planning": planning,
        "multi_exit": multi_exit,
        "health_score": _compute_health_score(metacog, budget, planning),
        "alerts": _check_alerts(metacog, budget, planning),
    }

    return dashboard


def _compute_health_score(metacog: dict, budget: dict, planning: dict) -> dict:
    """计算综合健康分数 (0-100)。"""
    score = 100
    factors = []

    # 低置信度惩罚
    lc = metacog.get("low_confidence_events", 0)
    ta = metacog.get("total_assessments", 1)
    if ta > 0:
        lc_rate = lc / ta
        if lc_rate > 0.3:
            penalty = int(lc_rate * 30)
            score -= penalty
            factors.append(f"low_confidence_rate={lc_rate:.2f} (-{penalty})")

    # 上下文坍塌惩罚
    cw = budget.get("collapse_warnings", 0)
    if cw > 0:
        penalty = min(cw * 15, 50)
        score -= penalty
        factors.append(f"collapse_warnings={cw} (-{penalty})")

    # 全局使用率惩罚
    usage = budget.get("global_usage_rate", 0)
    if usage > 0.9:
        penalty = int((usage - 0.9) * 200)
        score -= penalty
        factors.append(f"high_usage_rate={usage:.2f} (-{penalty})")

    # 规划完成率奖励/惩罚
    cr = planning.get("completion_rate", 0)
    if planning.get("available"):
        if cr >= 0.8:
            factors.append(f"completion_rate={cr:.2f} (good)")
        elif cr < 0.3 and planning.get("total_milestones", 0) > 0:
            penalty = 10
            score -= penalty
            factors.append(f"low_completion_rate={cr:.2f} (-{penalty})")

    score = max(0, min(100, score))
    return {
        "score": score,
        "grade": _score_to_grade(score),
        "factors": factors,
    }


def _score_to_grade(score: int) -> str:
    if score >= 90:
        return "A"
    elif score >= 75:
        return "B"
    elif score >= 60:
        return "C"
    elif score >= 40:
        return "D"
    else:
        return "F"


def _check_alerts(metacog: dict, budget: dict, planning: dict) -> list:
    """检查需要关注的告警条件。"""
    alerts = []

    # 上下文坍塌告警
    cw = budget.get("collapse_warnings", 0)
    if cw >= 3:
        alerts.append({
            "level": "critical",
            "source": "context_budget",
            "message": f"上下文坍塌告警触发 {cw} 次，建议增加压缩频率",
            "metric": f"collapse_warnings={cw}",
        })
    elif cw >= 1:
        alerts.append({
            "level": "warning",
            "source": "context_budget",
            "message": f"上下文坍塌已发生 {cw} 次",
            "metric": f"collapse_warnings={cw}",
        })

    # 高使用率告警
    usage = budget.get("global_usage_rate", 0)
    if usage > 0.95:
        alerts.append({
            "level": "critical",
            "source": "context_budget",
            "message": f"上下文使用率 {usage:.1%}，接近耗尽",
            "metric": f"global_usage_rate={usage:.4f}",
        })
    elif usage > 0.85:
        alerts.append({
            "level": "warning",
            "source": "context_budget",
            "message": f"上下文使用率 {usage:.1%}，建议压缩",
            "metric": f"global_usage_rate={usage:.4f}",
        })

    # 低置信度峰值告警
    lc = metacog.get("low_confidence_events", 0)
    ta = metacog.get("total_assessments", 0)
    if ta > 0 and (lc / ta) > 0.5:
        alerts.append({
            "level": "warning",
            "source": "metacognition",
            "message": f"低置信度事件比例过高: {lc}/{ta} ({lc/ta:.1%})",
            "metric": f"low_confidence_rate={lc/ta:.4f}",
        })

    # 规划停滞告警
    total_ms = planning.get("total_milestones", 0)
    completed = planning.get("completed", 0)
    if total_ms > 0 and completed == 0:
        alerts.append({
            "level": "warning",
            "source": "planning",
            "message": f"有 {total_ms} 个里程碑但无一完成，可能规划停滞",
            "metric": f"completed=0, total={total_ms}",
        })

    # 无数据告警
    if not budget.get("available"):
        alerts.append({
            "level": "info",
            "source": "context_budget",
            "message": "上下文预算数据不可用",
        })
    if not metacog.get("available"):
        alerts.append({
            "level": "info",
            "source": "metacognition",
            "message": "元认知数据不可用",
        })

    return alerts


# ── 报告输出 ──────────────────────────────────────────────────────────────

def pretty_report(dashboard: dict):
    """美观打印仪表盘摘要。"""
    print("=" * 60)
    print("     Agent Health Dashboard")
    print("=" * 60)
    print(f"  生成时间: {dashboard.get('generated_at', 'N/A')}")
    print(f"  Hermes Home: {dashboard.get('hermes_home', 'N/A')}")
    print()

    hs = dashboard.get("health_score", {})
    print(f"  🏥 健康分数: {hs.get('score', '?')}/100  ({hs.get('grade', '?')})")
    for f in hs.get("factors", []):
        print(f"     └─ {f}")

    print()
    print("─" * 40)
    print("  📊 元认知 (Metacognition)")
    mc = dashboard.get("metacognition", {})
    print(f"     总评估: {mc.get('total_assessments', 0)}")
    print(f"     低置信度事件: {mc.get('low_confidence_events', 0)}")
    print(f"     当前策略: {mc.get('current_strategy', 'N/A')}")
    sd = mc.get("strategy_distribution", {})
    if sd:
        print(f"     策略分布: {sd}")

    print()
    print("─" * 40)
    print("  📦 上下文预算 (Context Budget)")
    cb = dashboard.get("context_budget", {})
    print(f"     已分配: {cb.get('total_allocated', 0)} tokens")
    print(f"     已消耗: {cb.get('total_consumed', 0)} tokens")
    print(f"     使用率: {cb.get('global_usage_rate', 0):.1%}")
    print(f"     坍塌告警: {cb.get('collapse_warnings', 0)}")
    lu = cb.get("layer_utilization", {})
    if lu:
        print("     层利用率:")
        for layer, info in sorted(lu.items()):
            print(f"       {layer}: {info['usage_rate']:.1%} ({info['consumed']}/{info['allocated']})")

    print()
    print("─" * 40)
    print("  🤖 模型路由 (Model Router)")
    mr = dashboard.get("model_router", {})
    print(f"     总调用: {mr.get('total_calls', 0)}")
    print(f"     总 Token: {mr.get('total_tokens', 0)}")
    print(f"     总成本: ${mr.get('total_cost', 0):.6f}")
    bm = mr.get("by_model", {})
    if bm:
        print("     按模型:")
        for model, info in sorted(bm.items()):
            print(f"       {model}: {info['calls']} 次, {info['tokens']} tokens, ${info['cost']:.6f}")

    print()
    print("─" * 40)
    print("  🎯 规划 (Planning)")
    pl = dashboard.get("planning", {})
    print(f"     任务数: {pl.get('total_tasks', 0)}")
    print(f"     里程碑: {pl.get('total_milestones', 0)}")
    print(f"     已完成: {pl.get('completed', 0)}")
    print(f"     失败: {pl.get('failed', 0)}")
    print(f"     进行中: {pl.get('in_progress', 0)}")
    print(f"     完成率: {pl.get('completion_rate', 0):.1%}")

    print()
    print("─" * 40)
    print("  🚪 多出口 (Multi Exit)")
    me = dashboard.get("multi_exit", {})
    print(f"     总退出: {me.get('total_exits', 0)}")
    print(f"     提前退出: {me.get('early_exits', 0)}")
    print(f"     提前退出率: {me.get('early_exit_rate', 0):.1%}")

    print()
    alerts = dashboard.get("alerts", [])
    if alerts:
        print("─" * 40)
        print("  🚨 告警")
        for a in alerts:
            level_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(a.get("level"), "⚪")
            print(f"     {level_icon} [{a.get('level', 'unknown').upper()}] {a.get('message', '')}")
    else:
        print("  ✅ 无告警")

    print()
    print("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Agent Health Dashboard — 健康指标聚合与告警"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collect", help="收集所有指标并保存到 dashboard.json")

    rpt = sub.add_parser("report", help="美观打印健康摘要")
    rpt.add_argument("--json", action="store_true", help="输出 JSON 而非美观打印")

    alt = sub.add_parser("alert", help="检查告警条件")
    alt.add_argument("--json", action="store_true", help="输出 JSON 格式")

    args = parser.parse_args()

    if args.command == "collect":
        HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        dashboard = collect_all()
        with open(DASHBOARD_FILE, "w") as f:
            json.dump(dashboard, f, indent=2, ensure_ascii=False)
        print(json.dumps({
            "status": "collected",
            "saved_to": str(DASHBOARD_FILE),
            "health_score": dashboard["health_score"]["score"],
            "alerts_count": len(dashboard["alerts"]),
        }, indent=2, ensure_ascii=False))

    elif args.command == "report":
        dashboard = collect_all()
        if args.json:
            print(json.dumps(dashboard, indent=2, ensure_ascii=False))
        else:
            pretty_report(dashboard)

    elif args.command == "alert":
        metacog = collect_metacognition()
        budget = collect_context_budget()
        planning = collect_planning()
        alerts = _check_alerts(metacog, budget, planning)

        if args.json:
            print(json.dumps({
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "alerts_count": len(alerts),
                "alerts": alerts,
            }, indent=2, ensure_ascii=False))
        else:
            if alerts:
                print(f"🚨 {len(alerts)} 条告警:")
                for a in alerts:
                    level_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(a.get("level"), "⚪")
                    print(f"  {level_icon} [{a.get('source', '?')}] {a.get('message', '')}")
            else:
                print("✅ 无告警 — 系统运行正常")


if __name__ == "__main__":
    main()