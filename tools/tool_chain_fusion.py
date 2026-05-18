#!/usr/bin/env python3
"""
tool_chain_fusion.py — 工具链融合引擎（对标 LangChain/LlamaIndex）

自动学习高频工具序列，通过一阶马尔可夫链预测后续工具调用，
识别可融合的工具链模式。

用法:
  python3 tool_chain_fusion.py record --tools "web_search,web_extract,write_file"
  python3 tool_chain_fusion.py suggest --tools "web_search"
  python3 tool_chain_fusion.py stats
  python3 tool_chain_fusion.py inject
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────
CHAIN_DIR = Path.home() / ".hermes" / "tool_chain"
STATE_FILE = CHAIN_DIR / "chain_state.json"
FUSION_MIN_COUNT = 3  # 连续出现 3+ 次标记为 fusion_candidate


# ── 数据结构 ──────────────────────────────────────────
def _default_state() -> dict:
    """返回初始状态结构。"""
    return {
        "version": 1,
        "transitions": {},       # {"A": {"B": count, "C": count}, ...}
        "sequences": {},         # {"A,B,C": {"count": N, "last_seen": ts}, ...}
        "fusion_candidates": [], # [{"sequence": [...], "count": N, "last_seen": ts}, ...]
        "total_recordings": 0,
        "created": time.time(),
        "updated": time.time(),
    }


# ── 持久化 ────────────────────────────────────────────
def _ensure_dir() -> None:
    CHAIN_DIR.mkdir(parents=True, exist_ok=True)


def _load_state() -> dict:
    """加载链状态。缺失则返回默认状态。"""
    if not STATE_FILE.exists():
        return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # 补全缺失的键（向后兼容）
        default = _default_state()
        for key in default:
            if key not in state:
                state[key] = default[key]
        return state
    except (json.JSONDecodeError, OSError):
        return _default_state()


def _save_state(state: dict) -> None:
    """保存链状态。"""
    _ensure_dir()
    state["updated"] = time.time()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── 马尔可夫链更新 ────────────────────────────────────
def _update_transitions(state: dict, tools: list[str]) -> None:
    """更新一阶马尔可夫转移矩阵。

    对工具序列中每对相邻工具 (A, B)：P(B|A) += 1
    """
    transitions = state.setdefault("transitions", {})
    for i in range(len(tools) - 1):
        a, b = tools[i], tools[i + 1]
        if a not in transitions:
            transitions[a] = {}
        transitions[a][b] = transitions[a].get(b, 0) + 1


def _update_sequences(state: dict, tools: list[str]) -> None:
    """更新工具序列记录和融合候选。

    序列键为逗号分隔字符串。
    连续出现 >= FUSION_MIN_COUNT 次 → 加入 fusion_candidates。
    """
    sequences = state.setdefault("sequences", {})
    key = ",".join(tools)
    if key not in sequences:
        sequences[key] = {"count": 0, "last_seen": 0}
    sequences[key]["count"] += 1
    sequences[key]["last_seen"] = time.time()

    # 检查是否达到融合候选阈值
    count = sequences[key]["count"]
    if count >= FUSION_MIN_COUNT:
        candidates = state.setdefault("fusion_candidates", [])
        # 检查是否已存在
        existing = [c for c in candidates if c["sequence"] == tools]
        if existing:
            existing[0]["count"] = count
            existing[0]["last_seen"] = time.time()
        else:
            candidates.append({
                "sequence": tools,
                "count": count,
                "last_seen": time.time(),
            })


# ── 预测 ──────────────────────────────────────────────
def _predict_next(state: dict, current_tool: str, max_depth: int = 3, min_prob: float = 0.15) -> list[str]:
    """基于马尔可夫链预测后续工具链。

    从 current_tool 出发，每次选择概率最高的后继工具，
    直到概率低于阈值或达到最大深度。

    Returns:
        预测的后续工具列表（不含当前工具）。
    """
    transitions = state.get("transitions", {})
    chain: list[str] = []
    current = current_tool
    visited: set[str] = {current}

    for _ in range(max_depth):
        successors = transitions.get(current, {})
        if not successors:
            break

        # 按频率排序，选最高的
        sorted_succ = sorted(successors.items(), key=lambda x: x[1], reverse=True)
        total = sum(v for _, v in sorted_succ)

        if total == 0:
            break

        best_tool, best_count = sorted_succ[0]
        prob = best_count / total

        if prob < min_prob:
            break
        if best_tool in visited:
            break  # 防止循环

        chain.append(best_tool)
        visited.add(best_tool)
        current = best_tool

    return chain


# ── CLI 命令 ──────────────────────────────────────────
def cmd_record(tools_str: str) -> dict:
    """记录一次工具调用序列。"""
    tools = [t.strip() for t in tools_str.split(",") if t.strip()]
    if len(tools) < 2:
        result = {
            "status": "error",
            "action": "record",
            "message": "至少需要 2 个工具才能形成序列。",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    state = _load_state()
    _update_transitions(state, tools)
    _update_sequences(state, tools)
    state["total_recordings"] += 1
    _save_state(state)

    result = {
        "status": "ok",
        "action": "record",
        "sequence": tools,
        "sequence_length": len(tools),
        "total_recordings": state["total_recordings"],
        "transitions_learned": len(tools) - 1,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def cmd_suggest(tools_str: str) -> dict:
    """预测后续工具链。"""
    tools = [t.strip() for t in tools_str.split(",") if t.strip()]
    if not tools:
        result = {
            "status": "error",
            "action": "suggest",
            "message": "至少提供一个当前工具。",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    current = tools[-1]  # 取最后一个工具作为当前状态
    state = _load_state()
    chain = _predict_next(state, current)

    # 获取转移概率详情
    transitions = state.get("transitions", {})
    successors = transitions.get(current, {})
    total = sum(successors.values()) if successors else 0
    candidates = []
    for tool, count in sorted(successors.items(), key=lambda x: x[1], reverse=True):
        candidates.append({
            "tool": tool,
            "count": count,
            "probability": round(count / total, 4) if total > 0 else 0,
        })

    result = {
        "status": "ok",
        "action": "suggest",
        "current_tool": current,
        "input_sequence": tools,
        "predicted_chain": chain,
        "full_sequence": tools + chain,
        "candidates": candidates[:10],
        "total_recordings": state["total_recordings"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def cmd_stats() -> dict:
    """统计报告。"""
    state = _load_state()
    transitions = state.get("transitions", {})
    sequences = state.get("sequences", {})
    candidates = state.get("fusion_candidates", [])

    # 转移矩阵统计
    total_edges = sum(len(succ) for succ in transitions.values())
    unique_sources = len(transitions)
    all_targets: set[str] = set()
    for succ in transitions.values():
        all_targets.update(succ.keys())
    unique_targets = len(all_targets)

    # 热门转移 Top 10
    top_transitions: list[dict] = []
    for src, targets in transitions.items():
        for tgt, count in targets.items():
            top_transitions.append({"from": src, "to": tgt, "count": count})
    top_transitions.sort(key=lambda x: x["count"], reverse=True)

    # 最频繁序列 Top 10
    top_sequences = sorted(
        [{"sequence": k, "count": v["count"], "last_seen": v["last_seen"]}
         for k, v in sequences.items()],
        key=lambda x: x["count"],
        reverse=True,
    )

    # 融合候选
    fusion_info = sorted(candidates, key=lambda x: x["count"], reverse=True)

    result = {
        "status": "ok",
        "action": "stats",
        "total_recordings": state["total_recordings"],
        "unique_sequences": len(sequences),
        "unique_transition_sources": unique_sources,
        "unique_transition_targets": unique_targets,
        "total_transition_edges": total_edges,
        "fusion_candidates_count": len(candidates),
        "top_transitions": top_transitions[:10],
        "top_sequences": top_sequences[:10],
        "fusion_candidates": fusion_info,
        "storage_file": str(STATE_FILE),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def cmd_inject() -> dict:
    """注入当前活跃的融合链建议。"""
    state = _load_state()
    candidates = state.get("fusion_candidates", [])

    if not candidates:
        output = {
            "status": "ok",
            "action": "inject",
            "injected": False,
            "message": "暂无融合候选序列（需连续 3+ 次出现）。",
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return output

    # 按出现次数降序排列
    sorted_candidates = sorted(candidates, key=lambda x: x["count"], reverse=True)

    lines = ["## 🔗 工具链融合建议\n"]
    lines.append(f"基于 {state['total_recordings']} 次记录，发现以下高频工具链：\n")

    for i, c in enumerate(sorted_candidates, 1):
        chain_str = " → ".join(c["sequence"])
        lines.append(f"**{i}.** `{chain_str}`")
        lines.append(f"  出现 {c['count']} 次（常用，可合并为复合调用）\n")

    # 同时输出热门转移
    transitions = state.get("transitions", {})
    if transitions:
        lines.append("---\n")
        lines.append("### 热门工具转移\n")
        top: list[tuple[str, str, int]] = []
        for src, targets in transitions.items():
            for tgt, count in targets.items():
                top.append((src, tgt, count))
        top.sort(key=lambda x: x[2], reverse=True)

        for src, tgt, count in top[:5]:
            lines.append(f"- `{src}` → `{tgt}`（{count} 次）\n")

    inject_text = "\n".join(lines)

    output = {
        "status": "ok",
        "action": "inject",
        "injected": True,
        "injection_text": inject_text,
        "candidate_count": len(sorted_candidates),
        "top_candidate": {
            "sequence": sorted_candidates[0]["sequence"],
            "chain_str": " → ".join(sorted_candidates[0]["sequence"]),
            "count": sorted_candidates[0]["count"],
        } if sorted_candidates else None,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output


# ── 入口 ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="工具链融合引擎（对标 LangChain/LlamaIndex）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # record
    p_record = subparsers.add_parser("record", help="记录工具调用序列")
    p_record.add_argument(
        "--tools", required=True,
        help="逗号分隔的工具名称序列，如 'web_search,web_extract,write_file'"
    )

    # suggest
    p_suggest = subparsers.add_parser("suggest", help="预测后续工具链")
    p_suggest.add_argument(
        "--tools", required=True,
        help="当前工具序列（逗号分隔），取最后一个作为当前状态进行预测"
    )

    # stats
    subparsers.add_parser("stats", help="统计报告")

    # inject
    subparsers.add_parser("inject", help="注入活跃融合链建议")

    args = parser.parse_args()

    if args.command == "record":
        cmd_record(args.tools)
    elif args.command == "suggest":
        cmd_suggest(args.tools)
    elif args.command == "stats":
        cmd_stats()
    elif args.command == "inject":
        cmd_inject()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()