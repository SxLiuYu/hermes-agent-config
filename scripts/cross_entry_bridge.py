#!/usr/bin/env python3
"""
Cross-Entry Bridge v2 — 跨平台消息桥接
=======================================
从 state.db 提取各平台的最近会话，生成上下文摘要。

v2 新增:
  - 智能摘要: 用 oMLX 本地模型压缩多条消息，代替原始拼接
  - 去重: 相同内容跨平台自动去重
  - 对话间隙检测: 标记用户最后活跃时间 vs 当前时间
  - event_bus 集成: 生成 bridge_summary 事件
  - 多格式输出: markdown / json / text

用法:
  python3 cross_entry_bridge.py                    # Markdown 输出
  python3 cross_entry_bridge.py --format json      # JSON 输出
  python3 cross_entry_bridge.py --max-age 48       # 回溯 48 小时
  python3 cross_entry_bridge.py --compact          # 极简模式
"""

import sqlite3
import json
import os
import time
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB_PATH = HERMES_HOME / "state.db"
BRIDGE_PATH = HERMES_HOME / "memory" / "cross-entry-bridge.md"
EVENT_STREAM = HERMES_HOME / "event_stream.jsonl"
MAX_AGE_HOURS = 72

# 平台名映射
PLATFORM_EMOJI = {
    "feishu": "📩",
    "weixin": "💬",
    "local": "🖥",
    "telegram": "✈️",
    "discord": "🎮",
}


def fetch_sessions(conn, max_age_hours: int = MAX_AGE_HOURS):
    """获取活跃 session 的最近消息."""
    conn.row_factory = sqlite3.Row

    sessions = conn.execute("""
        SELECT id, source, started_at
        FROM sessions
        WHERE ended_at IS NULL
          AND datetime(started_at, 'unixepoch') > datetime('now', ? || ' hours')
        ORDER BY source, started_at DESC
    """, (f"-{max_age_hours}",)).fetchall()

    # 每平台取最新 session
    seen = set()
    latest = {}
    for s in sessions:
        src = s["source"]
        if src not in seen:
            seen.add(src)
            latest[src] = s["id"]

    if not latest:
        return {}

    # 获取每个平台的消息
    now = time.time()
    results = {}
    seen_content = set()  # 去重

    for src, sid in latest.items():
        messages = conn.execute("""
            SELECT role, content, timestamp
            FROM messages
            WHERE session_id = ? AND timestamp > ?
            ORDER BY timestamp ASC
            LIMIT 30
        """, (sid, now - max_age_hours * 3600)).fetchall()

        if not messages:
            continue

        # 去重
        unique_msgs = []
        for m in messages:
            h = hashlib.md5((m["content"] or "").encode()).hexdigest()[:12]
            if h not in seen_content:
                seen_content.add(h)
                unique_msgs.append(m)

        if unique_msgs:
            last_ts = max(m["timestamp"] for m in unique_msgs)
            results[src] = {
                "messages": unique_msgs,
                "count": len(unique_msgs),
                "last_active": last_ts,
                "gap_hours": round((now - last_ts) / 3600, 1),
            }

    return results


def smart_summarize(messages: list, max_chars: int = 500) -> str:
    """用 oMLX 压缩消息摘要 (fallback: 截断)."""
    text = "\n".join(
        f"[{m['role']}] {m['content'][:200]}" for m in messages[-10:]
    )
    if len(text) <= max_chars:
        return text

    # 尝试 oMLX
    try:
        result = subprocess.run(
            ["omlx", "chat", "--model", "qwen3.5-4b",
             "--prompt", f"将以下对话摘要为 3-5 句中文:\n{text[:2000]}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:max_chars]
    except Exception:
        pass

    # Fallback: 简单截断
    return text[:max_chars] + "..."


def build_markdown(data: dict, compact: bool = False) -> str:
    """生成 Markdown 格式桥接内容."""
    now = datetime.now()
    lines = [
        "# 🌉 Cross-Entry Bridge",
        f"*生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
    ]

    if not data:
        lines.append("> ⚠️ 没有活跃的跨平台会话")
        return "\n".join(lines)

    # 按 gap_hours 排序
    sorted_sources = sorted(
        data.items(),
        key=lambda x: x[1]["gap_hours"],
    )

    for src, info in sorted_sources:
        emoji = PLATFORM_EMOJI.get(src, "📡")
        gap = info["gap_hours"]
        gap_str = f"{gap:.0f}h" if gap >= 1 else f"{int(gap * 60)}min"

        lines.append(f"## {emoji} {src}")
        lines.append(f"**消息数**: {info['count']} | **最后活跃**: {gap_str}前")
        lines.append("")

        if compact:
            # 极简模式: 只显示摘要
            summary = smart_summarize(info["messages"])
            lines.append(f"> {summary[:300]}")
        else:
            for m in info["messages"][-8:]:
                role = "👤" if m["role"] == "user" else "🤖"
                ts = datetime.fromtimestamp(m["timestamp"]).strftime("%H:%M")
                content = (m["content"] or "")[:120].replace("\n", " ")
                lines.append(f"  {role} `{ts}` {content}")

        lines.append("")

    lines.append("---")
    lines.append("*由 cross_entry_bridge v2 生成*")
    return "\n".join(lines)


def emit_event(data: dict):
    """发送 bridge 事件."""
    if not data:
        return
    event = {
        "type": "bridge.synced",
        "sources": list(data.keys()),
        "total_messages": sum(v["count"] for v in data.values()),
        "timestamp": time.time(),
    }
    try:
        with open(EVENT_STREAM, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cross-Entry Bridge v2")
    parser.add_argument("--format", choices=["markdown", "json", "text"], default="markdown")
    parser.add_argument("--max-age", type=int, default=MAX_AGE_HOURS, help="回溯小时数")
    parser.add_argument("--compact", action="store_true", help="极简模式")
    parser.add_argument("--save", action="store_true", help="保存到文件")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("⚠️ state.db 不存在")
        return

    conn = sqlite3.connect(str(DB_PATH))
    data = fetch_sessions(conn, args.max_age)
    conn.close()

    if args.format == "json":
        output = json.dumps({
            "generated_at": datetime.now().isoformat(),
            "sources": {
                src: {"count": info["count"], "gap_hours": info["gap_hours"]}
                for src, info in data.items()
            }
        }, ensure_ascii=False, indent=2)
    elif args.format == "text":
        lines = ["=== Cross-Entry Bridge ==="]
        for src, info in data.items():
            lines.append(f"[{src}] {info['count']} msgs, {info['gap_hours']}h ago")
        output = "\n".join(lines)
    else:
        output = build_markdown(data, args.compact)

    print(output)

    if args.save:
        BRIDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BRIDGE_PATH.write_text(output)
        print(f"\n✅ Saved: {BRIDGE_PATH}")

    emit_event(data)


if __name__ == "__main__":
    main()