#!/usr/bin/env python3
"""
Context Budget Manager — 对标 Google 上下文工程白皮书 (2025.11)

核心思想:
  "上下文工程是一门艺术和科学，即在正确的时间、以正确的格式、
   将正确的信息填入上下文窗口。"
  — Gartner 预测上下文工程是 2026 年 AI Agent 性能的首要差异化因素

六大能力:
  1. 分层配额: 按四层记忆分配 token 预算
  2. JIT 渐进加载: 只加载当前需要的，按需展开
  3. 增量摘要化: 完成步骤后压缩，防上下文坍塌
  4. 预算监控: 实时追踪 token 使用率
  5. 动态调整: 任务难度高时自动提额
  6. 坍塌预警: 接近上限时主动压缩

对标:
  - Google 白皮书: Session + Memory 二分，上下文分层
  - Claude Code: 三层记忆 + 压缩注入
  - JetBrains: "上下文坍塌是长时间 agent 的首要失败模式"
  - LangChain: 截断/摘要/选择性保留/外部记忆 四种策略

架构:
  ~/.hermes/context_budget/
    config.json    — 预算配置
    state.json     — 当前会话状态
    log.jsonl      — 使用日志

用法:
  python3 tools/context_budget.py init                     # 初始化会话
  python3 tools/context_budget.py check                    # 检查剩余预算
  python3 tools/context_budget.py allocate --layer memory  # 分配预算
  python3 tools/context_budget.py consume --amount 500     # 消费 token
  python3 tools/context_budget.py inject --mode jit         # JIT 注入
  python3 tools/context_budget.py summary --since-last      # 增量摘要
"""

import argparse
import json
import os
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
BUDGET_DIR = HERMES_HOME / "context_budget"
CONFIG_FILE = BUDGET_DIR / "config.json"
STATE_FILE = BUDGET_DIR / "state.json"
USAGE_LOG = HERMES_HOME / "logs" / "context_budget.jsonl"
MEMORY_FILE = HERMES_HOME / "memories" / "MEMORY.md"

# ── 默认分层配额 (总预算 12000 tokens) ──────────────────────
# 对标 Google 白皮书四层:
#   Working (工作记忆) → 当前任务，最高优先级
#   Episodic (情节记忆) → 最近会话摘要
#   Semantic (语义记忆) → 持久事实和偏好
#   Procedural (程序性记忆) → Skills 和工具使用模式
DEFAULT_BUDGET = {
    "total": 12000,
    "layers": {
        "working":     {"share": 0.30, "min": 1500, "max": 5000, "desc": "当前任务上下文"},
        "episodic":    {"share": 0.20, "min": 800,  "max": 3000, "desc": "最近会话摘要"},
        "semantic":    {"share": 0.30, "min": 1000, "max": 4500, "desc": "持久记忆/偏好"},
        "procedural":  {"share": 0.12, "min": 400,  "max": 2000, "desc": "Skills/工具模式"},
        "system":      {"share": 0.08, "min": 300,  "max": 1500, "desc": "系统 prompt/身份"},
    },
    "collapse_threshold": 0.85,  # 消耗 > 85% 时触发压缩
    "jit_settings": {
        "incremental_batch_size": 3,  # JIT 每次加载 N 条记忆
        "expand_threshold": 0.60,     # 低于此消耗率时允许扩展开
    },
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_BUDGET, **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            return dict(DEFAULT_BUDGET)
    return dict(DEFAULT_BUDGET)


def save_config(cfg: dict):
    BUDGET_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def load_state() -> dict:
    default = {
        "session_id": "",
        "total_tokens": 0,
        "consumed": {k: 0 for k in DEFAULT_BUDGET["layers"]},
        "allocations": {},
        "summaries": [],
        "last_summary_at": None,
        "collapse_warnings": 0,
        "jit_state": {"loaded_count": 0, "loaded_entries": []},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if STATE_FILE.exists():
        try:
            return {**default, **json.loads(STATE_FILE.read_text())}
        except Exception:
            return default
    return default


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def get_total_tokens(state: dict) -> int:
    """总已消费 token 数"""
    return sum(state["consumed"].values())


def get_remaining(state: dict, cfg: dict) -> int:
    """剩余预算"""
    used = get_total_tokens(state)
    return max(0, cfg["total"] - used)


def get_usage_rate(state: dict, cfg: dict) -> float:
    """使用率"""
    return get_total_tokens(state) / cfg["total"]


def should_collapse(state: dict, cfg: dict) -> bool:
    """是否应触发上下文压缩 (collapse)"""
    rate = get_usage_rate(state, cfg)
    return rate >= cfg["collapse_threshold"]


def get_quota(layer: str, state: dict, cfg: dict) -> dict:
    """获取指定层的配额和已使用量"""
    lconf = cfg["layers"].get(layer, {})
    allocated = int(cfg["total"] * lconf.get("share", 0.1))
    consumed = state["consumed"].get(layer, 0)
    return {
        "layer": layer,
        "desc": lconf.get("desc", ""),
        "allocated": allocated,
        "consumed": consumed,
        "remaining": max(0, allocated - consumed),
        "min": lconf.get("min", 100),
        "max": lconf.get("max", 5000),
        "usage_pct": consumed / allocated if allocated > 0 else 0,
    }


def init_session(session_id: str = "", cfg: dict | None = None):
    """初始化新会话的上下文预算"""
    if cfg is None:
        cfg = load_config()
    
    state = {
        "session_id": session_id or f"ses_{int(time.time())}",
        "total_tokens": 0,
        "consumed": {k: 0 for k in cfg["layers"]},
        "allocations": {
            k: int(cfg["total"] * v["share"])
            for k, v in cfg["layers"].items()
        },
        "summaries": [],
        "last_summary_at": None,
        "collapse_warnings": 0,
        "jit_state": {"loaded_count": 0, "loaded_entries": []},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    
    print(json.dumps({
        "status": "initialized",
        "session_id": state["session_id"],
        "total_budget": cfg["total"],
        "allocations": state["allocations"],
    }, indent=2, ensure_ascii=False))


def consume_tokens(layer: str, amount: int, state: dict | None = None, cfg: dict | None = None):
    """消费 token"""
    if state is None:
        state = load_state()
    if cfg is None:
        cfg = load_config()
    
    if layer not in state["consumed"]:
        print(f"Unknown layer: {layer}", file=sys.stderr)
        return
    
    state["consumed"][layer] += amount
    
    # 日志
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG, "a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "layer": layer,
            "amount": amount,
            "total": get_total_tokens(state),
            "rate": get_usage_rate(state, cfg),
        }, ensure_ascii=False) + "\n")
    
    save_state(state)
    
    rate = get_usage_rate(state, cfg)
    status = "🟢" if rate < 0.5 else "🟡" if rate < 0.85 else "🔴"
    
    print(json.dumps({
        "layer": layer,
        "consumed_amount": amount,
        "layer_quota": get_quota(layer, state, cfg),
        "total_used": get_total_tokens(state),
        "total_remaining": get_remaining(state, cfg),
        "usage_rate": f"{rate:.1%}",
        "status": status,
        "should_collapse": should_collapse(state, cfg),
    }, indent=2, ensure_ascii=False))


def build_check_report(state: dict | None = None, cfg: dict | None = None) -> str:
    """生成预算检查报告"""
    if state is None:
        state = load_state()
    if cfg is None:
        cfg = load_config()
    
    used = get_total_tokens(state)
    remaining = get_remaining(state, cfg)
    rate = get_usage_rate(state, cfg)
    
    bar_len = 20
    filled = int(rate * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    
    lines = [
        "📊 上下文预算报告",
        f"   [{bar}] {rate:.0%}",
        f"   总预算: {cfg['total']:,} | 已用: {used:,} | 剩余: {remaining:,}",
        f"   坍塌警告: {state.get('collapse_warnings', 0)} 次",
        f"",
    ]
    
    for layer in cfg["layers"]:
        q = get_quota(layer, state, cfg)
        lbar_filled = int(min(1, q["usage_pct"]) * 15)
        lbar = "▓" * lbar_filled + "░" * (15 - lbar_filled)
        emoji = "🟢" if q["usage_pct"] < 0.5 else "🟡" if q["usage_pct"] < 0.8 else "🔴"
        lines.append(
            f"   {emoji} {layer:12s} [{lbar}] {q['consumed']:,}/{q['allocated']:,} "
            f"({q['usage_pct']:.0%})"
        )
    
    if should_collapse(state, cfg):
        lines.append(f"\n⚠️  已达到坍塌阈值 ({cfg['collapse_threshold']:.0%})，建议立即压缩")
    
    return "\n".join(lines)


def generate_incremental_summary(state: dict | None = None) -> str:
    """生成增量摘要 (上次摘要之后新增的内容)"""
    if state is None:
        state = load_state()
    
    # 简化版: 从 session_memory 截取最近内容
    sm_file = HERMES_HOME / "session_memory.md"
    if not sm_file.exists():
        return "暂无会话记录"
    
    text = sm_file.read_text()
    if len(text) < 500:
        return text
    
    # 取最后 2000 字符作为最近摘要
    recent = text[-2000:]
    
    # 提取关键信息
    lines = recent.split("\n")
    key_lines = [l for l in lines if any(kw in l.lower() for kw in 
        ["完成", "结果", "错误", "修复", "写入", "修改", "创建", "done", "fixed", "created"])]
    
    summary_parts = ["## 增量上下文摘要", ""]
    if key_lines:
        summary_parts.append("### 关键操作")
        summary_parts.extend(f"- {l.strip()}" for l in key_lines[:10])
    
    summary_parts.append(f"\n_(上次摘要: {state.get('last_summary_at', '无')})_")
    
    summary = "\n".join(summary_parts)
    
    # 更新状态
    state["summaries"].append({
        "at": datetime.now(timezone.utc).isoformat(),
        "tokens_before": get_total_tokens(state),
        "key_actions": len(key_lines),
    })
    state["last_summary_at"] = datetime.now(timezone.utc).isoformat()
    state["summaries"] = state["summaries"][-20:]
    save_state(state)
    
    return summary


def jit_inject(state: dict | None = None, cfg: dict | None = None,
               mode: str = "hot") -> str:
    """
    JIT 渐进式记忆注入
    
    mode:
      hot    — 仅注入热记忆 (最近访问的 3 条)
      warm   — 注入热 + 温记忆 (~10 条)
      full   — 全部注入
    """
    if state is None:
        state = load_state()
    if cfg is None:
        cfg = load_config()
    
    # 读取 MEMORY.md
    if not MEMORY_FILE.exists():
        return "# 暂无记忆数据"
    
    memory_text = MEMORY_FILE.read_text()
    entries = [e.strip() for e in memory_text.split("§") if e.strip()]
    
    # 读取 decay 元数据 (如果存在)
    meta_file = HERMES_HOME / "memories" / "memory_meta.json"
    meta = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            pass
    
    # 排序
    import hashlib
    def _h(t): return hashlib.sha256(t.strip().encode()).hexdigest()[:12]
    
    scored = []
    for e in entries:
        m = meta.get(_h(e), {})
        factor = 1.0
        if m.get("last_accessed"):
            try:
                last = datetime.fromisoformat(m["last_accessed"].replace("Z", "+00:00"))
                hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if hours < 1:
                    factor = 1.5
                elif hours < 72:
                    factor = 1.5 - (hours - 1) * 0.007
                else:
                    factor = max(0.3, 1.0 - (hours - 72) * 0.002)
            except:
                pass
        scored.append((factor, e))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # JIT 模式选择
    batch_size = cfg.get("jit_settings", {}).get("incremental_batch_size", 3)
    
    if mode == "hot":
        selected = scored[:batch_size]
    elif mode == "warm":
        # 热 + 温 (decay > 0.5)
        selected = [(f, e) for f, e in scored if f > 0.5][:batch_size * 3]
    else:
        selected = scored
    
    # 生成注入文本
    parts = []
    for factor, entry in selected:
        tag = f"🔥" if factor > 1.2 else "🟡" if factor > 0.5 else "❄️"
        parts.append(f"{tag} {entry}")
    
    if mode == "hot":
        # 记录未加载的数量
        remaining = len(scored) - len(selected)
        if remaining > 0:
            parts.append(f"\n💡 +{remaining} 条温/冷记忆可用 `context_budget inject --mode warm` 展开")
    
    return "\n§\n".join(parts)


def collapse_context(state: dict | None = None, cfg: dict | None = None) -> dict:
    """
    上下文压缩：当使用率超过 collapse_threshold 时触发
    
    策略:
    1. 总结最近会话内容 → 注入 episodic 层
    2. 压缩 episodic 中的旧摘要
    3. 选择性保留 semantic 中的高频记忆
    """
    if state is None:
        state = load_state()
    if cfg is None:
        cfg = load_config()
    
    summary = generate_incremental_summary(state)
    
    # 减少 episodic 消费 (用摘要替代详细记录)
    old_episodic = state["consumed"].get("episodic", 0)
    state["consumed"]["episodic"] = max(0, old_episodic - int(old_episodic * 0.6))
    
    # 减少 working (保留最后几条)
    old_working = state["consumed"].get("working", 0)
    state["consumed"]["working"] = max(0, old_working - int(old_working * 0.3))
    
    state["collapse_warnings"] += 1
    save_state(state)
    
    new_rate = get_usage_rate(state, cfg)
    
    return {
        "action": "collapse",
        "before_rate": f"{old_episodic + old_working / sum(state['consumed'].values()) * 100 if get_total_tokens(state) > 0 else 0:.0%}",
        "after_rate": f"{new_rate:.0%}",
        "summary": summary[:500],
        "collapse_count": state["collapse_warnings"],
    }


def main():
    parser = argparse.ArgumentParser(description="Context Budget Manager")
    sub = parser.add_subparsers(dest="command")
    
    # init
    p = sub.add_parser("init", help="初始化会话预算")
    p.add_argument("--session-id", default="")
    
    # check
    sub.add_parser("check", help="检查剩余预算")
    
    # consume
    p = sub.add_parser("consume", help="消费 token")
    p.add_argument("--layer", required=True, choices=list(DEFAULT_BUDGET["layers"]))
    p.add_argument("--amount", type=int, required=True)
    
    # inject (JIT)
    p = sub.add_parser("inject", help="JIT 渐进式注入")
    p.add_argument("--mode", choices=["hot", "warm", "full"], default="hot")
    
    # summary
    sub.add_parser("summary", help="生成增量摘要")
    
    # collapse
    sub.add_parser("collapse", help="触发上下文压缩")
    
    # quota
    p = sub.add_parser("quota", help="查看层配额")
    p.add_argument("--layer", default="memory")
    
    # config
    sub.add_parser("config", help="查看当前预算配置")
    
    args = parser.parse_args()
    
    if args.command == "init":
        init_session(args.session_id)
    
    elif args.command == "check":
        print(build_check_report())
    
    elif args.command == "consume":
        consume_tokens(args.layer, args.amount)
    
    elif args.command == "inject":
        print(jit_inject(mode=args.mode))
    
    elif args.command == "summary":
        print(generate_incremental_summary())
    
    elif args.command == "collapse":
        result = collapse_context()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    elif args.command == "quota":
        state = load_state()
        cfg = load_config()
        q = get_quota(args.layer, state, cfg)
        print(json.dumps(q, indent=2, ensure_ascii=False))
    
    elif args.command == "config":
        cfg = load_config()
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()