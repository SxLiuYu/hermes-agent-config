#!/usr/bin/env python3
"""
Worktree Spawn — Git Worktree 隔离的子 Agent 工作空间

对标 OpenAI Codex Git Worktree:
  每个子 Agent 在自己的 git worktree 中工作，互不干扰。
  完成后的变更可以 diff 审查，通过后合并回主分支。

与普通 delegate_task 的区别:
  - 普通: 子 Agent 直接修改当前目录，出错后需要回滚
  - Worktree: 子 Agent 在独立副本中工作，出错直接删 worktree

用法:
  # 创建隔离工作空间
  python3 scripts/worktree_spawn.py create --task "修复登录bug" [--repo ~/YuanFang]

  # 查看所有 worktree
  python3 scripts/worktree_spawn.py list

  # 查看变更
  python3 scripts/worktree_spawn.py diff <name>

  # 合并变更到主分支
  python3 scripts/worktree_spawn.py merge <name> [--squash]

  # 放弃变更
  python3 scripts/worktree_spawn.py discard <name> [--force]
"""

import json
import shutil
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERMES_HOME = Path.home() / ".hermes"
WORKTREE_DIR = Path.home() / ".hermes" / "worktrees"
REGISTRY_FILE = WORKTREE_DIR / "registry.json"


class WorktreeManager:
    """Git Worktree 隔离管理器"""

    def __init__(self, repo_path: str = None):
        self.repo_path = self._find_repo(repo_path)
        WORKTREE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_registry()

    def _find_repo(self, repo_path: str = None) -> Optional[Path]:
        """找到 git 仓库根目录"""
        if repo_path:
            rp = Path(repo_path).expanduser()
            if (rp / ".git").exists():
                return rp

        # 从当前目录向上找
        cwd = Path.cwd()
        for p in [cwd] + list(cwd.parents):
            if (p / ".git").exists():
                return p

        return None

    def _load_registry(self):
        if REGISTRY_FILE.exists():
            try:
                self.registry = json.loads(REGISTRY_FILE.read_text())
            except Exception:
                self.registry = {"worktrees": {}}
        else:
            self.registry = {"worktrees": {}}

    def _save_registry(self):
        REGISTRY_FILE.write_text(json.dumps(self.registry, indent=2, ensure_ascii=False))

    def create(self, task: str, branch: str = None, base_branch: str = None) -> Optional[str]:
        """创建隔离 worktree"""
        if not self.repo_path:
            print("❌ 当前目录不是 git 仓库，无法使用 worktree 隔离")
            return None

        # 生成名称
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        name = branch or f"hermes-{task.replace(' ', '-').lower()[:30]}-{ts}"

        # 清理名称
        name = name.replace("/", "-").replace("\\", "-")[:50]

        wt_path = WORKTREE_DIR / name

        # 确定基础分支
        if not base_branch:
            try:
                r = subprocess.run(
                    ["git", "-C", str(self.repo_path), "branch", "--show-current"],
                    capture_output=True, text=True, timeout=5
                )
                base_branch = r.stdout.strip() or "main"
            except Exception:
                base_branch = "main"

        print("\n🔀 创建 Git Worktree 隔离环境...")
        print(f"   仓库: {self.repo_path}")
        print(f"   基础分支: {base_branch}")
        print(f"   Worktree 路径: {wt_path}")

        # 创建 worktree（-b 创建新分支，避免"分支已被占用"错误）
        try:
            subprocess.run(
                ["git", "-C", str(self.repo_path), "worktree", "add",
                 "-b", name, str(wt_path), base_branch],
                check=True, capture_output=True, text=True, timeout=30
            )
            print(f"   ✅ Worktree 创建成功 (分支: {name})")
        except subprocess.CalledProcessError as e:
            print(f"   ❌ 创建失败: {e.stderr}")
            return None

        # 记录到注册表
        self.registry["worktrees"][name] = {
            "name": name,
            "task": task,
            "repo": str(self.repo_path),
            "path": str(wt_path),
            "base_branch": base_branch,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
        }
        self._save_registry()

        # 输出工作空间路径和提示
        print(f"\n📂 隔离工作空间: {wt_path}")
        print(f"📋 任务: {task}")
        print("\n💡 使用方式:")
        print(f"   1. 让 Agent 在 {wt_path} 中工作")
        print(f"   2. 完成后审查: python3 scripts/worktree_spawn.py diff {name}")
        print(f"   3. 合并: python3 scripts/worktree_spawn.py merge {name}")
        print(f"   4. 或放弃: python3 scripts/worktree_spawn.py discard {name}")

        return name

    def list_worktrees(self):
        """列出所有 worktree"""
        wt_entries = self.registry.get("worktrees", {})
        if not wt_entries:
            # 也检查 git worktree list
            try:
                r = subprocess.run(
                    ["git", "-C", str(self.repo_path), "worktree", "list"],
                    capture_output=True, text=True, timeout=5
                )
                print("\n🌳 Git Worktrees (git 原生):")
                for line in r.stdout.strip().split("\n"):
                    if str(WORKTREE_DIR) in line:
                        print(f"   {line}")
            except Exception:
                pass

            if not wt_entries:
                print("📭 没有活动的 worktree")
                return

        print(f"\n🌳 注册的 Worktrees ({len(wt_entries)}):")
        for name, info in sorted(wt_entries.items(),
                                 key=lambda x: x[1].get("created_at", ""),
                                 reverse=True):
            task = info.get("task", "?")
            status = info.get("status", "?")
            ts = info.get("created_at", "?")[:19]
            path = info.get("path", "?")

            status_icon = {"active": "🟢", "merged": "🔵", "discarded": "⚫"}.get(status, "⚪")
            exists = Path(path).exists() if path != "?" else False
            exist_mark = "✓" if exists else "✗"

            print(f"  [{name}] {status_icon} {status} | {task} | {ts} | {exist_mark}")

    def diff(self, name: str) -> int:
        """查看 worktree 的变更"""
        info = self.registry["worktrees"].get(name)
        if not info:
            print(f"❌ Worktree '{name}' 不在注册表中")
            return 1

        wt_path = Path(info["path"])
        if not wt_path.exists():
            print(f"❌ Worktree 路径不存在: {wt_path}")
            return 1

        try:
            # git diff
            r = subprocess.run(
                ["git", "-C", str(wt_path), "diff", "--stat", info["base_branch"]],
                capture_output=True, text=True, timeout=10
            )

            stat = r.stdout.strip()
            print(f"\n📊 变更统计 (vs {info['base_branch']}):")
            if stat:
                print(stat)
            else:
                print("   (无变更)")

            # 详细 diff（限制长度）
            r2 = subprocess.run(
                ["git", "-C", str(wt_path), "diff", "--", ":(exclude)*.pyc",
                 ":(exclude)__pycache__", ":(exclude)*.log"],
                capture_output=True, text=True, timeout=10
            )
            diff_text = r2.stdout
            if diff_text:
                print("\n📝 详细变更 (前 200 行):")
                lines = diff_text.split("\n")[:200]
                print("\n".join(lines))
                if len(diff_text.split("\n")) > 200:
                    print(f"\n... (共 {len(diff_text.split(chr(10)))} 行，已截断)")
            else:
                print("   (无变更)")

            # 具体的 commit 信息
            r3 = subprocess.run(
                ["git", "-C", str(wt_path), "log", "--oneline",
                 f"{info['base_branch']}..HEAD", "-10"],
                capture_output=True, text=True, timeout=5
            )
            commits = r3.stdout.strip()
            if commits:
                print("\n📋 Commits:")
                print(commits)

        except subprocess.CalledProcessError as e:
            print(f"   ⚠️ diff 失败: {e.stderr}")
            return 1

        return 0

    def merge(self, name: str, squash: bool = False) -> int:
        """合并 worktree 变更到主分支"""
        info = self.registry["worktrees"].get(name)
        if not info:
            print(f"❌ Worktree '{name}' 不在注册表中")
            return 1

        wt_path = Path(info["path"])
        repo = Path(info["repo"])
        base = info["base_branch"]

        if not wt_path.exists():
            print(f"❌ Worktree 路径不存在: {wt_path}")
            return 1

        # 先展示 diff
        print(f"\n🔀 准备合并 '{name}' → '{base}'")
        self.diff(name)

        # 确认
        print(f"\n⚠️ 即将合并以上变更到 {base} 分支")
        confirm = input("   确认合并? [y/N]: ").strip().lower()
        if confirm != "y":
            print("   ❌ 已取消")
            return 1

        try:
            # 先把 worktree 的变更 commit
            subprocess.run(
                ["git", "-C", str(wt_path), "add", "-A"],
                check=True, capture_output=True, timeout=10
            )
            r = subprocess.run(
                ["git", "-C", str(wt_path), "diff", "--cached", "--quiet"],
                capture_output=True, timeout=5
            )
            if r.returncode != 1:  # 没有变更
                print("   ⚠️ 没有待合并的变更")
                return 0

            subprocess.run(
                ["git", "-C", str(wt_path), "commit", "-m",
                 f"hermes: {info.get('task', name)}"],
                check=True, capture_output=True, timeout=10
            )

            # 合并到主仓库
            if squash:
                subprocess.run(
                    ["git", "-C", str(repo), "merge", "--squash", name],
                    check=True, capture_output=True, timeout=10
                )
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-m",
                     f"hermes(squash): {info.get('task', name)}"],
                    capture_output=True, timeout=10
                )
                print("   ✅ 已 squash 合并")
            else:
                subprocess.run(
                    ["git", "-C", str(repo), "merge", name],
                    check=True, capture_output=True, timeout=10
                )
                print("   ✅ 已合并")

            # 更新状态
            info["status"] = "merged"
            info["merged_at"] = datetime.now(timezone.utc).isoformat()
            self._save_registry()

            print("\n🎉 合并完成！")
            print(f"   任务: {info.get('task', '?')}")
            print(f"   💡 清理 worktree: python3 scripts/worktree_spawn.py discard {name}")

        except subprocess.CalledProcessError as e:
            print(f"   ❌ 合并失败: {e.stderr}")
            print(f"   💡 可能有冲突，请在 {repo} 中手动解决")
            return 1

        return 0

    def discard(self, name: str, force: bool = False) -> int:
        """放弃 worktree 并清理"""
        info = self.registry["worktrees"].get(name)
        if not info:
            print(f"❌ Worktree '{name}' 不在注册表中")
            return 1

        wt_path = Path(info["path"])
        repo = Path(info["repo"])

        # 检查是否有未合并的变更
        if not force:
            try:
                r = subprocess.run(
                    ["git", "-C", str(wt_path), "diff", "--stat", info["base_branch"]],
                    capture_output=True, text=True, timeout=5
                )
                if r.stdout.strip():
                    print("\n⚠️ 有未合并的变更:")
                    print(r.stdout)
                    print("\n   使用 --force 强制丢弃，或先 merge")
                    return 1
            except Exception:
                pass

        print(f"\n🗑️ 清理 worktree: {name}")

        # 删除 worktree
        if wt_path.exists():
            try:
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", str(wt_path), "--force"],
                    check=True, capture_output=True, timeout=10
                )
                print("   ✅ worktree 已从 git 中移除")
            except subprocess.CalledProcessError:
                # 手动清理
                try:
                    subprocess.run(
                        ["git", "-C", str(repo), "worktree", "prune"],
                        capture_output=True, timeout=5
                    )
                except Exception:
                    pass

        # 删除目录
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
            print("   ✅ 目录已删除")

        # 删除分支
        try:
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", name],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

        # 更新注册表
        if not force:
            info["status"] = "discarded"
            info["discarded_at"] = datetime.now(timezone.utc).isoformat()
        else:
            del self.registry["worktrees"][name]
        self._save_registry()

        print("   ✅ 完成")
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Worktree Spawn — Git Worktree 隔离的子 Agent 工作空间"
    )
    parser.add_argument("--repo", "-r", help="Git 仓库路径")

    sub = parser.add_subparsers(dest="command")

    create_p = sub.add_parser("create", help="创建隔离 worktree")
    create_p.add_argument("--task", "-t", required=True, help="任务描述")
    create_p.add_argument("--branch", "-b", help="分支名（自动生成）")
    create_p.add_argument("--base", default=None, help="基础分支（默认当前）")

    sub.add_parser("list", help="列出所有 worktree")

    diff_p = sub.add_parser("diff", help="查看变更")
    diff_p.add_argument("name", help="Worktree 名称")

    merge_p = sub.add_parser("merge", help="合并变更")
    merge_p.add_argument("name", help="Worktree 名称")
    merge_p.add_argument("--squash", action="store_true", help="Squash 合并")

    discard_p = sub.add_parser("discard", help="放弃并清理")
    discard_p.add_argument("name", help="Worktree 名称")
    discard_p.add_argument("--force", "-f", action="store_true", help="强制丢弃")

    args = parser.parse_args()
    mgr = WorktreeManager(args.repo)

    if args.command == "create":
        name = mgr.create(args.task, args.branch, args.base)
        if name:
            print(name)  # 输出给管道使用
    elif args.command == "list":
        mgr.list_worktrees()
    elif args.command == "diff":
        mgr.diff(args.name)
    elif args.command == "merge":
        mgr.merge(args.name, getattr(args, 'squash', False))
    elif args.command == "discard":
        mgr.discard(args.name, getattr(args, 'force', False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()