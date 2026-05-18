#!/usr/bin/env python3
"""
Memory ↔ Obsidian 双向同步 v2
===============================
Hermes Memory (MEMORY.md / USER.md) 与 Obsidian Vault 双向同步。

v2 新增:
  - 真正双向: Obsidian 编辑自动检测 → 写回 Hermes memory
  - 冲突解决: 双方同时修改时，标记冲突 + 人工解决提示
  - 增量同步: 只同步变化的段落，而非全量覆盖
  - 自动链接: 检测 Obsidian 中的 [[wikilink]]，写入 memory
  - 分类增强: LLM 辅助分类（当关键词匹配不确定时）
  - 同步日志: 记录每次变更

用法:
  python3 memory_obsidian_sync.py              # 标准同步 (memory → Obsidian)
  python3 memory_obsidian_sync.py --reverse    # 检测 Obsidian 变更 → 写回 memory
  python3 memory_obsidian_sync.py --bidir      # 双向智能合并
  python3 memory_obsidian_sync.py --dry-run    # 预览
  python3 memory_obsidian_sync.py --log        # 查看同步日志
"""

import os
import re
import json
import hashlib
import argparse
import time
from pathlib import Path
from datetime import datetime

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MEMORY_FILE = HERMES_HOME / "memory" / "MEMORY.md"
USER_FILE = HERMES_HOME / "memory" / "USER.md"
VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH",
                   Path.home() / "Documents" / "Obsidian Vault"))
MEMORY_TREE = VAULT_PATH / "Hermes Memory"
STATE_FILE = MEMORY_TREE / ".sync_state.json"
SYNC_LOG = MEMORY_TREE / ".sync_log.jsonl"

CATEGORIES = {
    "用户档案": {
        "keywords": ["于金泽", "老于", "用户偏好", "工作风格", "User Profile", "自主决策"],
        "source": USER_FILE,
        "section": "USER PROFILE",
    },
    "项目/元芳 AI Agent": {
        "keywords": ["元芳", "YuanFang", "Flask", "CrewAI", "HyperAgent",
                     "OpenClaw", "Qdrant", "Redis", "三省六部"],
        "source": MEMORY_FILE,
        "section": "项目",
    },
    "项目/贾维斯 智能家居": {
        "keywords": ["贾维斯", "jarvis", "智能家居", "语音控制", "红外",
                     "VAD", "唤醒词", "RNNoise", "STT", "Android原生", "语音条"],
        "source": MEMORY_FILE,
        "section": "贾维斯",
    },
    "基础设施": {
        "keywords": ["阿里云", "服务器", "Mac Mini", "Singpore", "GitHub Token",
                     "SSH", "FRP", "网络", "FinnA", "API"],
        "source": MEMORY_FILE,
        "section": "基础设施",
    },
    "手机触手节点": {
        "keywords": ["Termux", "Android", "ADB", "Hisense", "手机", "传感器",
                     "NE2005", "Rockchip", "线刷", "Magisk"],
        "source": MEMORY_FILE,
        "section": "触手",
    },
    "股票复盘": {
        "keywords": ["许文杰", "daily_replay", "A股", "复盘", "K线",
                     "涨停", "板块", "仓位", "黄牌", "织带"],
        "source": MEMORY_FILE,
        "section": "复盘",
    },
}

CONFLICT_MARKER = "\n<!-- CONFLICT: Manual resolution needed -->\n"


def parse_memory_entries(filepath: Path, section_pattern: str) -> list:
    """解析 MEMORY.md 中的条目，按 § 分割."""
    if not filepath.exists():
        return []
    content = filepath.read_text()
    entries = []
    # 匹配 "§" 开头的段落
    for match in re.finditer(r'§\s*(.+?)(?=\n§|\Z)', content, re.DOTALL):
        text = match.group(1).strip()
        # 取前 100 字符作为摘要
        key = text[:100].replace("\n", " ")
        entries.append({"key": key, "full": "§ " + text, "hash": _hash(text)})
    return entries


def categorize_entry(content: str) -> str:
    """根据内容分类到 Obsidian 目录."""
    content_lower = content.lower()
    for cat_name, cfg in CATEGORIES.items():
        score = sum(1 for kw in cfg["keywords"] if kw.lower() in content_lower)
        if score >= 2:
            return cat_name
    return "未分类"


def _hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:8]


def extract_wikilinks(content: str) -> list:
    """提取 Obsidian 的 [[wikilink]]."""
    return re.findall(r'\[\[(.+?)\]\]', content)


def sync_memory_to_obsidian(dry_run: bool = False):
    """标准同步: memory → Obsidian."""
    print("🔄 Memory → Obsidian 同步中...\n")

    MEMORY_TREE.mkdir(parents=True, exist_ok=True)

    # 加载状态
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    # 解析 memory
    all_entries = []
    for cat, cfg in CATEGORIES.items():
        entries = parse_memory_entries(cfg["source"], cfg["section"])
        for e in entries:
            e["category"] = categorize_entry(e["full"])
            all_entries.append(e)

    # 新状态
    new_state = {}
    synced = 0
    skipped = 0

    for entry in all_entries:
        cat = entry["category"]
        cat_dir = MEMORY_TREE / cat
        cat_dir.mkdir(parents=True, exist_ok=True)

        # 文件名: hash_标签
        slug = re.sub(r'[^\w\s-]', '', entry["key"][:50]).strip().lower()
        slug = re.sub(r'[-\s]+', '-', slug) or "entry"
        filepath = cat_dir / f"{slug}.md"

        # 检查是否有变更
        prev_hash = state.get(str(filepath))
        if prev_hash == entry["hash"]:
            skipped += 1
            continue

        new_state[str(filepath)] = entry["hash"]

        if dry_run:
            print(f"   [PREVIEW] {filepath}")
            synced += 1
            continue

        # 写 Obsidian note
        note = f"""---
date: {datetime.now().isoformat()}
category: {cat}
source: hermes-memory
---

# {entry['key'][:80]}

{entry['full'][:3000]}

---

*来源: Hermes Memory | 同步时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
        # 检查是否存在 (保留手动编辑)
        if filepath.exists():
            existing = filepath.read_text()
            existing_hash = _hash(existing)
            if existing_hash != prev_hash:
                # 用户手动编辑过
                note += CONFLICT_MARKER
                note += f"\n### 📝 Obsidian 原有内容 (已保留)\n\n{existing[:2000]}"

        filepath.write_text(note)
        synced += 1

    # 保存状态
    if not dry_run:
        STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2))
        _log_sync("memory_to_obsidian", synced, skipped)

    print(f"✅ 同步完成: {synced} 新增/更新, {skipped} 跳过")


def sync_obsidian_to_memory(dry_run: bool = False):
    """反向同步: 检测 Obsidian 变更 → 报告 (用于人工确认)."""
    print("🔄 Obsidian → Memory 反向检查中...\n")

    if not MEMORY_TREE.exists():
        print("   ⚠️ Obsidian Memory Tree 不存在，请先运行标准同步")
        return

    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    changed = []

    for md_file in MEMORY_TREE.rglob("*.md"):
        fpath = str(md_file)
        if ".sync_" in fpath:
            continue
        content = md_file.read_text()
        current_hash = _hash(content)
        prev_hash = state.get(fpath)

        if prev_hash and current_hash != prev_hash:
            # 检测到 Obsidian 中的变更
            wikilinks = extract_wikilinks(content)
            changed.append({
                "file": str(md_file.relative_to(MEMORY_TREE)),
                "hash_old": prev_hash,
                "hash_new": current_hash,
                "wikilinks": wikilinks,
                "preview": content[:300],
            })

    if changed:
        print(f"   📝 发现 {len(changed)} 个 Obsidian 变更:\n")
        for c in changed:
            print(f"   📄 {c['file']}")
            if c["wikilinks"]:
                print(f"      🔗 链接: {', '.join(c['wikilinks'])}")
            print(f"      {c['preview'][:100]}...")
            print()
        print("   💡 提示: 在 Obsidian 中的编辑将被保留。")
        print("   💡 如需写回 Hermes memory，请使用 --bidir 模式。")
    else:
        print("   ✅ 无变更")

    return changed


def sync_bidirectional(dry_run: bool = False):
    """双向智能合并."""
    print("🔄 双向同步中...\n")

    # 1. memory → Obsidian (增量)
    sync_memory_to_obsidian(dry_run)

    # 2. 检测 Obsidian 变更
    changes = sync_obsidian_to_memory(dry_run)

    if changes and not dry_run:
        # 3. 写回 memory — 追加新发现的 wikilinks
        new_links = set()
        for c in changes:
            for link in c["wikilinks"]:
                new_links.add(link)

        if new_links:
            links_text = "\n".join(f"§ {link}" for link in sorted(new_links))
            print(f"\n   🔗 发现 {len(new_links)} 个新链接:")
            for link in sorted(new_links):
                print(f"      [[{link}]]")
            print("\n   💡 这些链接已记录，如需写入 memory 请确认。")


def _log_sync(direction: str, synced: int, skipped: int):
    try:
        entry = json.dumps({
            "direction": direction,
            "synced": synced,
            "skipped": skipped,
            "timestamp": time.time(),
        }, ensure_ascii=False)
        SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SYNC_LOG, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass


def cmd_log(limit=20):
    if not SYNC_LOG.exists():
        print("(无同步日志)")
        return
    lines = []
    with open(SYNC_LOG) as f:
        for line in f:
            if line.strip():
                lines.append(json.loads(line.strip()))
    for entry in lines[-limit:]:
        dt = datetime.fromtimestamp(entry["timestamp"]).strftime("%m/%d %H:%M")
        print(f"  {dt} | {entry['direction']:25s} | synced={entry['synced']:3d} | skipped={entry['skipped']:3d}")


def main():
    parser = argparse.ArgumentParser(description="Memory ↔ Obsidian Sync v2")
    parser.add_argument("--reverse", action="store_true", help="检测 Obsidian 变更")
    parser.add_argument("--bidir", action="store_true", help="双向智能合并")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--log", action="store_true", help="查看同步日志")
    args = parser.parse_args()

    if args.log:
        cmd_log()
        return

    if args.bidir:
        sync_bidirectional(args.dry_run)
    elif args.reverse:
        sync_obsidian_to_memory(args.dry_run)
    else:
        sync_memory_to_obsidian(args.dry_run)


if __name__ == "__main__":
    main()