#!/usr/bin/env python3
"""
Session Recap — 会话摘要生成器

对标 Claude Code Session Recap + Gemini CLI Checkpointing:
  当用户在终端切换焦点回来时，快速了解刚才 agent 做了什么。
  支持保存/恢复复杂会话状态。

用法:
  python3 scripts/session_recap.py summary          # 生成最近总结
  python3 scripts/session_recap.py checkpoint save  # 保存当前状态
  python3 scripts/session_recap.py checkpoint list  # 列出检查点
  python3 scripts/session_recap.py checkpoint restore <id>  # 恢复检查点
"""

import json
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
CHECKPOINT_DIR = HERMES_HOME / "checkpoints"
RECAP_FILE = HERMES_HOME / "logs" / "session_recaps.md"


def generate_summary():
    """从 session_memory 和最新日志生成快速摘要"""
    parts = []

    # 1. Session Memory 摘要
    sm_file = HERMES_HOME / "session_memory.md"
    if sm_file.exists():
        lines = sm_file.read_text().split("\n")
        # 提取 Current State
        in_state = False
        for line in lines[:50]:
            if "Current State" in line:
                in_state = True
                continue
            if in_state and line.startswith("##"):
                break
            if in_state and line.strip():
                # 简化：提取最近 session 文件路径
                if "session_" in line or "transcript" in line.lower():
                    parts.append(f"📄 {line.strip()}")

    # 2. 文件变更摘要
    diff_file = HERMES_HOME / "logs" / "diff_summary.md"
    if diff_file.exists():
        diff_lines = diff_file.read_text().split("\n")[:10]
        parts.extend(diff_lines)

    # 3. 活跃 hook 状态
    hooks_dir = HERMES_HOME / "hooks"
    active_hooks = []
    for event_dir in hooks_dir.iterdir():
        if event_dir.is_dir():
            scripts = list(event_dir.glob("*.sh")) + list(event_dir.glob("*.py"))
            if scripts:
                active_hooks.append(f"  {event_dir.name}: {len(scripts)} scripts")
    if active_hooks:
        parts.append("🪝 活跃 Hooks:\n" + "\n".join(active_hooks))

    # 4. 最近工具调用
    session_file = HERMES_HOME / "session_memory.md"
    if session_file.exists():
        content = session_file.read_text()
        # 提取 Worklog
        in_wl = False
        wl_lines = []
        for line in content.split("\n"):
            if "Worklog" in line:
                in_wl = True
                continue
            if in_wl and line.startswith("##"):
                break
            if in_wl and line.strip() and len(wl_lines) < 5:
                wl_lines.append(line.strip())
        if wl_lines:
            parts.append("📋 最近操作:\n" + "\n".join(wl_lines))

    if not parts:
        return "🆕 没有最近活动"

    return "\n\n".join(parts)


def save_checkpoint(label: str = ""):
    """保存当前会话检查点"""
    import subprocess

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    # 保存文件变更状态
    git_status = ""
    try:
        r = subprocess.run(["git", "-C", str(HERMES_HOME), "status", "--porcelain"],
                         capture_output=True, text=True, timeout=10)
        git_status = r.stdout.strip()
    except Exception:
        git_status = "(无法获取 git 状态)"

    # 保存 session memory
    session_content = ""
    sm_file = HERMES_HOME / "session_memory.md"
    if sm_file.exists():
        session_content = sm_file.read_text()[:5000]

    checkpoint = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "git_status": git_status,
        "session_memory_snapshot": session_content,
        "memory_snapshot": {
            "memory_md": (HERMES_HOME / "memory" / "memory.md").read_text()[:3000]
            if (HERMES_HOME / "memory" / "memory.md").exists() else "",
        },
    }

    idx = len(list(CHECKPOINT_DIR.glob("*.json"))) + 1
    cp_file = CHECKPOINT_DIR / f"checkpoint_{idx:04d}.json"
    cp_file.write_text(json.dumps(checkpoint, indent=2, ensure_ascii=False))

    print(f"💾 检查点已保存: checkpoint_{idx:04d}.json")
    if label:
        print(f"   标签: {label}")
    print(f"   Git 变更: {len(git_status.split(chr(10))) if git_status else 0} 个文件")


def list_checkpoints():
    """列出所有检查点"""
    cps = sorted(CHECKPOINT_DIR.glob("checkpoint_*.json"))
    if not cps:
        print("📭 没有检查点")
        return

    for cp in cps:
        try:
            data = json.loads(cp.read_text())
            ts = data.get("timestamp", "?")[:19]
            label = data.get("label", "")
            changes = len((data.get("git_status", "") or "").split("\n"))
            print(f"  {cp.name}: {ts} | {label} | {changes} files changed")
        except Exception:
            print(f"  {cp.name}: (无法读取)")


def restore_checkpoint(idx: int):
    """恢复检查点"""
    cp_file = CHECKPOINT_DIR / f"checkpoint_{idx:04d}.json"
    if not cp_file.exists():
        print(f"❌ 检查点 checkpoint_{idx:04d}.json 不存在")
        return

    data = json.loads(cp_file.read_text())
    label = data.get("label", "")
    ts = data.get("timestamp", "?")[:19]

    print(f"""
📋 检查点 {idx} — {ts}
   标签: {label or '无'}
   
   要恢复的内容:
   1. Session Memory 快照 ({len(data.get('session_memory_snapshot', ''))} 字符)
   2. Memory 快照 ({len(data.get('memory_snapshot', {}).get('memory_md', ''))} 字符)
   3. Git 状态: {data.get('git_status', '未知')}

   ⚠️ 恢复操作会覆盖当前状态，请确认后在 Hermes 中执行:
      "恢复检查点 {idx} 的 session_memory 和 memory 文件"
""")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Session Recap & Checkpoint Manager")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("summary", help="生成会话摘要")

    cp = sub.add_parser("checkpoint", help="检查点操作")
    cp.add_argument("action", choices=["save", "list", "restore"])
    cp.add_argument("arg", nargs="?", help="标签或检查点 ID")

    args = parser.parse_args()

    if args.command == "summary":
        recap = generate_summary()
        # 保存到文件
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = f"## 🧠 Session Recap — {ts}\n\n{recap}\n\n---\n"
        RECAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RECAP_FILE, "a") as f:
            f.write(entry)
        print(entry)

    elif args.command == "checkpoint":
        if args.action == "save":
            save_checkpoint(args.arg or "")
        elif args.action == "list":
            list_checkpoints()
        elif args.action == "restore":
            if not args.arg:
                print("❌ 需要指定检查点 ID")
            else:
                restore_checkpoint(int(args.arg))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()