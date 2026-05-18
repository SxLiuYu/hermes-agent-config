#!/usr/bin/env python3
"""
Pre-Session 动态上下文注入

对标 Claude Code PreSession Hook:
  在每个新 session 开始时，自动注入项目特定的上下文。
  包括：当前项目状态、最近变更、活跃目标等。

触发: SessionStart hook (已注册)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"


def get_active_goals() -> str:
    """获取当前活跃的 /goal"""
    goals_dir = HERMES_HOME / "goals"
    if not goals_dir.exists():
        return ""

    parts = []
    for gfile in sorted(goals_dir.glob("*.json")):
        try:
            data = json.loads(gfile.read_text())
            if data.get("paused"):
                continue
            goal = data.get("goal", "")
            subgoals = data.get("subgoals", [])
            turns = data.get("turns_used", 0)
            max_t = data.get("max_turns", 20)
            parts.append(f"🎯 {goal} ({turns}/{max_t} turns)")
            for sg in subgoals:
                parts.append(f"   ✓ {sg}")
        except Exception:
            pass

    return "\n".join(parts) if parts else ""


def get_recent_changes() -> str:
    """获取最近的文件变更摘要"""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", str(HERMES_HOME), "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()
    except Exception:
        return ""


def get_project_context() -> str:
    """构建动态项目上下文"""
    lines = []

    # 活跃目标
    goals = get_active_goals()
    if goals:
        lines.append(f"## 活跃目标\n{goals}")

    # 最近变更
    changes = get_recent_changes()
    if changes:
        lines.append(f"## 最近变更\n{changes}")

    # 上次会话摘要
    recap_file = HERMES_HOME / "logs" / "session_recaps.md"
    if recap_file.exists():
        content = recap_file.read_text()
        # 提取最近的 recaps（最多 2 个）
        recaps = content.split("## 🧠")[1:3]  # 跳过第一个空分割
        if recaps:
            lines.append(f"## 上次会话摘要\n{chr(10).join(r[:500] for r in recaps)}")

    return "\n\n".join(lines) if lines else ""


if __name__ == "__main__":
    ctx = get_project_context()
    if ctx:
        ts = datetime.now(timezone.utc).strftime("%H:%M")
        print(f"[Session context loaded at {ts}]\n{ctx}")