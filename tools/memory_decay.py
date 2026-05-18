#!/usr/bin/env python3
"""
Memory Decay Engine — 对标 Mem0 Memory Decay (2026.5)

核心机制:
  1. 每次记忆被检索时，记录 last_accessed 时间戳
  2. 检索时：最近访问的记忆 boost (最高 1.5x)，闲置记忆 dampen (最低 0.3x)
  3. 永不过滤/删除，只改变排序权重

解决"Day 30 问题": 长运行 agent 的历史累积越多，检索越容易返回过时记忆。
语义相似度是时间盲的，decay 通过 access_recency 信号打破平局。

架构:
  ~/.hermes/memories/memory_meta.json  — 每条记忆的元数据
    {
      "<entry_hash>": {
        "last_accessed": "2026-05-18T14:00:00Z",
        "access_count": 15,
        "created_at": "2026-04-01T00:00:00Z",
        "decay_factor": 1.0
      }
    }

用法:
  python3 tools/memory_decay.py touch <entry_text>        # 标记记忆被访问
  python3 tools/memory_decay.py rank <query_context>     # recency 加权排序
  python3 tools/memory_decay.py stats                    # 衰减统计
  python3 tools/memory_decay.py reap --days 30           # 清理极久未访问记忆
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MEMORY_FILE = HERMES_HOME / "memories" / "MEMORY.md"
META_FILE = HERMES_HOME / "memories" / "memory_meta.json"
USER_FILE = HERMES_HOME / "memories" / "USER.md"
LOG_FILE = HERMES_HOME / "logs" / "memory_decay.log"

# Decay 参数 (对标 Mem0)
BOOST_MAX = 1.5         # 最近访问的最高放大倍数
DAMPEN_MIN = 0.3        # 闲置记忆的最低衰减倍数
DECAY_HALF_LIFE_DAYS = 14  # 半衰期：14 天无访问衰减到 0.5
HOT_THRESHOLD_HOURS = 1    # < 1小时 = "热"记忆 (boost=1.5)
WARM_THRESHOLD_DAYS = 3    # < 3天 = "温"记忆


def log(msg: str):
    """写日志"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _hash_entry(text: str) -> str:
    """对记忆条目取 hash (前 12 位)"""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:12]


def load_meta() -> dict:
    """加载记忆元数据"""
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_meta(meta: dict):
    """保存记忆元数据"""
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


def parse_memory_entries(mem_file: Path = MEMORY_FILE) -> list[dict]:
    """解析 MEMORY.md 为结构化条目列表"""
    if not mem_file.exists():
        return []

    text = mem_file.read_text()
    entries = text.split("§")
    result = []
    for i, entry in enumerate(entries):
        content = entry.strip()
        if not content:
            continue
        result.append({
            "index": i,
            "hash": _hash_entry(content),
            "content": content,
            "length": len(content),
        })
    return result


def compute_decay_factor(last_accessed_str: str | None, 
                         access_count: int = 0) -> float:
    """
    计算衰减因子
    
    对标 Mem0 Memory Decay:
    - 最近 1 小时内访问: 1.5x boost
    - 1h ~ 3 天: 1.0x ~ 1.5x 线性
    - 3 天 ~ 14 天: 0.5x ~ 1.0x 指数衰减
    - > 14 天: 0.3x ~ 0.5x 渐进到底
    - 从未访问 (创建时): 1.0x
    - 高频记忆 (access_count > 20): +0.1 偏移
    """
    if not last_accessed_str:
        return 1.0  # 新记忆，无衰减
    
    try:
        last_ts = datetime.fromisoformat(last_accessed_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 1.0
    
    now = datetime.now(timezone.utc)
    elapsed = now - last_ts
    hours = elapsed.total_seconds() / 3600
    
    if hours < HOT_THRESHOLD_HOURS:
        factor = BOOST_MAX
    elif hours < WARM_THRESHOLD_DAYS * 24:
        # 线性: 1小时→1.5, 3天→1.0
        factor = BOOST_MAX - (BOOST_MAX - 1.0) * (hours - 1) / (72 - 1)
    elif hours < DECAY_HALF_LIFE_DAYS * 24:
        # 指数: 3天→1.0, 14天→0.5
        days = hours / 24
        progress = (days - WARM_THRESHOLD_DAYS) / (DECAY_HALF_LIFE_DAYS - WARM_THRESHOLD_DAYS)
        factor = 1.0 - progress * 0.5
    else:
        # 渐进到底: 14天→0.5, ∞→0.3
        days = hours / 24
        extra_days = days - DECAY_HALF_LIFE_DAYS
        factor = max(DAMPEN_MIN, 0.5 - 0.2 * (1 - 2.718 ** (-extra_days / 30)))
    
    # 高频偏移
    if access_count > 50:
        factor = min(BOOST_MAX, factor + 0.15)
    elif access_count > 20:
        factor = min(BOOST_MAX, factor + 0.08)
    
    return round(factor, 3)


def touch_entry(entry_text: str, meta: dict | None = None) -> dict:
    """标记记忆条目被访问，更新元数据"""
    if meta is None:
        meta = load_meta()
    
    h = _hash_entry(entry_text)
    now = datetime.now(timezone.utc).isoformat()
    
    if h in meta:
        meta[h]["last_accessed"] = now
        meta[h]["access_count"] = meta[h].get("access_count", 0) + 1
    else:
        meta[h] = {
            "last_accessed": now,
            "access_count": 1,
            "created_at": now,
        }
    
    # 重新计算衰减因子
    meta[h]["decay_factor"] = compute_decay_factor(
        meta[h]["last_accessed"],
        meta[h]["access_count"]
    )
    
    save_meta(meta)
    log(f"touch {h}: access_count={meta[h]['access_count']}, decay={meta[h].get('decay_factor','?')}")
    return meta


def touch_by_query(query: str, entries: list[dict] | None = None, 
                   meta: dict | None = None) -> dict:
    """根据查询 touch 最相关的记忆条目"""
    if meta is None:
        meta = load_meta()
    if entries is None:
        entries = parse_memory_entries()
    
    if not entries:
        return meta
    
    # 简单的关键词匹配（不对接向量模型，避免依赖）
    query_words = set(query.lower().split())
    scored = []
    for e in entries:
        content_lower = e["content"].lower()
        score = sum(1 for w in query_words if w in content_lower)
        if score > 0:
            scored.append((score, e))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # Touch top 3
    for _, e in scored[:3]:
        touch_entry(e["content"], meta)
    
    return meta


def rank_entries(entries: list[dict] | None = None, 
                 meta: dict | None = None) -> list[dict]:
    """按 recency 权重重新排序记忆条目"""
    if meta is None:
        meta = load_meta()
    if entries is None:
        entries = parse_memory_entries()
    
    scored = []
    for e in entries:
        m = meta.get(e["hash"], {})
        factor = compute_decay_factor(
            m.get("last_accessed"),
            m.get("access_count", 0)
        )
        
        # 位置权重：前面的条目默认优先级高 (MEMORY.md 是有意排序的)
        position_weight = max(0.5, 1.0 - e["index"] * 0.01)
        
        # 综合分数 = decay_factor × position_weight
        final_score = factor * position_weight
        
        scored.append({
            **e,
            "decay_factor": factor,
            "position_weight": position_weight,
            "score": round(final_score, 3),
            "access_count": m.get("access_count", 0),
            "last_accessed": m.get("last_accessed"),
        })
    
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def get_decay_stats(meta: dict | None = None) -> str:
    """衰减统计"""
    if meta is None:
        meta = load_meta()
    
    if not meta:
        return "📊 记忆衰减统计: 暂无数据"
    
    entries = parse_memory_entries()
    ranked = rank_entries(entries, meta)
    
    now = datetime.now(timezone.utc)
    lines = [
        f"📊 记忆衰减统计",
        f"   总记忆条目: {len(entries)}",
        f"   已追踪元数据: {len(meta)}",
        f"",
        f"   Top 5 (按 recency):",
    ]
    
    for i, e in enumerate(ranked[:5]):
        factor = f"🔥 {e['decay_factor']:.2f}x" if e["decay_factor"] > 1.0 else \
                 f"🟡 {e['decay_factor']:.2f}x" if e["decay_factor"] > 0.5 else \
                 f"❄️ {e['decay_factor']:.2f}x"
        lines.append(f"   {i+1}. [{factor}] {e['content'][:60]}...")
    
    # 热/温/冷 分布
    hot = sum(1 for e in ranked if e["decay_factor"] > 1.2)
    warm = sum(1 for e in ranked if 0.5 < e["decay_factor"] <= 1.2)
    cold = sum(1 for e in ranked if e["decay_factor"] <= 0.5)
    lines.append(f"")
    lines.append(f"   🔥 热记忆 (>1.2x): {hot}")
    lines.append(f"   🟡 温记忆: {warm}")
    lines.append(f"   ❄️ 冷记忆 (<0.5x): {cold}")
    
    return "\n".join(lines)


def reap_old_memories(days: int = 30, dry_run: bool = True) -> str:
    """清理极久未访问的记忆（归档，不删除）"""
    meta = load_meta()
    entries = parse_memory_entries()
    ranked = rank_entries(entries, meta)
    
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    
    stale = []
    for e in ranked:
        if e["last_accessed"]:
            try:
                last = datetime.fromisoformat(e["last_accessed"].replace("Z", "+00:00"))
                if last < cutoff:
                    stale.append(e)
            except (ValueError, TypeError):
                pass
    
    if not stale:
        return f"✅ 无超过 {days} 天未访问的记忆"
    
    archive_dir = HERMES_HOME / "memories" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    lines = [f"📦 待归档记忆 ({len(stale)} 条, >{days}天未访问):"]
    
    if dry_run:
        for e in stale[:10]:
            lines.append(f"   - [{e['decay_factor']:.2f}x] {e['content'][:60]}...")
        if len(stale) > 10:
            lines.append(f"   ... 及其他 {len(stale)-10} 条")
        lines.append(f"\n   运行 --no-dry-run 确认归档")
        return "\n".join(lines)
    
    # 实际归档
    ts = now.strftime("%Y%m%d")
    archive_file = archive_dir / f"archived_{ts}.md"
    with open(archive_file, "w") as f:
        f.write(f"# 归档于 {now.isoformat()}\n\n")
        for e in stale:
            f.write(f"{e['content']}\n§\n")
    
    # 从 MEMORY.md 中移除
    stale_hashes = {e["hash"] for e in stale}
    current = MEMORY_FILE.read_text() if MEMORY_FILE.exists() else ""
    sections = current.split("§")
    kept = []
    for s in sections:
        content = s.strip()
        if not content:
            continue
        if _hash_entry(content) not in stale_hashes:
            kept.append(content)
    
    MEMORY_FILE.write_text("\n§\n".join(kept) + "\n")
    lines.append(f"\n   已归档 {len(stale)} 条到 {archive_file}")
    lines.append(f"   保留 {len(kept)} 条")
    
    return "\n".join(lines)


def generate_context_injection(max_entries: int = 0) -> str:
    """
    生成 recency 加权的上下文注入（用于 session_start hook）
    按衰减因子排序后输出，确保热记忆优先出现在 system prompt 中
    """
    meta = load_meta()
    entries = parse_memory_entries()
    ranked = rank_entries(entries, meta)
    
    if max_entries > 0:
        ranked = ranked[:max_entries]
    
    # 按原格式输出，但按 recency 排序
    parts = []
    for e in ranked:
        # 如果有冷记忆标记，加注释但不删除
        marker = ""
        if e["decay_factor"] < 0.4:
            marker = f" [stale: {e.get('last_accessed','?')[:10]}]"
        parts.append(e["content"] + marker)
    
    return "\n§\n".join(parts) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Memory Decay Engine")
    sub = parser.add_subparsers(dest="command")
    
    # touch — 标记记忆被访问
    p = sub.add_parser("touch", help="标记记忆条目被访问")
    p.add_argument("text", help="记忆文本内容")
    
    # touch-query — 根据查询词 touch 最相关记忆
    p = sub.add_parser("touch-query", help="根据查询词匹配并 touch 相关记忆")
    p.add_argument("query", help="查询文本")
    
    # rank — recency 加权排序
    p = sub.add_parser("rank", help="按 recency 加权排序并输出")
    p.add_argument("--limit", type=int, default=0, help="限制输出条数")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    
    # stats — 统计
    sub.add_parser("stats", help="衰减统计")
    
    # context — 生成上下文注入
    p = sub.add_parser("context", help="生成 recency 加权的上下文注入")
    p.add_argument("--max", type=int, default=0, help="最大条目数")
    
    # reap — 归档
    p = sub.add_parser("reap", help="归档极久未访问的记忆")
    p.add_argument("--days", type=int, default=30, help="未访问天数阈值")
    p.add_argument("--no-dry-run", action="store_true", help="确认执行")
    
    # inject — 自动路径：touch + context 一体
    p = sub.add_parser("inject", help="自动触摸 + 生成注入 (hook 用)")
    p.add_argument("--query", default="", help="当前查询上下文")
    
    args = parser.parse_args()
    
    if args.command == "touch":
        touch_entry(args.text)
        print("✅ touched")
    
    elif args.command == "touch-query":
        touch_by_query(args.query)
        print("✅ touched by query")
    
    elif args.command == "rank":
        ranked = rank_entries()
        if args.limit:
            ranked = ranked[:args.limit]
        
        if args.json:
            print(json.dumps(ranked, indent=2, ensure_ascii=False))
        else:
            for i, e in enumerate(ranked):
                bar = "█" * int(e["decay_factor"] * 5)
                print(f"{i+1:3d}. [{e['decay_factor']:.2f}x {bar}] {e['content'][:80]}")
    
    elif args.command == "stats":
        print(get_decay_stats())
    
    elif args.command == "context":
        print(generate_context_injection(args.max))
    
    elif args.command == "reap":
        print(reap_old_memories(args.days, dry_run=not args.no_dry_run))
    
    elif args.command == "inject":
        if args.query:
            touch_by_query(args.query)
        ctx = generate_context_injection()
        print(f"# Memory Decay Context Injection")
        print(f"# Generated: {datetime.now(timezone.utc).isoformat()}")
        print(ctx)
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()