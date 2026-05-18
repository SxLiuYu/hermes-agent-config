#!/usr/bin/env python3
"""
Identity Drift Detector — 对标 Letta Context Constitution

监控 agent 输出风格的漂移，确保 agent 保持一致的"身份感"。
Letta 的核心洞察：agent 需要持续的身份认知，否则会逐渐"遗忘自己是谁"。

检测维度:
  1. 语言风格 — 句子长度、词汇复杂度
  2. 工具使用模式 — tool call 频率和多样性
  3. 回应质量 — 代码块占比、回答长度
  4. 个性化特征 — emoji 使用、中文混合度

用法:
  hermes identity baseline          # 建立基线指纹
  hermes identity check             # 检查当前漂移
  hermes identity trend             # 显示趋势
  hermes identity alert             # 漂移告警
"""

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

HERMES_HOME = Path.home() / ".hermes"
IDENTITY_DIR = HERMES_HOME / "identity"
BASELINE_FILE = IDENTITY_DIR / "baseline.json"
DRIFT_LOG = IDENTITY_DIR / "drift_log.jsonl"

# 漂移阈值
DRIFT_WARN = 0.20   # 单维度漂移 >20% 告警
DRIFT_CRITICAL = 0.35  # 漂移 >35% 严重告警


# ────────────────────────────────────────────────────────
# 风格指纹提取
# ────────────────────────────────────────────────────────


def extract_fingerprint(text: str) -> dict:
    """从文本中提取风格指纹"""
    if not text or len(text) < 100:
        return _empty_fingerprint()

    sentences = re.split(r"[。！？.!?\n]+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]

    words = text.split()
    chars_total = len(text)
    chars_chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    chars_english = len(re.findall(r"[a-zA-Z]", text))

    # 句子特征
    avg_sentence_len = sum(len(s) for s in sentences) / max(len(sentences), 1)

    # 代码块
    code_blocks = len(re.findall(r"```", text)) // 2
    code_ratio = min(code_blocks / max(len(sentences) // 3, 1), 1.0)

    # emoji 使用
    emoji_count = len(re.findall(r"[\U0001F300-\U0001F9FF\u2600-\u27BF\u2700-\u27BF]", text))
    emoji_density = emoji_count / max(len(sentences), 1)

    # 标记符（bullet points, 编号等）
    bullets = len(re.findall(r"^[•\-\*\d+\.]", text, re.MULTILINE))

    # 英文占比
    eng_ratio = chars_english / max(chars_total, 1)

    # 中文占比
    cn_ratio = chars_chinese / max(chars_total, 1)

    # 平均词长（英文）
    avg_word_len = sum(len(w) for w in words) / max(len(words), 1) if words else 0

    return {
        "avg_sentence_len": round(avg_sentence_len, 1),
        "code_ratio": round(code_ratio, 3),
        "emoji_density": round(emoji_density, 3),
        "english_ratio": round(eng_ratio, 3),
        "chinese_ratio": round(cn_ratio, 3),
        "avg_word_len": round(avg_word_len, 1),
        "bullet_density": round(bullets / max(len(sentences), 1), 3),
        "chars_total": chars_total,
        "sentence_count": len(sentences),
    }


def _empty_fingerprint() -> dict:
    return {k: 0.0 for k in [
        "avg_sentence_len", "code_ratio", "emoji_density",
        "english_ratio", "chinese_ratio", "avg_word_len",
        "bullet_density", "chars_total", "sentence_count",
    ]}


def extract_tool_pattern(text: str) -> dict:
    """从会话记录中提取工具使用模式"""
    # 统计工具调用
    tool_pattern = re.findall(r"Tool:\s*(\w+)|<｜DSML｜invoke name=\"(\w+)\"", text)
    tools = [t[0] or t[1] for t in tool_pattern]

    counter = Counter(tools)
    total = max(len(tools), 1)

    return {
        "total_tool_calls": len(tools),
        "unique_tools": len(counter),
        "top_tools": dict(counter.most_common(5)),
        "terminal_ratio": counter.get("terminal", 0) / total,
        "write_ratio": counter.get("write_file", 0) / total,
        "search_ratio": (counter.get("web_search", 0) + counter.get("search_files", 0)) / total,
    }


# ────────────────────────────────────────────────────────
# 从最近的会话中提取指纹
# ────────────────────────────────────────────────────────


def get_recent_transcripts(n: int = 3) -> List[str]:
    """获取最近 N 次会话的转录文本"""
    transcripts = []
    session_dir = HERMES_HOME / "sessions"

    # 查找最近的会话记录
    sources = []

    # session_memory.md
    sm = HERMES_HOME / "session_memory.md"
    if sm.exists():
        text = sm.read_text()
        if len(text) > 500:
            sources.append(text)

    # 按会话分割
    if session_dir.exists():
        for d in sorted(session_dir.glob("session-*"), key=lambda x: x.stat().st_mtime, reverse=True):
            for f in d.glob("*.md"):
                try:
                    text = f.read_text()[:5000]
                    if len(text) > 200:
                        sources.append(text)
                except Exception:
                    pass
            if len(sources) >= n:
                break

    return sources


def _extract_agent_responses(text: str) -> str:
    """从混合转录中提取 agent 回复部分"""
    # 尝试按角色分割
    parts = re.split(r"(?:User:|用户:|Human:|assistant:|Assistant:)", text)
    if len(parts) > 1:
        # 取 agent 部分（通常较长）
        agent_parts = [p for p in parts[1:] if len(p) > 100]
        return "\n".join(agent_parts)
    return text


# ────────────────────────────────────────────────────────
# 漂移检测
# ────────────────────────────────────────────────────────


def compute_drift(current: dict, baseline: dict) -> dict:
    """计算当前指纹相对基线的漂移程度"""
    drift = {}
    dimensions = [
        "avg_sentence_len", "code_ratio", "emoji_density",
        "english_ratio", "chinese_ratio", "avg_word_len", "bullet_density",
    ]

    for dim in dimensions:
        b = baseline.get(dim, 0)
        c = current.get(dim, 0)
        if b == 0:
            drift[dim] = 0.0
        else:
            drift[dim] = abs(c - b) / b

    drift["overall"] = sum(drift.get(d, 0) for d in dimensions) / len(dimensions)
    return drift


def establish_baseline(force: bool = False):
    """建立基线指纹"""
    if BASELINE_FILE.exists() and not force:
        print("📌 基线已存在。使用 --force 重新建立")
        return

    transcripts = get_recent_transcripts(5)
    if not transcripts:
        print("❌ 没有找到会话记录")
        return

    # 合并所有 transcripts
    combined = "\n".join(transcripts)
    agent_text = _extract_agent_responses(combined)
    fp = extract_fingerprint(agent_text)

    baseline = {
        "established_at": datetime.now(timezone.utc).isoformat(),
        "source_sessions": len(transcripts),
        "fingerprint": fp,
    }

    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2, ensure_ascii=False))

    print(f"📌 基线已建立 ({len(transcripts)} 个会话):")
    for k, v in fp.items():
        if k not in ("chars_total", "sentence_count"):
            print(f"   {k}: {v}")


def check_drift() -> dict:
    """检查当前漂移"""
    if not BASELINE_FILE.exists():
        return {"error": "未建立基线。运行: hermes identity baseline"}

    baseline = json.loads(BASELINE_FILE.read_text())
    transcripts = get_recent_transcripts(3)

    if not transcripts:
        return {"error": "没有最近的会话记录"}

    combined = "\n".join(transcripts)
    agent_text = _extract_agent_responses(combined)
    current = extract_fingerprint(agent_text)
    drift = compute_drift(current, baseline["fingerprint"])

    # 评估
    alerts = []
    for dim, value in drift.items():
        if dim == "overall":
            continue
        if value > DRIFT_CRITICAL:
            alerts.append({"dim": dim, "level": "critical", "drift": round(value, 3)})
        elif value > DRIFT_WARN:
            alerts.append({"dim": dim, "level": "warning", "drift": round(value, 3)})

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall_drift": round(drift["overall"], 3),
        "alerts": alerts,
        "current": current,
        "baseline": baseline["fingerprint"],
        "dimensions": {dim: round(v, 3) for dim, v in drift.items()},
    }

    # 记录
    DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DRIFT_LOG, "a") as f:
        f.write(json.dumps({
            "t": result["timestamp"],
            "overall": result["overall_drift"],
            "alerts": len(alerts),
        }, ensure_ascii=False) + "\n")

    return result


def show_trend():
    """显示漂移趋势"""
    if not DRIFT_LOG.exists():
        print("暂无漂移记录")
        return

    entries = []
    with open(DRIFT_LOG) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue

    if not entries:
        print("暂无数据")
        return

    print(f"📈 身份漂移趋势 (最近 {len(entries)} 次):\n")

    for e in entries[-20:]:
        ts = e.get("t", "?")[:19]
        drift = e.get("overall", 0)
        alerts = e.get("alerts", 0)

        # 可视化条
        bar_len = int(min(drift, 1.0) * 30)
        bar = "█" * bar_len + "░" * (30 - bar_len)
        alert_str = f" ⚠️ x{alerts}" if alerts > 0 else ""

        status = "🟢" if drift < DRIFT_WARN else ("🟡" if drift < DRIFT_CRITICAL else "🔴")
        print(f"  {status} {ts} │{bar}│ {drift:.3f}{alert_str}")

    # 基线参考
    print(f"\n  基线: DRIFT_WARN={DRIFT_WARN} DRIFT_CRITICAL={DRIFT_CRITICAL}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Identity Drift Detector")
    sub = parser.add_subparsers(dest="command")

    bl_p = sub.add_parser("baseline", help="建立基线指纹")
    bl_p.add_argument("--force", action="store_true")

    sub.add_parser("check", help="检查当前漂移")
    sub.add_parser("trend", help="显示趋势")
    sub.add_parser("alert", help="漂移告警")

    args = parser.parse_args()

    if args.command == "baseline":
        establish_baseline(force=args.force)
    elif args.command == "check":
        result = check_drift()
        if "error" in result:
            print(f"❌ {result['error']}")
            return

        status = "🟢" if result["overall_drift"] < DRIFT_WARN else \
                 ("🟡" if result["overall_drift"] < DRIFT_CRITICAL else "🔴")
        print(f"{status} 整体漂移: {result['overall_drift']:.3f}\n")

        print("维度详情:")
        for dim, value in result.get("dimensions", {}).items():
            if dim == "overall":
                continue
            b = result["baseline"].get(dim, 0)
            c = result["current"].get(dim, 0)
            status = "🟢" if value < DRIFT_WARN else \
                     ("🟡" if value < DRIFT_CRITICAL else "🔴")
            print(f"  {status} {dim:<20} {b:.3f} → {c:.3f}  (Δ={value:.3f})")

        if result["alerts"]:
            print(f"\n⚠️  告警 ({len(result['alerts'])} 项):")
            for a in result["alerts"]:
                print(f"  - [{a['level']}] {a['dim']}: {a['drift']:.3f}")

    elif args.command == "trend":
        show_trend()

    elif args.command == "alert":
        result = check_drift()
        if "error" in result:
            print(f"ℹ️  {result['error']}")
            return
        if result["alerts"]:
            for a in result["alerts"]:
                print(f"⚠️  [{a['level']}] {a['dim']}: 漂移 {a['drift']:.3f}")
        else:
            print(f"✅ 身份稳定 (漂移: {result['overall_drift']:.3f})")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()