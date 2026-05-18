#!/usr/bin/env python3
"""
Few-Shot Selector — Few-Shot 自动选例 (对标 DSPy)

核心思想：从历史成功案例库中匹配最相似的 few-shot 示例，提升模型准确率。

对标论文:
  - DSPy (2023/2024): 声明式 prompt 编程框架，few-shot 自动优化
  - MIPROv2 (2024): DSPy 的优化器，BootstrapFewShot + Bayesian 自动选例

设计:
  1. 案例库：~/.hermes/few_shots/examples.jsonl（每条包含 task, output, tags, timestamp）
  2. 相似度算法：TF-IDF 关键词加权（cosine-like）
     - 抽取 task 关键词 → 与案例库中每条关键词向量比较
     - 返回 top-k
  3. 注入格式：
     ## 💡 参考案例
     ### 案例1: [任务摘要]
     [输出摘要...]

命令行用法:
  python3 few_shot_selector.py select --task "写一个 Python 脚本处理 JSON" --k 3
  python3 few_shot_selector.py select --task "..." --k 3 --tags "python,coding"
  python3 few_shot_selector.py add --task "写 Python 脚本" --output "import json..." --tags "python,script"
  python3 few_shot_selector.py inject --task "..." --k 3
  python3 few_shot_selector.py list --tags "python"
  python3 few_shot_selector.py stats
"""

import argparse
import json
import os
import re
import sys
import time
import math
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter, defaultdict
from typing import Optional

# ─── 路径配置 ─────────────────────────────────────────────

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
FEW_SHOTS_DIR = HERMES_HOME / "few_shots"
EXAMPLES_FILE = FEW_SHOTS_DIR / "examples.jsonl"
os.makedirs(FEW_SHOTS_DIR, exist_ok=True)

# ─── 常量 ─────────────────────────────────────────────────

MAX_EXAMPLES = 5000                        # 案例库最大容量


# ─── 工具函数 ─────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tokenize(text: str) -> list[str]:
    """分词：中英文关键词抽取"""
    # 中文：连续中文字符（≥2 字符）
    cn_tokens = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    # 英文：连续字母/数字（≥2 字符）
    en_tokens = re.findall(r"[a-zA-Z0-9]{2,}", text.lower())
    return cn_tokens + en_tokens


# ─── TF-IDF 向量化 ────────────────────────────────────────

def _compute_tf(tokens: list[str]) -> dict:
    """计算词频（TF）"""
    tf = Counter(tokens)
    total = len(tokens) or 1
    return {word: count / total for word, count in tf.items()}


def _compute_idf(documents: list[list[str]]) -> dict:
    """计算逆文档频率（IDF）"""
    N = len(documents) or 1
    df = Counter()
    for doc in documents:
        df.update(set(doc))

    idf = {}
    for word, count in df.items():
        idf[word] = math.log((N + 1) / (count + 1)) + 1  # smooth IDF
    return idf


def _build_tfidf_vectors(documents: list[list[str]]) -> tuple[list[dict], dict]:
    """构建 TF-IDF 向量列表"""
    tf_list = [_compute_tf(doc) for doc in documents]
    idf = _compute_idf(documents)

    vectors = []
    for tf in tf_list:
        vec = {word: tf[word] * idf.get(word, 0) for word in tf}
        vectors.append(vec)
    return vectors, idf


def _tfidf_vectorize(tokens: list[str], idf: dict) -> dict:
    """对单个查询做 TF-IDF 向量化"""
    tf = _compute_tf(tokens)
    vec = {word: tf[word] * idf.get(word, 0) for word in tf}
    return vec


def _cosine_similarity(vec_a: dict, vec_b: dict) -> float:
    """计算两个稀疏向量的余弦相似度"""
    if not vec_a or not vec_b:
        return 0.0

    # 点积
    dot_product = sum(vec_a.get(k, 0) * vec_b.get(k, 0) for k in set(vec_a) | set(vec_b))

    # 模长
    norm_a = math.sqrt(sum(v ** 2 for v in vec_a.values()))
    norm_b = math.sqrt(sum(v ** 2 for v in vec_b.values()))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot_product / (norm_a * norm_b)


# ─── 案例库管理 ───────────────────────────────────────────

def load_examples() -> list[dict]:
    """加载全部案例"""
    if EXAMPLES_FILE.exists():
        examples = []
        try:
            for line in EXAMPLES_FILE.read_text().strip().split("\n"):
                if line.strip():
                    examples.append(json.loads(line))
        except Exception:
            pass
        return examples
    return []


def add_example(task: str, output: str, tags: list[str] = None) -> dict:
    """添加一条成功案例"""
    examples = load_examples()

    # 容量管理：超出上限删最早的
    if len(examples) >= MAX_EXAMPLES:
        examples = examples[-(MAX_EXAMPLES - 1):]

    entry = {
        "id": hashlib.sha256(f"{task}{_now_iso()}".encode()).hexdigest()[:16],
        "task": task,
        "output": output,
        "tags": tags or [],
        "timestamp": _now_iso(),
    }

    examples.append(entry)

    # 回写
    EXAMPLES_FILE.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in examples) + "\n"
    )

    return entry


def _extract_summary(text: str, max_len: int = 60) -> str:
    """提取文本摘要（前 max_len 字符）"""
    clean = text.strip().replace("\n", " ")
    if len(clean) > max_len:
        return clean[:max_len] + "..."
    return clean


# ─── 相似度匹配 ───────────────────────────────────────────

def select_similar(task: str, k: int = 3, filter_tags: Optional[list[str]] = None,
                   examples: Optional[list[dict]] = None) -> list[dict]:
    """
    从案例库中匹配与 task 最相似的 top-k 示例。

    算法：
      1. 构建全部案例的 TF-IDF 向量（基于 task 字段）
      2. 对查询 task 做 TF-IDF 向量化
      3. 计算余弦相似度并排序
      4. 返回 top-k
    """
    if examples is None:
        examples = load_examples()

    if not examples:
        return []

    # 标签过滤
    if filter_tags:
        filter_set = set(filter_tags)
        examples = [
            e for e in examples
            if filter_set & set(e.get("tags", []))
        ]

    if not examples:
        return []

    # 计算 task 字段的 TF-IDF 向量
    doc_tokens = [_tokenize(e.get("task", "")) for e in examples]
    vectors, idf = _build_tfidf_vectors(doc_tokens)

    # 查询向量化
    query_tokens = _tokenize(task)
    query_vec = _tfidf_vectorize(query_tokens, idf)

    # 计算相似度
    scored = []
    for i, vec in enumerate(vectors):
        sim = _cosine_similarity(query_vec, vec)
        scored.append((sim, examples[i]))

    # 排序（相似度降序）
    scored.sort(key=lambda x: x[0], reverse=True)

    # 去重（基于 task 内容相似度 > 0.9 的去重，保留第一个）
    top_k = []
    seen_tasks = []
    for sim, ex in scored:
        ex_tokens = set(_tokenize(ex.get("task", "")))
        is_dup = False
        for seen in seen_tasks:
            if _keyword_overlap(ex_tokens, seen) > 0.9:
                is_dup = True
                break
        if not is_dup:
            top_k.append({"similarity": round(sim, 4), "example": ex})
            seen_tasks.append(ex_tokens)
        if len(top_k) >= k:
            break

    return top_k


def _keyword_overlap(tokens_a: set, tokens_b: set) -> float:
    """计算两组关键词的 Jaccard 相似度"""
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# ─── 注入格式 ─────────────────────────────────────────────

def build_inject_text(task: str, k: int = 3, filter_tags: list[str] = None,
                      examples: list[dict] = None) -> dict:
    """
    构建 few-shot 注入文本

    返回:
      {
        "inject_text": str,
        "examples_count": int,
        "top_similarity": float,
        "selected_examples": list[dict]
      }
    """
    selected = select_similar(task, k, filter_tags, examples)

    if not selected:
        return {
            "inject_text": "",
            "examples_count": 0,
            "top_similarity": 0.0,
            "selected_examples": [],
        }

    blocks = ["## 💡 参考案例\n"]
    for i, item in enumerate(selected, 1):
        ex = item["example"]
        task_summary = _extract_summary(ex.get("task", ""), 80)
        output_summary = _extract_summary(ex.get("output", ""), 200)

        block = (
            f"### 案例{i}: {task_summary}\n"
            f"**相似度**: {item['similarity']}\n\n"
            f"{output_summary}"
        )
        blocks.append(block)

    inject_text = "\n\n".join(blocks)
    return {
        "inject_text": inject_text,
        "examples_count": len(selected),
        "top_similarity": selected[0]["similarity"] if selected else 0.0,
        "selected_examples": [
            {
                "id": s["example"].get("id", ""),
                "task": s["example"].get("task", ""),
                "similarity": s["similarity"],
                "tags": s["example"].get("tags", []),
            }
            for s in selected
        ],
    }


# ─── 统计 ─────────────────────────────────────────────────

def get_stats() -> dict:
    """获取案例库统计"""
    examples = load_examples()

    all_tags = []
    for e in examples:
        all_tags.extend(e.get("tags", []))

    tag_counter = Counter(all_tags)

    return {
        "total_examples": len(examples),
        "max_capacity": MAX_EXAMPLES,
        "unique_tags": len(tag_counter),
        "top_tags": tag_counter.most_common(10),
        "storage_file": str(EXAMPLES_FILE),
        "last_updated": _now_iso(),
    }


def list_examples(filter_tags: list[str] = None, limit: int = 20) -> list[dict]:
    """列出案例（可按标签过滤）"""
    examples = load_examples()

    if filter_tags:
        filter_set = set(filter_tags)
        examples = [
            e for e in examples
            if filter_set & set(e.get("tags", []))
        ]

    # 按时间倒序
    examples.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    return examples[:limit]


# ─── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Few-Shot Selector — Few-Shot 自动选例 (对标 DSPy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 选 top-3 相似示例
  python3 few_shot_selector.py select --task "写一个 Python 脚本处理 JSON" --k 3

  # 标签过滤选例
  python3 few_shot_selector.py select --task "数据分析" --k 5 --tags "python,data"

  # 添加成功案例
  python3 few_shot_selector.py add --task "写 Python 脚本" \\
      --output "import json; data = json.loads(content)" --tags "python,json"

  # 直接在 prompt 中注入
  python3 few_shot_selector.py inject --task "处理 CSV 文件" --k 3

  # 列出案例
  python3 few_shot_selector.py list --tags "python"

  # 统计
  python3 few_shot_selector.py stats
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # select
    p_select = subparsers.add_parser("select", help="选 top-k 相似示例")
    p_select.add_argument("--task", type=str, required=True, help="查询任务描述")
    p_select.add_argument("--k", type=int, default=3, help="返回 top-k 个示例")
    p_select.add_argument("--tags", type=str, default="", help="逗号分隔的标签过滤")

    # add
    p_add = subparsers.add_parser("add", help="添加成功案例")
    p_add.add_argument("--task", type=str, required=True, help="任务描述")
    p_add.add_argument("--output", type=str, required=True, help="成功输出")
    p_add.add_argument("--tags", type=str, default="", help="逗号分隔的标签")

    # inject
    p_inject = subparsers.add_parser("inject", help="构建注入文本")
    p_inject.add_argument("--task", type=str, required=True, help="查询任务描述")
    p_inject.add_argument("--k", type=int, default=3, help="注入 top-k 个示例")
    p_inject.add_argument("--tags", type=str, default="", help="逗号分隔的标签过滤")

    # list
    p_list = subparsers.add_parser("list", help="列出案例")
    p_list.add_argument("--tags", type=str, default="", help="逗号分隔的标签过滤")
    p_list.add_argument("--limit", type=int, default=20, help="返回条数")

    # stats
    subparsers.add_parser("stats", help="案例库统计")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "select":
            filter_tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
            results = select_similar(args.task, args.k, filter_tags)

            output = {
                "query": args.task,
                "k": args.k,
                "filter_tags": filter_tags,
                "results_count": len(results),
                "results": [
                    {
                        "rank": i + 1,
                        "id": r["example"].get("id", ""),
                        "task": r["example"].get("task", ""),
                        "tags": r["example"].get("tags", []),
                        "similarity": r["similarity"],
                        "output_preview": _extract_summary(r["example"].get("output", ""), 100),
                    }
                    for i, r in enumerate(results)
                ],
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))

        elif args.command == "add":
            tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
            entry = add_example(args.task, args.output, tags)
            print(json.dumps({"status": "added", "example": entry}, ensure_ascii=False, indent=2))

        elif args.command == "inject":
            filter_tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
            result = build_inject_text(args.task, args.k, filter_tags)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "list":
            filter_tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
            results = list_examples(filter_tags, args.limit)
            output = {
                "count": len(results),
                "filter_tags": filter_tags,
                "examples": [
                    {
                        "id": e.get("id", ""),
                        "task": e.get("task", ""),
                        "tags": e.get("tags", []),
                        "timestamp": e.get("timestamp", ""),
                        "output_preview": _extract_summary(e.get("output", ""), 80),
                    }
                    for e in results
                ],
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))

        elif args.command == "stats":
            result = get_stats()
            print(json.dumps(result, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()