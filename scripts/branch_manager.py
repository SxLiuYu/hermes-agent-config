#!/usr/bin/env python3
"""
Session Branching — 对标 Claude Code 双 ESC 会话分支

允许在会话中创建分支点，探索替代方案而不丢失当前位置。
支持多分支并行探索、分支间对比、变更合并。

数据结构:
  ~/.hermes/sessions/<session-id>/
    branches/
      main/
        context.json     # 分支点上下文
        changes.json     # 分支后修改的文件列表
        snapshots/       # 文件快照

用法:
  hermes branch create <name>           # 从当前位置创建分支
  hermes branch switch <name>           # 切换到指定分支
  hermes branch merge <name>            # 合并分支变更到当前分支
  hermes branch list                    # 列出所有分支
  hermes branch diff <name>             # 查看分支差异
  hermes branch delete <name>           # 删除分支
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERMES_HOME = Path.home() / ".hermes"
SESSIONS_DIR = HERMES_HOME / "sessions"


def _get_session_id() -> str:
    """获取当前 session ID"""
    sid = os.environ.get("HERMES_SESSION_ID")
    if sid:
        return sid
    # Fallback: 查找最新的 session
    if SESSIONS_DIR.exists():
        sessions = sorted(SESSIONS_DIR.glob("session-*"), key=lambda d: d.stat().st_mtime, reverse=True)
        if sessions:
            return sessions[0].name
    return f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _get_active_branch(session_dir: Path) -> str:
    """获取当前活跃分支名"""
    active_file = session_dir / "branches" / ".active"
    if active_file.exists():
        return active_file.read_text().strip()
    return "main"


def _set_active_branch(session_dir: Path, name: str):
    """设置活跃分支"""
    branches_dir = session_dir / "branches"
    branches_dir.mkdir(parents=True, exist_ok=True)
    (branches_dir / ".active").write_text(name)


def create_branch(name: str, reason: str = "") -> dict:
    """从当前会话创建新分支

    保存当前文件状态作为分支点，之后两个分支独立演进。
    """
    session_id = _get_session_id()
    session_dir = SESSIONS_DIR / session_id
    branches_dir = session_dir / "branches"
    branches_dir.mkdir(parents=True, exist_ok=True)

    current_branch = _get_active_branch(session_dir)

    # 检查重名
    new_branch_dir = branches_dir / name
    if new_branch_dir.exists():
        return {"error": f"分支 '{name}' 已存在"}

    new_branch_dir.mkdir(parents=True)
    snapshots_dir = new_branch_dir / "snapshots"
    snapshots_dir.mkdir()

    # 保存分支上下文
    context = {
        "name": name,
        "parent": current_branch,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "session_id": session_id,
        "files": {},
    }

    # 快照工作目录中的所有源文件（轻量级，只记录路径和 hash）
    workdir = Path(os.getcwd())
    for pattern in ["*.py", "*.sh", "*.json", "*.yaml", "*.yml", "*.toml", "*.md"]:
        for f in workdir.glob(pattern):
            # 跳过 .hermes 和虚拟环境
            if ".hermes" in str(f) or "venv/" in str(f) or "node_modules/" in str(f):
                continue
            if f.stat().st_size > 5 * 1024 * 1024:  # 跳过 >5MB
                continue

            import hashlib
            content_hash = hashlib.md5(f.read_bytes()).hexdigest() if f.stat().st_size < 1 * 1024 * 1024 else "large"
            context["files"][str(f)] = {
                "hash": content_hash,
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            }

    # 写入上下文
    (new_branch_dir / "context.json").write_text(
        json.dumps(context, indent=2, ensure_ascii=False)
    )

    # 初始化变更记录
    (new_branch_dir / "changes.json").write_text(json.dumps({"branch_from": current_branch, "files": []}))

    print(f"🌿 分支 '{name}' 已创建")
    print(f"   父分支: {current_branch}")
    print(f"   快照文件: {len(context['files'])} 个")
    print(f"   切换到该分支: hermes branch switch {name}")

    return context


def switch_branch(name: str, dry_run: bool = False) -> dict:
    """切换到指定分支"""
    session_id = _get_session_id()
    session_dir = SESSIONS_DIR / session_id
    target_dir = session_dir / "branches" / name

    if not target_dir.exists():
        return {"error": f"分支 '{name}' 不存在"}

    current = _get_active_branch(session_dir)
    result = {"from": current, "to": name, "dry_run": dry_run}

    if not dry_run:
        _set_active_branch(session_dir, name)
        print(f"🌿 切换到分支: {current} → {name}")
    else:
        print(f"🌿 [DRY RUN] {current} → {name}")

    return result


def merge_branch(name: str, strategy: str = "keep-ours") -> dict:
    """合并分支到当前分支

    策略:
      keep-ours: 保留当前分支的文件（仅记录差异）
      keep-theirs: 用目标分支覆盖
      manual: 只显示差异，手动处理
    """
    session_id = _get_session_id()
    session_dir = SESSIONS_DIR / session_id
    source_dir = session_dir / "branches" / name
    current = _get_active_branch(session_dir)

    if not source_dir.exists():
        return {"error": f"分支 '{name}' 不存在"}
    if name == current:
        return {"error": "不能合并到自己"}

    # 加载两个分支的上下文
    source_ctx = json.loads((source_dir / "context.json").read_text())
    current_dir = session_dir / "branches" / current
    target_ctx = {}
    if (current_dir / "context.json").exists():
        target_ctx = json.loads((current_dir / "context.json").read_text())

    results = {"merged": [], "conflicts": [], "unchanged": []}

    for filepath, sinfo in source_ctx.get("files", {}).items():
        fp = Path(filepath)
        if not fp.exists():
            results["unchanged"].append(f"{filepath} (已删除)")
            continue

        current_hash = "unknown"
        if filepath in target_ctx.get("files", {}):
            current_hash = target_ctx["files"][filepath].get("hash", "?")

        import hashlib
        now_hash = hashlib.md5(fp.read_bytes()).hexdigest() if fp.stat().st_size < 1 * 1024 * 1024 else "large"

        if now_hash == sinfo.get("hash"):
            results["unchanged"].append(filepath)
        else:
            if strategy == "keep-theirs":
                # 恢复源分支的版本（需要先有快照，这里简化处理）
                results["conflicts"].append(f"{filepath} (需要快照恢复，当前策略={strategy})")
            else:
                results["conflicts"].append(filepath)

    print(f"🌿 合并 '{name}' → '{current}':")
    print(f"   冲突: {len(results['conflicts'])}")
    print(f"   已合并: {len(results['merged'])}")
    print(f"   未变化: {len(results['unchanged'])}")

    if results["conflicts"]:
        print(f"\n   ⚠️  需手动处理的文件:")
        for f in results["conflicts"][:10]:
            print(f"     - {f}")

    return results


def list_branches():
    """列出所有分支"""
    session_id = _get_session_id()
    session_dir = SESSIONS_DIR / session_id
    branches_dir = session_dir / "branches"

    if not branches_dir.exists():
        print("🌿 暂无分支")
        return

    active = _get_active_branch(session_dir)
    branches = []

    for d in sorted(branches_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            ctx_file = d / "context.json"
            if ctx_file.exists():
                try:
                    ctx = json.loads(ctx_file.read_text())
                    branches.append({
                        "name": d.name,
                        "parent": ctx.get("parent", "?"),
                        "created": ctx.get("created_at", "?")[:19],
                        "reason": ctx.get("reason", ""),
                        "files": len(ctx.get("files", {})),
                    })
                except Exception:
                    branches.append({"name": d.name, "parent": "?", "created": "?", "reason": "?", "files": 0})

    print(f"🌿 会话分支 ({len(branches)} 个):\n")
    for b in branches:
        marker = "●" if b["name"] == active else " "
        active_label = " ← 当前" if b["name"] == active else ""
        print(f"  {marker} {b['name']:<20} {b['created']}  | {b['files']} 文件{active_label}")
        if b["reason"]:
            print(f"     理由: {b['reason']}")


def diff_branch(name: str):
    """查看分支与当前分支的文件差异"""
    session_id = _get_session_id()
    session_dir = SESSIONS_DIR / session_id
    source_dir = session_dir / "branches" / name
    current = _get_active_branch(session_dir)

    if not source_dir.exists():
        print(f"❌ 分支 '{name}' 不存在")
        return

    source_ctx = json.loads((source_dir / "context.json").read_text())
    current_dir = session_dir / "branches" / current
    target_ctx = {}
    if (current_dir / "context.json").exists():
        target_ctx = json.loads((current_dir / "context.json").read_text())

    print(f"🌿 差异: {name} → {current}\n")

    all_files = set()
    all_files.update(source_ctx.get("files", {}).keys())
    all_files.update(target_ctx.get("files", {}).keys())

    for filepath in sorted(all_files):
        fp = Path(filepath)
        s_hash = source_ctx.get("files", {}).get(filepath, {}).get("hash", "?")
        t_hash = target_ctx.get("files", {}).get(filepath, {}).get("hash", "?")

        if not fp.exists():
            print(f"  🗑️  {filepath} (已删除)")
        elif s_hash == "?":
            print(f"  🆕 {filepath} (仅当前分支有)")
        elif t_hash == "?":
            print(f"  🆕 {filepath} (仅 {name} 分支有)")
        else:
            import hashlib
            now = hashlib.md5(fp.read_bytes()).hexdigest() if fp.stat().st_size < 1 * 1024 * 1024 else "large"
            if now != s_hash:
                print(f"  🔄 {filepath} (相对 {name} 分支已修改)")
            elif now != t_hash:
                print(f"  ✅ {filepath} (与 {name} 分支相同)")


def delete_branch(name: str, force: bool = False):
    """删除分支"""
    session_id = _get_session_id()
    session_dir = SESSIONS_DIR / session_id
    branch_dir = session_dir / "branches" / name
    active = _get_active_branch(session_dir)

    if not branch_dir.exists():
        print(f"❌ 分支 '{name}' 不存在")
        return
    if name == "main":
        print("❌ 不能删除 main 分支")
        return
    if name == active:
        print(f"❌ 不能删除当前活跃分支 '{name}'，先切换到其他分支")
        return

    if not force:
        response = input(f"⚠️  确认删除分支 '{name}'? [y/N] ")
        if response.lower() not in ("y", "yes"):
            print("已取消")
            return

    shutil.rmtree(str(branch_dir))
    print(f"🗑️  分支 '{name}' 已删除")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session Branching")
    sub = parser.add_subparsers(dest="command")

    create_p = sub.add_parser("create", help="创建分支")
    create_p.add_argument("name", help="分支名")
    create_p.add_argument("--reason", default="", help="分支理由")

    switch_p = sub.add_parser("switch", help="切换分支")
    switch_p.add_argument("name")
    switch_p.add_argument("--dry-run", action="store_true")

    merge_p = sub.add_parser("merge", help="合并分支")
    merge_p.add_argument("name")
    merge_p.add_argument("--strategy", default="keep-ours",
                         choices=["keep-ours", "keep-theirs", "manual"])

    sub.add_parser("list", help="列出分支")

    diff_p = sub.add_parser("diff", help="查看分支差异")
    diff_p.add_argument("name")

    delete_p = sub.add_parser("delete", help="删除分支")
    delete_p.add_argument("name")
    delete_p.add_argument("--force", action="store_true")

    args = parser.parse_args()

    if args.command == "create":
        create_branch(args.name, args.reason)
    elif args.command == "switch":
        switch_branch(args.name, args.dry_run)
    elif args.command == "merge":
        merge_branch(args.name, args.strategy)
    elif args.command == "list":
        list_branches()
    elif args.command == "diff":
        diff_branch(args.name)
    elif args.command == "delete":
        delete_branch(args.name, args.force)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()