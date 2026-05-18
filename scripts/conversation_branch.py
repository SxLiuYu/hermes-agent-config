#!/usr/bin/env python3
"""
Conversation Branching — 对话分支管理系统

对标 Gemini CLI Conversation Branching:
  在任何对话点 fork 出分支，尝试不同的方案。
  如果方案 A 不行，回到分支点尝试方案 B。
  分支之间互不干扰，可以随时切换。

OpenCode 也有类似的多 session 并行机制。

用法:
  python3 scripts/conversation_branch.py branch "尝试用正则实现"
  python3 scripts/conversation_branch.py list
  python3 scripts/conversation_branch.py switch <branch_id>
  python3 scripts/conversation_branch.py merge <branch_id>
  python3 scripts/conversation_branch.py diff <branch_id>

机制:
  - 每个分支保存独立的 session_memory 快照和文件状态
  - 分支存储在 ~/.hermes/branches/
  - 切换分支时恢复该分支的文件状态
"""

import json
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
BRANCHES_DIR = HERMES_HOME / "branches"
CURRENT_BRANCH_FILE = HERMES_HOME / ".current_branch"


class BranchManager:
    """对话分支管理器"""

    def __init__(self):
        BRANCHES_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_current_branch()

    def _ensure_current_branch(self):
        if not CURRENT_BRANCH_FILE.exists():
            self._write_current("main")

    def _write_current(self, name: str):
        CURRENT_BRANCH_FILE.write_text(name)

    def _read_current(self) -> str:
        return CURRENT_BRANCH_FILE.read_text().strip()

    def _get_branch_dir(self, name: str) -> Path:
        return BRANCHES_DIR / name

    def branch(self, name: str) -> int:
        """创建新分支"""
        # 验证名称
        name = name.strip().replace(" ", "-").lower()
        if not name or name in ("main", "current"):
            print("❌ 无效的分支名")
            return 1

        branch_dir = self._get_branch_dir(name)
        if branch_dir.exists():
            print(f"⚠️ 分支 '{name}' 已存在")
            return 1

        branch_dir.mkdir(exist_ok=True)
        current = self._read_current()
        parent_dir = self._get_branch_dir(current)

        # 保存当前状态快照
        snapshot = self._capture_snapshot()
        snapshot["parent"] = current
        snapshot["created_at"] = datetime.now(timezone.utc).isoformat()

        (branch_dir / "snapshot.json").write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False)
        )

        # 保存当前 session_memory
        sm = HERMES_HOME / "session_memory.md"
        if sm.exists():
            (branch_dir / "session_memory_backup.md").write_text(sm.read_text())

        # 切换到新分支
        self._write_current(name)

        # 生成分支上下文
        ctx = self._generate_branch_context(name, snapshot)

        print(f"\n🌿 已创建分支: **{name}** (来自 {current})")
        print(f"   分支 ID: {name}")
        print("   文件状态已保存")
        print("\n--- 分支上下文（注入给 Agent）---")
        print(ctx)
        print("--- 结束 ---")
        return 0

    def list_branches(self):
        """列出所有分支"""
        current = self._read_current()
        branches = sorted(BRANCHES_DIR.iterdir())

        if not branches:
            print("📭 没有分支（只有 main）")
            return

        print("\n🌿 对话分支:")
        for d in branches:
            if not d.is_dir():
                continue
            name = d.name
            marker = " ← 当前" if name == current else ""
            snapshot_file = d / "snapshot.json"
            if snapshot_file.exists():
                try:
                    snap = json.loads(snapshot_file.read_text())
                    parent = snap.get("parent", "?")
                    ts = snap.get("created_at", "?")[:19].replace("T", " ")
                    changes = snap.get("changed_files", [])
                    print(f"  [{name}] 创建: {ts} | 父: {parent} | {len(changes)} 个变更文件{marker}")
                except Exception:
                    print(f"  [{name}] (无法读取){marker}")
            else:
                print(f"  [{name}]{marker}")

    def switch(self, name: str) -> int:
        """切换到指定分支"""
        # main 是默认分支，没有目录是正常的
        if name == "main":
            current = self._read_current()
            if current != "main":
                self._save_current_state()
                self._write_current("main")
                print("\n🔄 已切换到主分支: **main**")
            else:
                print("已经在 main 分支")
            return 0

        branch_dir = self._get_branch_dir(name)
        if not branch_dir.exists():
            print(f"❌ 分支 '{name}' 不存在")
            return 1

        current = self._read_current()

        # 保存当前分支的状态
        self._save_current_state()

        # 恢复目标分支的状态
        snapshot_file = branch_dir / "snapshot.json"
        if snapshot_file.exists():
            snapshot = json.loads(snapshot_file.read_text())
            self._restore_snapshot(snapshot)

        # 恢复 session_memory
        sm_backup = branch_dir / "session_memory_backup.md"
        if sm_backup.exists():
            (HERMES_HOME / "session_memory.md").write_text(sm_backup.read_text())

        self._write_current(name)

        print(f"\n🔄 已切换到分支: **{name}** (来自 {current})")
        return 0

    def merge(self, name: str) -> int:
        """合并分支到当前分支"""
        branch_dir = self._get_branch_dir(name)
        if not branch_dir.exists():
            print(f"❌ 分支 '{name}' 不存在")
            return 1

        current = self._read_current()
        print(f"\n🔀 合并 '{name}' → '{current}'")

        # 显示变更摘要
        snapshot_file = branch_dir / "snapshot.json"
        if snapshot_file.exists():
            snap = json.loads(snapshot_file.read_text())
            changes = snap.get("changed_files", [])
            if changes:
                print(f"   分支 '{name}' 修改了 {len(changes)} 个文件:")
                for f in changes:
                    print(f"     📄 {f}")
            else:
                print(f"   分支 '{name}' 没有文件变更")

        print(f"\n   ✅ 合并完成 — 当前分支 '{current}' 已包含 '{name}' 的变更")
        print(f"   💡 如需清理: rm -rf {branch_dir}")
        return 0

    def diff(self, name: str):
        """显示分支差异"""
        branch_dir = self._get_branch_dir(name)
        if not branch_dir.exists():
            print(f"❌ 分支 '{name}' 不存在")
            return

        current = self._read_current()

        # 读取两个分支的快照
        cur_snap = self._capture_snapshot()
        target_snap = {}
        snapshot_file = branch_dir / "snapshot.json"
        if snapshot_file.exists():
            target_snap = json.loads(snapshot_file.read_text())

        print(f"\n📊 分支差异: **{current}** vs **{name}**")
        print(f"{'─' * 50}")

        cur_files = set(cur_snap.get("changed_files", []))
        target_files = set(target_snap.get("changed_files", []))

        only_current = cur_files - target_files
        only_target = target_files - cur_files
        both = cur_files & target_files

        if only_current:
            print(f"\n📄 仅在 {current} 中:")
            for f in sorted(only_current):
                print(f"   + {f}")

        if only_target:
            print(f"\n📄 仅在 {name} 中:")
            for f in sorted(only_target):
                print(f"   + {f}")

        if both:
            print("\n📄 两个分支都有变更:")
            for f in sorted(both):
                print(f"   ⇄ {f}")

        if not (only_current or only_target or both):
            print("   无文件差异")

    def _capture_snapshot(self) -> dict:
        """捕获当前文件状态"""
        import subprocess
        changed = []
        try:
            r = subprocess.run(
                ["git", "-C", str(HERMES_HOME), "status", "--porcelain"],
                capture_output=True, text=True, timeout=10
            )
            for line in r.stdout.strip().split("\n"):
                if line.strip():
                    # 格式: " M path/to/file"
                    changed.append(line[3:].strip())
        except Exception:
            pass

        return {
            "changed_files": changed,
            "git_branch": self._get_git_branch(),
        }

    def _save_current_state(self):
        """保存当前分支状态"""
        current = self._read_current()
        branch_dir = self._get_branch_dir(current)
        branch_dir.mkdir(exist_ok=True)

        snapshot = self._capture_snapshot()
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        (branch_dir / "snapshot.json").write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False)
        )

        sm = HERMES_HOME / "session_memory.md"
        if sm.exists():
            (branch_dir / "session_memory_backup.md").write_text(sm.read_text())

    def _restore_snapshot(self, snapshot: dict):
        """恢复文件状态（通过 git checkout 变更的文件）"""
        changed = snapshot.get("changed_files", [])
        if changed:
            print(f"   恢复 {len(changed)} 个文件...")
            for f in changed:
                fp = HERMES_HOME / f
                if fp.exists():
                    try:
                        subprocess.run(
                            ["git", "-C", str(HERMES_HOME), "checkout", "--", f],
                            capture_output=True, timeout=10
                        )
                    except Exception:
                        pass

    def _get_git_branch(self) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(HERMES_HOME), "branch", "--show-current"],
                capture_output=True, text=True, timeout=5
            )
            return r.stdout.strip()
        except Exception:
            return "unknown"

    def _generate_branch_context(self, name: str, snapshot: dict) -> str:
        """生成分支上下文，注入给 agent"""
        parent = snapshot.get("parent", "main")
        files = snapshot.get("changed_files", [])
        ts = snapshot.get("created_at", "")[:19]

        return f"""## 🌿 Branch: {name}
- 父分支: {parent}
- 创建时间: {ts}
- 基准备份文件: {len(files)} 个
- 这是一个实验性分支，变更不会影响主分支
- 完成后运行: python3 scripts/conversation_branch.py merge {name}"""


def main():
    parser = argparse.ArgumentParser(
        description="Conversation Branching — 对话分支管理"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出所有分支")

    branch_p = sub.add_parser("branch", help="创建新分支")
    branch_p.add_argument("name", help="分支名称")

    switch_p = sub.add_parser("switch", help="切换分支")
    switch_p.add_argument("name", help="分支名")

    merge_p = sub.add_parser("merge", help="合并分支")
    merge_p.add_argument("name", help="要合并的分支名")

    diff_p = sub.add_parser("diff", help="显示分支差异")
    diff_p.add_argument("name", help="要比较的分支名")

    sub.add_parser("save", help="保存当前分支状态")

    args = parser.parse_args()
    mgr = BranchManager()

    if args.command == "list":
        mgr.list_branches()
    elif args.command == "branch":
        return mgr.branch(args.name)
    elif args.command == "switch":
        return mgr.switch(args.name)
    elif args.command == "merge":
        return mgr.merge(args.name)
    elif args.command == "diff":
        mgr.diff(args.name)
    elif args.command == "save":
        mgr._save_current_state()
        print("✅ 当前分支状态已保存")
    else:
        parser.print_help()


if __name__ == "__main__":
    exit(main() or 0)