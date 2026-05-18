#!/usr/bin/env python3
"""
Session Checkpoint/Restore — 对标 Gemini CLI 会话管理

自动在写文件前创建检查点，支持一键回滚到任意历史状态。

机制:
  1. PreToolUse hook: 拦截 write_file/patch，自动创建快照
  2. 每个检查点保存文件内容 + 路径映射
  3. restore 命令: 回滚到指定检查点

用法:
  hermes checkpoint list              # 列出当前会话检查点
  hermes checkpoint show <id>        # 显示检查点详情
  hermes checkpoint restore <id>     # 回滚到指定检查点
  hermes checkpoint prune            # 清理旧检查点
  hermes checkpoint diff <id>        # 对比当前和检查点的差异
"""

import json
import os
import shutil
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

HERMES_HOME = Path.home() / ".hermes"
CHECKPOINTS_DIR = HERMES_HOME / "checkpoints"
MAX_CHECKPOINTS = 20  # 每个会话最多保留的检查点数


def create_checkpoint(files: list = None, reason: str = "") -> dict:
    """创建检查点：备份指定文件（默认备份最近修改的 10 个文件）

    智能策略:
    - 只备份工作目录内的文件
    - 跳过 >10MB 的文件
    - 跳过 .git/ 和 node_modules/ 下的文件
    """
    session_id = os.environ.get("HERMES_SESSION_ID", datetime.now().strftime("%Y%m%d-%H%M%S"))
    session_dir = CHECKPOINTS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # 自动发现修改的文件
    if files is None:
        files = _find_modified_files()

    if not files:
        return {"id": None, "reason": "没有需要备份的文件"}

    # 创建检查点
    idx = 0
    existing = sorted(session_dir.glob("cp-*"))
    if existing:
        idx = max(int(e.name.split("-")[1]) for e in existing) + 1

    cp_id = f"cp-{idx:03d}"
    cp_dir = session_dir / cp_id
    cp_dir.mkdir()

    # 备份文件
    manifest = {
        "id": cp_id,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "files": {},
        "truncated": False,
    }

    for fpath in files:
        src = Path(fpath).expanduser().resolve()
        if not src.exists():
            continue
        if src.stat().st_size > 10 * 1024 * 1024:  # 跳过 >10MB
            manifest["truncated"] = True
            continue
        if ".git/" in str(src) or "node_modules/" in str(src):
            continue

        # 在 checkpoints 中重建路径结构
        rel_path = str(src).replace(str(Path.home()), "~")
        # 用 _ 替换 / 避免子目录
        safe_name = rel_path.lstrip("~").lstrip("/").replace("/", "___")
        dest = cp_dir / safe_name

        try:
            shutil.copy2(str(src), str(dest))
            manifest["files"][str(src)] = {
                "safe_name": safe_name,
                "size": src.stat().st_size,
                "mtime": datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
        except Exception as e:
            print(f"⚠️  备份失败: {src} -> {e}", file=sys.stderr)

    if not manifest["files"]:
        shutil.rmtree(str(cp_dir))
        return {"id": None, "reason": "没有成功备份任何文件"}

    # 写入 manifest
    (cp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    # 限制检查点数量
    _prune_session(session_id, max_keep=MAX_CHECKPOINTS)

    file_count = len(manifest["files"])
    print(f"📸 检查点: {cp_id} ({file_count} 个文件)" + (f" — {reason}" if reason else ""))
    return manifest


def _find_modified_files() -> list:
    """找到工作目录下最近 30 分钟修改的源文件"""
    workdir = Path(os.getcwd())
    recent = []

    try:
        result = subprocess.run(
            ["find", str(workdir), "-type", "f",
             "-name", "*.py", "-o", "-name", "*.sh", "-o",
             "-name", "*.json", "-o", "-name", "*.yaml", "-o", "-name", "*.yml", "-o",
             "-name", "*.toml", "-o", "-name", "*.md", "-o",
             "-name", "*.js", "-o", "-name", "*.ts", "-o",
             "-name", "*.html", "-o", "-name", "*.css",
             "-mmin", "-30"],
            capture_output=True, text=True, timeout=10,
        )
        recent = [line for line in result.stdout.strip().split("\n") if line]
    except Exception:
        pass

    # 排除 .git, node_modules, __pycache__, .hermes
    recent = [
        f for f in recent
        if ".git/" not in f
        and "node_modules/" not in f
        and "__pycache__" not in f
        and ".hermes/" not in f
    ]

    return recent[:15]


def _prune_session(session_id: str, max_keep: int = 20):
    """清理会话内过多检查点（保留最近的 max_keep 个）"""
    session_dir = CHECKPOINTS_DIR / session_id
    if not session_dir.exists():
        return

    all_cps = sorted(session_dir.glob("cp-*"))
    if len(all_cps) <= max_keep:
        return

    to_remove = all_cps[:-max_keep]
    for cp in to_remove:
        shutil.rmtree(str(cp))


def restore_checkpoint(cp_id: str, dry_run: bool = False) -> dict:
    """回滚到指定检查点"""
    # 查找检查点
    cp_dir = _find_checkpoint(cp_id)
    if not cp_dir:
        return {"error": f"找不到检查点: {cp_id}"}

    manifest = json.loads((cp_dir / "manifest.json").read_text())
    results = {"restored": [], "skipped": [], "errors": []}

    for orig_path, info in manifest["files"].items():
        safe_name = info["safe_name"]
        backup_file = cp_dir / safe_name

        if not backup_file.exists():
            results["skipped"].append(f"{orig_path} (备份丢失)")
            continue

        if not dry_run:
            try:
                # 恢复前再次备份当前版本
                current = Path(orig_path)
                if current.exists():
                    shutil.copy2(str(current), str(current.with_suffix(current.suffix + ".pre-restore")))

                # 恢复
                Path(orig_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(backup_file), str(orig_path))
                results["restored"].append(orig_path)
            except Exception as e:
                results["errors"].append(f"{orig_path}: {e}")
        else:
            results["restored"].append(f"[DRY RUN] {orig_path}")

    return {
        "checkpoint": cp_id,
        "manifest": manifest,
        "results": results,
        "dry_run": dry_run,
    }


def _find_checkpoint(cp_id: str) -> Optional[Path]:
    """查找检查点目录"""
    if "/" in cp_id:
        # 完整路径: session_id/cp-xxx
        cp_dir = CHECKPOINTS_DIR / cp_id
        if cp_dir.exists():
            return cp_dir
    else:
        # 搜索所有会话
        for session_dir in sorted(CHECKPOINTS_DIR.glob("*/"), reverse=True):
            cp_dir = session_dir / cp_id
            if cp_dir.exists():
                return cp_dir

    return None


def list_checkpoints(session_id: str = None, limit: int = 30):
    """列出检查点"""
    if session_id:
        session_dirs = [CHECKPOINTS_DIR / session_id]
    else:
        session_dirs = sorted(
            CHECKPOINTS_DIR.glob("*/"),
            key=lambda d: d.name,
            reverse=True,
        ) if CHECKPOINTS_DIR.exists() else []

    found = 0
    for sd in session_dirs:
        cps = sorted(sd.glob("cp-*/"))
        for cp in cps:
            mf = cp / "manifest.json"
            if not mf.exists():
                continue
            try:
                m = json.loads(mf.read_text())
                ts = m.get("timestamp", "?")[:19]
                n = len(m.get("files", {}))
                reason = m.get("reason", "")
                print(f"  {m['id']}  {ts}  {n:2d} 文件" + (f"  ({reason})" if reason else ""))
                found += 1
                if found >= limit:
                    return
            except Exception:
                continue

    if found == 0:
        print("📸 暂无检查点")


def show_checkpoint(cp_id: str):
    """显示检查点详情"""
    cp_dir = _find_checkpoint(cp_id)
    if not cp_dir:
        print(f"❌ 找不到检查点: {cp_id}")
        return

    manifest = json.loads((cp_dir / "manifest.json").read_text())

    print(f"检查点: {manifest['id']}")
    print(f"时间:   {manifest.get('timestamp', '?')[:19]}")
    print(f"原因:   {manifest.get('reason', '（无）')}")
    print(f"文件数: {len(manifest.get('files', {}))}")
    print()

    for path, info in sorted(manifest["files"].items()):
        size_kb = info["size"] / 1024
        print(f"  {path}")
        print(f"    大小: {size_kb:.1f} KB  |  mtime: {info.get('mtime', '?')[:19]}")


def diff_checkpoint(cp_id: str):
    """对比当前文件和检查点的差异"""
    cp_dir = _find_checkpoint(cp_id)
    if not cp_dir:
        print(f"❌ 找不到检查点: {cp_id}")
        return

    manifest = json.loads((cp_dir / "manifest.json").read_text())

    for orig_path, info in manifest["files"].items():
        safe_name = info["safe_name"]
        backup_file = cp_dir / safe_name
        current_file = Path(orig_path)

        if not backup_file.exists():
            print(f"⚠️  {orig_path}: 备份丢失")
            continue

        if not current_file.exists():
            print(f"🆕 {orig_path}: 文件已删除（备份中有）")
            continue

        # 对比
        try:
            result = subprocess.run(
                ["diff", "-u", str(backup_file), str(current_file)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                print(f"✅ {orig_path}: 无变化")
            else:
                lines = result.stdout.split("\n")
                added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
                removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
                print(f"🔄 {orig_path}: +{added}/-{removed} 行变化")
        except Exception as e:
            print(f"⚠️  {orig_path}: diff 失败 ({e})")


def prune_all(days: int = 7):
    """清理超过 N 天的检查点"""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0

    for session_dir in CHECKPOINTS_DIR.glob("*/"):
        for cp in session_dir.glob("cp-*/"):
            try:
                mtime = cp.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(str(cp))
                    removed += 1
            except Exception:
                pass

        # 清理空会话目录
        if not any(session_dir.iterdir()):
            session_dir.rmdir()

    print(f"🧹 清理了 {removed} 个旧检查点")
    return removed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session Checkpoint/Restore")
    sub = parser.add_subparsers(dest="command")

    create_p = sub.add_parser("create", help="创建检查点")
    create_p.add_argument("--reason", default="手动创建", help="检查点原因")
    create_p.add_argument("--files", nargs="*", help="要备份的文件（默认自动发现）")

    sub.add_parser("list", help="列出检查点")

    show_p = sub.add_parser("show", help="显示检查点详情")
    show_p.add_argument("id", help="检查点 ID (如 cp-001)")

    restore_p = sub.add_parser("restore", help="回滚到检查点")
    restore_p.add_argument("id", help="检查点 ID")
    restore_p.add_argument("--dry-run", action="store_true", help="预览回滚（不实际修改文件）")

    diff_p = sub.add_parser("diff", help="对比差异")
    diff_p.add_argument("id", help="检查点 ID")

    prune_p = sub.add_parser("prune", help="清理旧检查点")
    prune_p.add_argument("--days", type=int, default=7, help="保留天数（默认 7）")

    args = parser.parse_args()

    if args.command == "create":
        create_checkpoint(files=args.files, reason=args.reason)
    elif args.command == "list":
        list_checkpoints()
    elif args.command == "show":
        show_checkpoint(args.id)
    elif args.command == "restore":
        result = restore_checkpoint(args.id, dry_run=args.dry_run)
        if "error" in result:
            print(f"❌ {result['error']}")
        else:
            mode = "[DRY RUN] " if args.dry_run else ""
            r = result["results"]
            print(f"📸 {mode}从 {args.id} 回滚:")
            for f in r["restored"]:
                print(f"  ✅ {f}")
            for f in r["skipped"]:
                print(f"  ⏭️  {f}")
            for f in r["errors"]:
                print(f"  ❌ {f}")
    elif args.command == "diff":
        diff_checkpoint(args.id)
    elif args.command == "prune":
        prune_all(days=args.days)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()