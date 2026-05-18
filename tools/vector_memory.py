#!/usr/bin/env python3
"""
vector_memory.py — 向量记忆检索系统（对标 Mem0）

用轻量级字符 n-gram 哈希 + 稀疏词袋向量实现语义检索。
无需外部 embedding API，纯 Python 标准库。

用法:
  python3 vector_memory.py add --text "..." --tags "..."    # 添加记忆
  python3 vector_memory.py search --query "..." --top-k 5   # 语义搜索
  python3 vector_memory.py stats                            # 统计信息
  python3 vector_memory.py inject --query "..."             # 注入最相关记忆
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────
VECTOR_DIR = Path.home() / ".hermes" / "vector_memory"
VECTORS_FILE = VECTOR_DIR / "vectors.jsonl"
VECTOR_DIM = 128
MAX_MEMORIES = 1000


# ── 向量化引擎 ────────────────────────────────────────
def _extract_ngrams(text: str, n: int) -> list[str]:
    """提取字符级 n-gram（跳过空白，保留中文字符）。"""
    clean = text.replace(" ", "").replace("\n", "")
    if len(clean) < n:
        return [clean]
    return [clean[i:i + n] for i in range(len(clean) - n + 1)]


def _hash_ngram(ngram: str, dim: int = VECTOR_DIM) -> int:
    """将 n-gram 哈希到 [0, dim) 的索引。"""
    h = hashlib.md5(ngram.encode("utf-8", errors="ignore")).digest()
    # 取前 4 字节作为整数并取模
    idx = int.from_bytes(h[:4], "big") % dim
    return idx


def _compute_idf(ngrams_list: list[list[str]]) -> dict[str, float]:
    """计算 n-gram 的 IDF（逆文档频率），用于 TF-IDF 加权。

    IDF = log(1 + 总文档数 / (1 + 包含该 ngram 的文档数))
    """
    doc_count = len(ngrams_list)
    if doc_count == 0:
        return {}

    # 统计每个 ngram 出现在多少文档中
    df: dict[str, int] = {}
    for ngrams in ngrams_list:
        for ng in set(ngrams):
            df[ng] = df.get(ng, 0) + 1

    idf = {}
    for ng, count in df.items():
        idf[ng] = math.log(1.0 + doc_count / (1.0 + count))
    return idf


def text_to_vector(text: str, idf: dict[str, float] | None = None) -> list[float]:
    """将文本转换为 128 维稀疏词袋向量（L2 归一化）。

    步骤：
    1. 提取 2-gram 和 3-gram
    2. 对每个 n-gram 做 MD5 哈希得到 [0, 128) 索引
    3. 对应维度 += tf * idf（如提供 IDF）
    4. L2 归一化
    """
    vec = [0.0] * VECTOR_DIM

    # 提取 n-gram
    bigrams = _extract_ngrams(text, 2)
    trigrams = _extract_ngrams(text, 3)
    all_ngrams = bigrams + trigrams

    if not all_ngrams:
        return vec

    # 计算 TF（n-gram 频率）
    tf: dict[str, float] = {}
    for ng in all_ngrams:
        tf[ng] = tf.get(ng, 0.0) + 1.0

    # 写入向量
    for ng, freq in tf.items():
        idx = _hash_ngram(ng)
        weight = freq
        if idf and ng in idf:
            weight *= idf[ng]
        vec[idx] += weight

    # L2 归一化
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-12:
        vec = [v / norm for v in vec]

    return vec


def _build_idf_from_file() -> dict[str, float]:
    """从已存储的记忆中构建全局 IDF。"""
    records = _load_all()
    if not records:
        return {}

    all_ngrams: list[list[str]] = []
    for rec in records:
        text = rec.get("text", "")
        bigrams = _extract_ngrams(text, 2)
        trigrams = _extract_ngrams(text, 3)
        all_ngrams.append(bigrams + trigrams)

    return _compute_idf(all_ngrams)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。

    向量已 L2 归一化，点积即为余弦相似度。
    """
    # 点积（向量已归一化）
    dot = sum(x * y for x, y in zip(a, b))
    # 钳制到 [-1, 1] 防止浮点误差
    return max(-1.0, min(1.0, dot))


# ── 数据持久化 ────────────────────────────────────────
def _ensure_dir() -> None:
    """确保数据目录存在。"""
    VECTOR_DIR.mkdir(parents=True, exist_ok=True)


def _load_all() -> list[dict]:
    """加载所有记忆记录。"""
    if not VECTORS_FILE.exists():
        return []
    records = []
    with open(VECTORS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _save_record(record: dict) -> None:
    """追加一条记录到 JSONL 文件。"""
    _ensure_dir()
    with open(VECTORS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _save_all(records: list[dict]) -> None:
    """覆盖写入所有记录。"""
    _ensure_dir()
    with open(VECTORS_FILE, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _prune_oldest(n: int) -> int:
    """删除最旧的 n 条记录，返回实际删除数。"""
    records = _load_all()
    if len(records) <= n:
        _save_all([])
        return len(records)
    # 保留最新的
    records.sort(key=lambda r: r.get("timestamp", 0))
    removed = len(records) - MAX_MEMORIES + 1
    _save_all(records[removed:])
    return removed


# ── CLI 命令 ──────────────────────────────────────────
def cmd_add(text: str, tags: str) -> dict:
    """添加一条记忆。"""
    records = _load_all()

    # 超出上限：删除最旧的
    if len(records) >= MAX_MEMORIES:
        _prune_oldest(len(records) - MAX_MEMORIES + 1)

    # 构建全局 IDF
    idf = _build_idf_from_file()
    vec = text_to_vector(text, idf)

    record = {
        "id": hashlib.sha256(f"{text}{time.time()}".encode()).hexdigest()[:16],
        "text": text,
        "tags": tags,
        "vector": vec,
        "timestamp": time.time(),
    }

    _save_record(record)
    result = {
        "status": "ok",
        "action": "add",
        "id": record["id"],
        "text_preview": text[:80] + ("..." if len(text) > 80 else ""),
        "vector_dim": len(vec),
        "total_memories": len(_load_all()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def cmd_search(query: str, top_k: int = 5) -> dict:
    """语义搜索最相关的记忆。"""
    records = _load_all()
    if not records:
        result = {
            "status": "ok",
            "action": "search",
            "query": query,
            "results": [],
            "total_memories": 0,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # 构建查询向量（使用全局 IDF 加权）
    idf = _build_idf_from_file()
    query_vec = text_to_vector(query, idf)

    # 计算相似度
    scored: list[tuple[float, dict]] = []
    for rec in records:
        sim = cosine_similarity(query_vec, rec.get("vector", [0] * VECTOR_DIM))
        scored.append((sim, rec))

    # 按相似度降序排序
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    result = {
        "status": "ok",
        "action": "search",
        "query": query,
        "results": [
            {
                "id": rec["id"],
                "text": rec["text"],
                "tags": rec.get("tags", ""),
                "score": round(sim, 6),
                "timestamp": rec.get("timestamp", 0),
            }
            for sim, rec in top
            if sim > 0.0
        ],
        "total_memories": len(records),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def cmd_stats() -> dict:
    """统计信息。"""
    records = _load_all()
    total = len(records)
    usage_pct = round(total / MAX_MEMORIES * 100, 1) if MAX_MEMORIES > 0 else 0

    # 标签统计
    tag_counts: dict[str, int] = {}
    for rec in records:
        tags_str = rec.get("tags", "")
        if tags_str:
            for tag in tags_str.split(","):
                tag = tag.strip()
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # 向量维度统计（非零维度数平均值）
    nonzero_dims = []
    for rec in records:
        vec = rec.get("vector", [])
        nonzero = sum(1 for v in vec if abs(v) > 1e-12)
        nonzero_dims.append(nonzero)
    avg_nonzero = round(sum(nonzero_dims) / total, 1) if total > 0 else 0

    result = {
        "status": "ok",
        "action": "stats",
        "total_memories": total,
        "max_capacity": MAX_MEMORIES,
        "usage_percent": usage_pct,
        "storage_file": str(VECTORS_FILE),
        "vector_dim": VECTOR_DIM,
        "avg_nonzero_dims": avg_nonzero,
        "top_tags": sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def cmd_inject(query: str) -> dict:
    """注入最相关的记忆（格式化为 AI 可用的上下文）。"""
    search_result = cmd_search(query, top_k=5)

    results = search_result.get("results", [])
    if not results:
        output = {
            "status": "ok",
            "action": "inject",
            "injected": False,
            "message": "未找到相关记忆。",
        }
    else:
        # 构建注入文本
        lines = ["## 🧠 向量记忆检索结果\n"]
        lines.append(f"查询: \"{query}\"\n")
        lines.append("相关记忆:\n")
        for i, r in enumerate(results, 1):
            sim_pct = round(r["score"] * 100, 1)
            tags_str = f" [{r['tags']}]" if r.get("tags") else ""
            lines.append(f"**{i}.** (相似度 {sim_pct}%){tags_str}")
            lines.append(f"  {r['text']}\n")

        inject_text = "\n".join(lines)
        output = {
            "status": "ok",
            "action": "inject",
            "injected": True,
            "injection_text": inject_text,
            "top_score": round(results[0]["score"], 6) if results else 0,
        }

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output


# ── 入口 ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="向量记忆检索系统（对标 Mem0）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # add
    p_add = subparsers.add_parser("add", help="添加记忆")
    p_add.add_argument("--text", required=True, help="记忆文本内容")
    p_add.add_argument("--tags", default="", help="标签（逗号分隔）")

    # search
    p_search = subparsers.add_parser("search", help="语义搜索记忆")
    p_search.add_argument("--query", required=True, help="搜索查询文本")
    p_search.add_argument("--top-k", type=int, default=5, help="返回结果数（默认 5）")

    # stats
    subparsers.add_parser("stats", help="统计信息")

    # inject
    p_inject = subparsers.add_parser("inject", help="注入最相关记忆（AI 上下文）")
    p_inject.add_argument("--query", required=True, help="搜索查询文本")

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args.text, args.tags)
    elif args.command == "search":
        cmd_search(args.query, args.top_k)
    elif args.command == "stats":
        cmd_stats()
    elif args.command == "inject":
        cmd_inject(args.query)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()