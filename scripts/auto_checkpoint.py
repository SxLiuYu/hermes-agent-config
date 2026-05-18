#!/usr/bin/env python3
"""
Auto-Checkpoint Hook — 文件变更前自动保存状态

对标 Gemini CLI Auto-Checkpoint:
  在执行任何文件修改工具（write_file、patch）之前，自动创建检查点。
  错误修改后可以一键恢复，像轻量级 Git revert。

Gemini CLI 的做法:
  - 在 write_file 前自动保存快照
  - 用 /restore 命令恢复到检查点
  - 检查点保存在 .gemini/checkpoints/ 下

触发: PreToolUse hook (hook 系统检测到 write_file/patch 调用时)

用法:
  python3 scripts/auto_checkpoint.py status
  python3 scripts/auto_checkpoint.py restore <checkpoint_id>
  python3 scripts/auto_checkpoint.py list
"""

import json
import shutil
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERMES_HOME = Path.home() / ".hermes"
CHECKPOINTS_DIR = HERMES_HOME / "checkpoints"


class AutoCheckpoint:
    """自动检查点管理器"""

    def __init__(self):
        CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)

    def create(self, tool_name: str, target_file: str = None) -> Optional[str]:
        """在文件修改前创建检查点"""
        cp_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        cp_dir = CHECKPOINTS_DIR / cp_id
        cp_dir.mkdir(exist_ok=True)

        snapshot = {
            "id": cp_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "target_file": target_file,
            "files_backed_up": [],
        }

        # 备份目标文件
        if target_file:
            fp = Path(target_file).expanduser()
            if fp.exists():
                backup = cp_dir / "target_file_backup"
                shutil.copy2(fp, backup)
                snapshot["files_backed_up"].append(str(fp))

        # 备份 session_memory
        sm = HERMES_HOME / "session_memory.md"
        if sm.exists():
            shutil.copy2(sm, cp_dir / "session_memory.md")
            snapshot["files_backed_up"].append("session_memory.md")

        # 保存快照元数据
        (cp_dir / "snapshot.json").write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False)
        )

        return cp_id

    def list_checkpoints(self, limit: int = 10):
        """列出最近的检查点"""
        checkpoints = sorted(CHECKPOINTS_DIR.iterdir(), reverse=True)
        if not checkpoints:
            print("📭 没有检查点")
            return []

        result = []
        print(f"\n📸 最近的检查点 ({min(limit, len(checkpoints))}/{len(checkpoints)}):")
        for d in checkpoints[:limit]:
            if not d.is_dir():
                continue
            sf = d / "snapshot.json"
            if sf.exists():
                try:
                    snap = json.loads(sf.read_text())
                    ts = snap.get("timestamp", "?")[:19].replace("T", " ")
                    tool = snap.get("tool", "?")
                    target = snap.get("target_file", "")
                    files = snap.get("files_backed_up", [])
                    print(f"  [{snap['id']}] {ts} | {tool} → {target}")
                    result.append(snap)
                except Exception:
                    print(f"  [{d.name}] (无法读取)")

        return result

    def restore(self, cp_id: str) -> int:
        """恢复到指定检查点"""
        cp_dir = CHECKPOINTS_DIR / cp_id
        if not cp_dir.exists():
            print(f"❌ 检查点 '{cp_id}' 不存在")
            return 1

        snapshot_file = cp_dir / "snapshot.json"
        if not snapshot_file.exists():
            print(f"❌ 检查点 '{cp_id}' 元数据丢失")
            return 1

        snap = json.loads(snapshot_file.read_text())
        files = snap.get("files_backed_up", [])
        target = snap.get("target_file", "")

        print(f"\n🔙 恢复检查点: {cp_id}")
        print(f"   时间: {snap.get('timestamp', '?')[:19]}")
        print(f"   触发工具: {snap.get('tool', '?')}")

        restored = 0
        for f in files:
            backup = cp_dir / f if f != "session_memory.md" else cp_dir / "session_memory.md"
            backup_alt = cp_dir / "target_file_backup"

            # 先检查原始命名备份
            if backup.exists():
                dest = HERMES_HOME / f if f == "session_memory.md" else Path(f)
                shutil.copy2(backup, dest)
                print(f"   ✅ 恢复: {f}")
                restored += 1
            # 再检查 target_file_backup
            elif backup_alt.exists() and f != "session_memory.md":
                shutil.copy2(backup_alt, Path(f))
                print(f"   ✅ 恢复: {f}")
                restored += 1
            else:
                print(f"   ⚠️ 备份丢失: {f}")

        if restored == 0 and target:
            # fallback: git checkout
            try:
                subprocess.run(
                    ["git", "-C", str(Path(target).parent), "checkout", "--",
                     Path(target).name],
                    capture_output=True, timeout=10
                )
                print(f"   ✅ 通过 git checkout 恢复: {target}")
            except Exception:
                print(f"   ❌ 无法恢复: {target}")

        print(f"\n   共恢复 {restored} 个文件")
        return 0

    def cleanup(self, keep: int = 20):
        """清理旧检查点，保留最近 N 个"""
        checkpoints = sorted(CHECKPOINTS_DIR.iterdir(), reverse=True)
        removed = 0
        for d in checkpoints[keep:]:
            if d.is_dir():
                shutil.rmtree(d)
                removed += 1
        if removed:
            print(f"🧹 已清理 {removed} 个旧检查点（保留最近 {keep} 个）")


def generate_state_diff(cp_id: str) -> str:
    """生成检查点前后差异（给 agent 做决策参考）"""
    cp_dir = CHECKPOINTS_DIR / cp_id
    snapshot_file = cp_dir / "snapshot.json"
    if not snapshot_file.exists():
        return ""

    snap = json.loads(snapshot_file.read_text())
    target = snap.get("target_file", "")
    if not target or not Path(target).exists():
        return ""

    backup = cp_dir / "target_file_backup"
    if not backup.exists():
        return ""

    try:
        r = subprocess.run(
            ["diff", "-u", str(backup), str(target)],
            capture_output=True, text=True, timeout=10
        )
        return r.stdout[:2000]  # 截断
    except Exception:
        return "(无法生成 diff)"


def main():
    parser = argparse.ArgumentParser(
        description="Auto-Checkpoint — 文件变更前自动保存状态"
    )
    sub = parser.add_subparsers(dest="command")

    create_p = sub.add_parser("create", help="创建检查点")
    create_p.add_argument("--tool", default="unknown", help="触发工具")
    create_p.add_argument("--file", help="目标文件")

    sub.add_parser("list", help="列出检查点")

    list_p = sub.add_parser("restore", help="恢复检查点")
    list_p.add_argument("checkpoint_id", help="检查点 ID")

    sub.add_parser("cleanup", help="清理旧检查点")

    sub.add_parser("install-hook", help="安装 PreToolUse hook")

    args = parser.parse_args()
    ac = AutoCheckpoint()

    if args.command == "create":
        cp_id = ac.create(args.tool, args.file)
        if cp_id:
            print(json.dumps({"checkpoint_id": cp_id, "status": "created"}))
    elif args.command == "list":
        ac.list_checkpoints()
    elif args.command == "restore":
        return ac.restore(args.checkpoint_id)
    elif args.command == "cleanup":
        ac.cleanup()
    elif args.command == "install-hook":
        install_hook()
    else:
        parser.print_help()


def install_hook():
    """安装 PreToolUse hook 来自动创建检查点"""
    hook_dir = HERMES_HOME / "hooks" / "pre_tool_use"
    hook_dir.mkdir(parents=True, exist_ok=True)

    hook_script = hook_dir / "auto-checkpoint.sh"
    content = """#!/bin/bash
# Auto-Checkpoint: 在 write_file/patch 前自动保存状态
# 对标 Gemini CLI 的行为

TOOL_NAME="$1"
TARGET_FILE="$2"

case "$TOOL_NAME" in
    write_file|patch|terminal*rm*|terminal*mv*)
        # 创建检查点（静默，不阻塞）
        python3 "$HOME/.hermes/scripts/auto_checkpoint.py" create \\
            --tool "$TOOL_NAME" --file "$TARGET_FILE" > /dev/null 2>&1
        ;;
esac

exit 0
"""
    hook_script.write_text(content)
    hook_script.chmod(0o755)
    print("✅ PreToolUse auto-checkpoint hook 已安装")
    print("   每次 write_file/patch/rm 前自动创建检查点")


if __name__ == "__main__":
    exit(main() or 0)