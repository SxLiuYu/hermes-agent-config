#!/usr/bin/env python3
"""
Experience Cards System — MemGovern + CodeTracer 风格的结构化经验卡片

对标论文:
  - MemGovern (2025): 结构化经验治理，Index Layer + Resolution Layer 双层检索
  - CodeTracer (2025): 代码错误签名 → 修复策略的因果链追踪

核心设计:
  1. 结构化经验卡片：Index Layer（症状/异常类型/错误签名，用于检索）
                      + Resolution Layer（根因/修复策略/补丁摘要/验证方法）
  2. 自动创建：SubagentStop 后，若 outcomes_grader 评分 ≥ 7，提炼为经验卡片
  3. Search-then-Browse 检索：先广搜 Index Layer 定位候选，再深入查看 Resolution
  4. 存储管理：最多 5000 张卡片，超出自动归档最旧的

技术栈:
  - 纯 Python 标准库 (json, hashlib, time, os, dataclasses, math, collections)
  - 自实现 TF-IDF 关键词匹配（不依赖 sklearn）
  - argparse 命令行接口 + subprocess 可调用

命令行用法:
  python3 ~/.hermes/tools/experience_cards.py create \\
      --symptoms "KeyError in data processing" \\
      --root-cause "Missing null check" \\
      --fix-strategy "Add None guard" \\
      --tags "python,data,bug"
  python3 ~/.hermes/tools/experience_cards.py search --query "null check key error" --top 5
  python3 ~/.hermes/tools/experience_cards.py browse --id exp_xxx
  python3 ~/.hermes/tools/experience_cards.py list --tags "python"
  python3 ~/.hermes/tools/experience_cards.py stats
"""

import json
import os
import re
import time
import math
import hashlib
import argparse
from dataclasses import dataclass, field, asdict
from collections import defaultdict, Counter
from typing import Optional

# ─── 数据存储路径 ─────────────────────────────────────────

HERMES_DIR = os.path.expanduser("~/.hermes")
CARDS_DIR = os.path.join(HERMES_DIR, "experience_cards")
INDEX_FILE = os.path.join(CARDS_DIR, "cards_index.json")
ARCHIVE_DIR = os.path.join(CARDS_DIR, "archive")
os.makedirs(CARDS_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# 系统上限
MAX_CARDS = 5000

# ─── 数据模型 ─────────────────────────────────────────────


@dataclass
class IndexLayer:
    """
    检索层 — 用于快速匹配和候选定位。
    症状、异常类型、错误签名等信息确保卡片在 search 阶段可被命中。
    """
    symptoms: str = ""              # 症状描述（自然语言）
    error_signature: str = ""       # 错误签名（标准化错误类型 + 关键特征）
    tags: list[str] = field(default_factory=list)  # 标签列表
    context_hint: str = ""          # 上下文提示（适用的场景/语言/框架）


@dataclass
class ResolutionLayer:
    """
    解析层 — 在 search 定位候选后，browse 阶段深入展示。
    包含根因、修复策略、补丁摘要和验证方法。
    """
    root_cause: str = ""            # 根因分析
    fix_strategy: str = ""          # 修复策略
    patch_summary: str = ""         # 补丁摘要（diff 或代码片段）
    verification: str = ""          # 验证方法（如何确认修复有效）


@dataclass
class ExperienceCard:
    """完整的经验卡片"""
    card_id: str
    index_layer: IndexLayer = field(default_factory=IndexLayer)
    resolution_layer: ResolutionLayer = field(default_factory=ResolutionLayer)
    source_task_id: str = ""        # 来源任务 ID
    source_score: float = 0.0       # 来源任务评分
    created_at: float = 0.0         # 创建时间戳
    updated_at: float = 0.0         # 最后更新时间
    access_count: int = 0           # 被检索次数（热度）
    success_count: int = 0          # 被标记为有效的次数
    metadata: dict = field(default_factory=dict)  # 扩展元数据

    def __post_init__(self):
        now = time.time()
        if self.created_at == 0.0:
            self.created_at = now
        if self.updated_at == 0.0:
            self.updated_at = now


# ─── ID 生成 ──────────────────────────────────────────────


def generate_card_id(symptoms: str = "", timestamp: float = None) -> str:
    """生成唯一卡片 ID: exp_{timestamp}_{hash8}"""
    ts = int(timestamp or time.time())
    raw = f"{ts}_{symptoms}"
    hash8 = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"exp_{ts}_{hash8}"


# ─── 序列化 ───────────────────────────────────────────────


def _serialize_card(card: ExperienceCard) -> dict:
    """将 ExperienceCard 序列化为纯 dict（无 dataclass 嵌套）"""
    return {
        "card_id": card.card_id,
        "index_layer": {
            "symptoms": card.index_layer.symptoms,
            "error_signature": card.index_layer.error_signature,
            "tags": card.index_layer.tags,
            "context_hint": card.index_layer.context_hint,
        },
        "resolution_layer": {
            "root_cause": card.resolution_layer.root_cause,
            "fix_strategy": card.resolution_layer.fix_strategy,
            "patch_summary": card.resolution_layer.patch_summary,
            "verification": card.resolution_layer.verification,
        },
        "source_task_id": card.source_task_id,
        "source_score": card.source_score,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
        "access_count": card.access_count,
        "success_count": card.success_count,
        "metadata": card.metadata,
    }


def _deserialize_card(data: dict) -> ExperienceCard:
    """从 dict 反序列化 ExperienceCard"""
    il = data.get("index_layer", {})
    rl = data.get("resolution_layer", {})
    return ExperienceCard(
        card_id=data.get("card_id", ""),
        index_layer=IndexLayer(
            symptoms=il.get("symptoms", ""),
            error_signature=il.get("error_signature", ""),
            tags=il.get("tags", []),
            context_hint=il.get("context_hint", ""),
        ),
        resolution_layer=ResolutionLayer(
            root_cause=rl.get("root_cause", ""),
            fix_strategy=rl.get("fix_strategy", ""),
            patch_summary=rl.get("patch_summary", ""),
            verification=rl.get("verification", ""),
        ),
        source_task_id=data.get("source_task_id", ""),
        source_score=data.get("source_score", 0.0),
        created_at=data.get("created_at", 0.0),
        updated_at=data.get("updated_at", 0.0),
        access_count=data.get("access_count", 0),
        success_count=data.get("success_count", 0),
        metadata=data.get("metadata", {}),
    )


def _card_path(card_id: str) -> str:
    """获取卡片文件路径"""
    return os.path.join(CARDS_DIR, f"{card_id}.json")


# ─── 原始 TF-IDF 实现 ────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """简单分词：英文按空白和标点分割，同时保留中文 n-gram（2-3 字）"""
    text = text.lower()
    # 提取英文单词
    words = re.findall(r'[a-z0-9_]+', text)
    # 提取中文 2-gram 和 3-gram
    chinese_chars = re.findall(r'[\u4e00-\u9fff]+', text)
    for segment in chinese_chars:
        for n in [2, 3]:
            for i in range(len(segment) - n + 1):
                words.append(segment[i:i + n])
    return words


def _compute_tf(tokens: list[str]) -> dict[str, float]:
    """计算词频（Term Frequency），归一化到 [0,1]"""
    if not tokens:
        return {}
    counts = Counter(tokens)
    max_count = max(counts.values())
    return {word: count / max_count for word, count in counts.items()}


def _build_idf(all_docs: list[list[str]]) -> dict[str, float]:
    """建立逆文档频率（Inverse Document Frequency）索引"""
    N = len(all_docs)
    if N == 0:
        return {}
    df = Counter()
    for doc_tokens in all_docs:
        df.update(set(doc_tokens))
    return {word: math.log((N + 1) / (count + 1)) + 1 for word, count in df.items()}


def _compute_tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """计算 TF-IDF 向量"""
    tf = _compute_tf(tokens)
    return {word: tf[word] * idf.get(word, 0) for word in tf}


def _cosine_similarity(vec1: dict[str, float], vec2: dict[str, float]) -> float:
    """计算两个稀疏向量的余弦相似度"""
    all_keys = set(vec1.keys()) | set(vec2.keys())
    dot = sum(vec1.get(k, 0) * vec2.get(k, 0) for k in all_keys)
    norm1 = math.sqrt(sum(v ** 2 for v in vec1.values()))
    norm2 = math.sqrt(sum(v ** 2 for v in vec2.values()))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


class TfidfIndexer:
    """
    纯 Python TF-IDF 索引器。
    为每张卡片的 Index Layer 建立 TF-IDF 向量，支持快速相似度检索。
    """

    def __init__(self):
        self.idf: dict[str, float] = {}
        self.doc_vectors: dict[str, dict[str, float]] = {}  # card_id → TF-IDF vector
        # 倒排索引: token → [(card_id, tfidf_weight), ...]
        self.inverted_index: dict[str, list[tuple[str, float]]] = defaultdict(list)

    def build(self, cards: list[ExperienceCard]):
        """从卡片列表构建索引"""
        # 对每张卡片构建 index text
        all_tokens: list[list[str]] = []
        doc_token_map: dict[str, list[str]] = {}

        for card in cards:
            il = card.index_layer
            index_text = f"{il.symptoms} {il.error_signature} {il.context_hint} {' '.join(il.tags)}"
            tokens = _tokenize(index_text)
            all_tokens.append(tokens)
            doc_token_map[card.card_id] = tokens

        # 计算 IDF
        self.idf = _build_idf(all_tokens)

        # 计算每个文档的 TF-IDF 向量，并建立倒排索引
        self.inverted_index.clear()
        for card in cards:
            tokens = doc_token_map.get(card.card_id, [])
            vec = _compute_tfidf_vector(tokens, self.idf)
            self.doc_vectors[card.card_id] = vec
            for token, weight in vec.items():
                if weight > 0:
                    self.inverted_index[token].append((card.card_id, weight))

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """
        使用倒排索引加速检索。
        阶段 1（Search）：在 Index Layer 中匹配候选卡片。
        返回按 TF-IDF 余弦相似度排序的 (card_id, score) 列表。
        """
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        # 通过倒排索引快速定位候选
        candidates: set[str] = set()
        for token in query_tokens:
            for card_id, _ in self.inverted_index.get(token, []):
                candidates.add(card_id)

        if not candidates:
            # 回退：暴力扫描（仅在没有候选时）
            query_vec = _compute_tfidf_vector(query_tokens, self.idf)
            if not query_vec:
                return []
            scores = []
            for card_id, doc_vec in self.doc_vectors.items():
                sim = _cosine_similarity(query_vec, doc_vec)
                if sim > 0:
                    scores.append((card_id, sim))
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:top_k]

        # 在候选集中精确计算相似度
        query_vec = _compute_tfidf_vector(query_tokens, self.idf)
        if not query_vec:
            return []
        scores = []
        for card_id in candidates:
            doc_vec = self.doc_vectors.get(card_id, {})
            sim = _cosine_similarity(query_vec, doc_vec)
            if sim > 0:
                scores.append((card_id, sim))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


# ─── 索引文件管理 ───────────────────────────────────────


def _load_index() -> dict:
    """加载索引文件（轻量元数据集）"""
    if not os.path.exists(INDEX_FILE):
        return {"cards": {}, "total": 0}
    try:
        with open(INDEX_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"cards": {}, "total": 0}


def _save_index(index: dict):
    """保存索引文件"""
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def _rebuild_index():
    """扫描所有 .json 文件重建索引"""
    cards_meta = {}
    for fname in os.listdir(CARDS_DIR):
        if fname.endswith(".json") and fname != "cards_index.json":
            path = os.path.join(CARDS_DIR, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
                card_id = data.get("card_id", fname.replace(".json", ""))
                cards_meta[card_id] = {
                    "symptoms": (data.get("index_layer", {}) or {}).get("symptoms", ""),
                    "error_signature": (data.get("index_layer", {}) or {}).get("error_signature", ""),
                    "tags": (data.get("index_layer", {}) or {}).get("tags", []),
                    "created_at": data.get("created_at", 0),
                    "access_count": data.get("access_count", 0),
                    "source_score": data.get("source_score", 0),
                }
            except (json.JSONDecodeError, KeyError):
                continue
    index = {"cards": cards_meta, "total": len(cards_meta)}
    _save_index(index)
    return index


# ─── 核心 CRUD ──────────────────────────────────────────


def create_card(
    symptoms: str,
    root_cause: str = "",
    fix_strategy: str = "",
    error_signature: str = "",
    patch_summary: str = "",
    verification: str = "",
    tags: list[str] = None,
    context_hint: str = "",
    source_task_id: str = "",
    source_score: float = 0.0,
    metadata: dict = None,
) -> ExperienceCard:
    """
    创建一张新的经验卡片。
    这是自动创建 hook 的核心入口。
    """
    card_id = generate_card_id(symptoms)

    # 自动推断错误签名（如果未提供）
    if not error_signature and symptoms:
        error_signature = _extract_error_signature(symptoms)

    card = ExperienceCard(
        card_id=card_id,
        index_layer=IndexLayer(
            symptoms=symptoms,
            error_signature=error_signature,
            tags=tags or [],
            context_hint=context_hint,
        ),
        resolution_layer=ResolutionLayer(
            root_cause=root_cause,
            fix_strategy=fix_strategy,
            patch_summary=patch_summary,
            verification=verification,
        ),
        source_task_id=source_task_id,
        source_score=source_score,
        metadata=metadata or {},
    )

    # 持久化卡片文件
    card_path = _card_path(card_id)
    with open(card_path, "w") as f:
        json.dump(_serialize_card(card), f, indent=2, ensure_ascii=False)

    # 更新索引
    _add_to_index(card)

    # 检查容量限制
    _enforce_capacity()

    return card


def _extract_error_signature(symptoms: str) -> str:
    """
    从症状描述中提取标准化错误签名。
    例如 "KeyError in data processing" → "KeyError"
    """
    # 匹配常见异常类型
    patterns = [
        r'\b([A-Z][a-zA-Z]*Error)\b',       # ValueError, KeyError, etc.
        r'\b([A-Z][a-zA-Z]*Exception)\b',    # RuntimeException, etc.
        r'\b([A-Z][a-zA-Z]*Warning)\b',      # DeprecationWarning, etc.
        r'\b(TypeError|SyntaxError|NameError|AttributeError)\b',
    ]
    for pat in patterns:
        m = re.search(pat, symptoms)
        if m:
            return m.group(1)
    # 尝试关键词
    keywords = ["timeout", "crash", "panic", "deadlock", "race condition",
                "memory leak", "segfault", "connection refused", "permission denied",
                "not found", "already exists", "null pointer", "undefined"]
    lower = symptoms.lower()
    for kw in keywords:
        if kw in lower:
            return kw.title()
    return "unknown"


def _add_to_index(card: ExperienceCard):
    """将卡片元数据加入索引"""
    index = _load_index()
    index["cards"][card.card_id] = {
        "symptoms": card.index_layer.symptoms,
        "error_signature": card.index_layer.error_signature,
        "tags": card.index_layer.tags,
        "created_at": card.created_at,
        "access_count": card.access_count,
        "source_score": card.source_score,
    }
    index["total"] = len(index["cards"])
    _save_index(index)


def _remove_from_index(card_id: str):
    """从索引中移除卡片"""
    index = _load_index()
    index["cards"].pop(card_id, None)
    index["total"] = len(index["cards"])
    _save_index(index)


def _enforce_capacity():
    """确保卡片总数不超过 MAX_CARDS"""
    index = _load_index()
    if index["total"] <= MAX_CARDS:
        return

    # 按创建时间升序排列，归档最旧的
    cards_sorted = sorted(index["cards"].items(), key=lambda x: x[1].get("created_at", 0))
    to_archive = cards_sorted[:(index["total"] - MAX_CARDS)]

    for card_id, _ in to_archive:
        _archive_card(card_id)

    # 重新计算
    _rebuild_index()


def _archive_card(card_id: str):
    """将卡片归档到 archive 目录"""
    src = _card_path(card_id)
    dst = os.path.join(ARCHIVE_DIR, f"{card_id}.json")
    if os.path.exists(src):
        try:
            os.rename(src, dst)
        except OSError:
            import shutil
            shutil.move(src, dst)
    _remove_from_index(card_id)


def load_card(card_id: str) -> Optional[ExperienceCard]:
    """加载单张卡片并更新访问计数（阶段 2：Browse）"""
    path = _card_path(card_id)

    # 尝试 archive 目录
    if not os.path.exists(path):
        archive_path = os.path.join(ARCHIVE_DIR, f"{card_id}.json")
        if os.path.exists(archive_path):
            path = archive_path
        else:
            return None

    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

    card = _deserialize_card(data)

    # 更新访问计数
    card.access_count += 1
    card.updated_at = time.time()

    # 写回（如果不是 archive 中的）
    if not path.startswith(ARCHIVE_DIR):
        with open(path, "w") as f:
            json.dump(_serialize_card(card), f, indent=2, ensure_ascii=False)
        # 更新索引中的 access_count
        index = _load_index()
        if card_id in index["cards"]:
            index["cards"][card_id]["access_count"] = card.access_count
            _save_index(index)

    return card


def load_all_cards() -> list[ExperienceCard]:
    """加载所有卡片"""
    index = _load_index()
    cards = []
    for card_id in index.get("cards", {}):
        path = _card_path(card_id)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                cards.append(_deserialize_card(data))
            except (json.JSONDecodeError, FileNotFoundError):
                continue
    cards.sort(key=lambda c: c.created_at, reverse=True)
    return cards


def search_cards(query: str, top_k: int = 10) -> list[dict]:
    """
    Search-then-Browse 的阶段 1：Search。
    使用 TF-IDF 在 Index Layer 中搜索候选卡片。
    返回匹配摘要列表（不包含完整 Resolution Layer）。
    """
    cards = load_all_cards()
    if not cards:
        return []

    indexer = TfidfIndexer()
    indexer.build(cards)
    results = indexer.search(query, top_k)

    output = []
    for card_id, score in results:
        card = load_card(card_id)
        if card:
            output.append({
                "card_id": card.card_id,
                "score": round(score, 4),
                "symptoms": card.index_layer.symptoms[:120],
                "error_signature": card.index_layer.error_signature,
                "tags": card.index_layer.tags,
                "root_cause_preview": card.resolution_layer.root_cause[:100],
                "created_at": card.created_at,
                "access_count": card.access_count,
            })
    return output


def browse_card(card_id: str) -> Optional[dict]:
    """
    Search-then-Browse 的阶段 2：Browse。
    查看指定卡片的完整内容（包括完整的 Resolution Layer）。
    自动递增访问计数。
    """
    card = load_card(card_id)
    if card is None:
        return None
    return _serialize_card(card)


def list_cards(tags_filter: list[str] = None, limit: int = 50) -> list[dict]:
    """
    列出卡片，支持标签过滤。
    """
    cards = load_all_cards()
    result = []
    for card in cards:
        if tags_filter:
            card_tags = set(t.lower() for t in card.index_layer.tags)
            filter_tags = set(t.lower() for t in tags_filter)
            if not card_tags & filter_tags:
                continue
        result.append({
            "card_id": card.card_id,
            "symptoms": card.index_layer.symptoms[:100],
            "error_signature": card.index_layer.error_signature,
            "tags": card.index_layer.tags,
            "root_cause": card.resolution_layer.root_cause[:80],
            "created_at": card.created_at,
            "access_count": card.access_count,
            "source_score": card.source_score,
        })
        if len(result) >= limit:
            break
    return result


def get_stats() -> dict:
    """获取卡片系统的统计信息"""
    cards = load_all_cards()
    index = _load_index()

    total = len(cards)
    if total == 0:
        return {
            "total_cards": 0,
            "total_archived": _count_archive(),
            "max_capacity": MAX_CARDS,
            "usage_percent": 0.0,
        }

    # 标签分布
    tag_counts = Counter()
    for card in cards:
        for tag in card.index_layer.tags:
            tag_counts[tag] += 1

    # 错误签名分布
    error_counts = Counter()
    for card in cards:
        sig = card.index_layer.error_signature
        if sig and sig != "unknown":
            error_counts[sig] += 1

    # 时间分布
    if cards:
        newest = max(c.created_at for c in cards)
        oldest = min(c.created_at for c in cards)
    else:
        newest = oldest = 0

    # 评分分布
    scores = [c.source_score for c in cards if c.source_score > 0]

    # 热度排名
    hot_cards = sorted(cards, key=lambda c: c.access_count, reverse=True)[:5]

    return {
        "total_cards": total,
        "total_archived": _count_archive(),
        "max_capacity": MAX_CARDS,
        "usage_percent": round(total / MAX_CARDS * 100, 1),
        "top_tags": tag_counts.most_common(10),
        "top_error_signatures": error_counts.most_common(10),
        "newest_card_ts": newest,
        "oldest_card_ts": oldest,
        "avg_source_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "hot_cards": [
            {"card_id": c.card_id, "access_count": c.access_count,
             "symptoms": c.index_layer.symptoms[:60]}
            for c in hot_cards
        ],
    }


def _count_archive() -> int:
    """统计归档目录中的卡片数"""
    count = 0
    if os.path.exists(ARCHIVE_DIR):
        for fname in os.listdir(ARCHIVE_DIR):
            if fname.endswith(".json"):
                count += 1
    return count


# ─── Hook 接口 ────────────────────────────────────────────


def on_subagent_stop(
    task_goal: str = "",
    task_outcome: str = "",
    task_score: float = 0.0,
    errors: list[str] = None,
    fixes: list[str] = None,
    lessons: list[str] = None,
    task_id: str = "",
    metadata: dict = None,
) -> Optional[str]:
    """
    SubagentStop Hook 入口。
    在 outcomes_grader 评分 ≥ 7 时自动提炼经验卡片。

    返回创建的 card_id，如果不符合条件则返回 None。
    """
    if task_score < 7:
        return None

    symptoms = task_goal
    root_cause = ""
    fix_strategy = ""
    verification = ""

    # 从错误和修复中提炼
    if errors:
        root_cause = "; ".join(errors[:3])
    if fixes:
        fix_strategy = "; ".join(fixes[:3])
    if not fix_strategy and task_outcome == "success":
        fix_strategy = f"Task completed successfully with score {task_score}/10"
    if lessons:
        verification = "; ".join(lessons[:2])

    # 自动生成标签
    auto_tags = _auto_tag(task_goal, errors or [])
    if task_outcome:
        auto_tags.append(task_outcome)

    card = create_card(
        symptoms=symptoms,
        root_cause=root_cause,
        fix_strategy=fix_strategy,
        verification=verification,
        tags=auto_tags,
        source_task_id=task_id,
        source_score=task_score,
        metadata=metadata or {},
    )

    return card.card_id


def _auto_tag(goal: str, errors: list[str]) -> list[str]:
    """从任务目标和错误中自动提取标签"""
    tags = []
    tag_patterns = {
        "bug": ["error", "exception", "fix", "修复", "bug", "failed", "crash", "traceback"],
        "config": ["config", "配置", "设置", "environment", "env", "path"],
        "data": ["data", "数据", "json", "csv", "parse", "format"],
        "api": ["api", "request", "http", "fetch", "response"],
        "python": ["python", "import", "module", "pip", "venv"],
        "shell": ["shell", "bash", "command", "script"],
        "file": ["file", "文件", "读取", "写入", "权限", "permission", "not found"],
        "network": ["network", "网络", "timeout", "connection", "socket"],
        "logic": ["logic", "逻辑", "condition", "check", "validation"],
    }
    text = (goal + " " + " ".join(errors)).lower()
    for tag, patterns in tag_patterns.items():
        if any(p in text for p in patterns):
            tags.append(tag)
    return tags or ["general"]


# ─── CLI ─────────────────────────────────────────────────


def _format_card_for_display(card_data: dict, full: bool = False) -> str:
    """格式化卡片用于终端显示"""
    il = card_data.get("index_layer", {})
    rl = card_data.get("resolution_layer", {})
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(card_data.get("created_at", 0)))

    lines = [
        f"┌─ {card_data['card_id']} ──────────────────────────────────────────────",
        f"│ 📅 Created: {ts}",
        f"│ 🔍 Score: {card_data.get('source_score', 0):.1f}/10  |  Views: {card_data.get('access_count', 0)}",
        f"│ 🏷️  Tags: {', '.join(il.get('tags', [])) or '(none)'}",
        f"│",
        f"│ 📋 Symptoms:",
    ]
    for line in il.get("symptoms", "").split("\n"):
        lines.append(f"│    {line.strip()[:100]}")

    if il.get("error_signature"):
        lines.append(f"│ ⚠️  Error Signature: {il['error_signature']}")

    if full:
        lines.append(f"│")
        lines.append(f"│ 🔧 Root Cause:")
        for line in rl.get("root_cause", "").split("\n"):
            lines.append(f"│    {line.strip()[:100]}")
        lines.append(f"│")
        lines.append(f"│ 🛠️  Fix Strategy:")
        for line in rl.get("fix_strategy", "").split("\n"):
            lines.append(f"│    {line.strip()[:100]}")
        if rl.get("patch_summary"):
            lines.append(f"│")
            lines.append(f"│ 📝 Patch Summary:")
            for line in rl.get("patch_summary", "").split("\n")[:15]:
                lines.append(f"│    {line.strip()[:100]}")
        if rl.get("verification"):
            lines.append(f"│")
            lines.append(f"│ ✅ Verification:")
            for line in rl.get("verification", "").split("\n"):
                lines.append(f"│    {line.strip()[:100]}")

    lines.append(f"└{'─' * 60}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Experience Cards — MemGovern+CodeTracer 风格结构化经验卡片系统",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # create
    create_p = sub.add_parser("create", help="创建经验卡片")
    create_p.add_argument("--symptoms", required=True, help="症状描述")
    create_p.add_argument("--root-cause", default="", help="根因分析")
    create_p.add_argument("--fix-strategy", default="", help="修复策略")
    create_p.add_argument("--error-signature", default="", help="错误签名（自动提取）")
    create_p.add_argument("--patch-summary", default="", help="补丁摘要")
    create_p.add_argument("--verification", default="", help="验证方法")
    create_p.add_argument("--tags", default="", help="逗号分隔标签")
    create_p.add_argument("--context-hint", default="", help="上下文提示")
    create_p.add_argument("--source-task-id", default="", help="来源任务 ID")
    create_p.add_argument("--source-score", type=float, default=0.0, help="来源评分")

    # search
    search_p = sub.add_parser("search", help="搜索经验卡片（阶段 1: Search）")
    search_p.add_argument("--query", required=True, help="搜索查询")
    search_p.add_argument("--top", type=int, default=10, help="返回数量（默认 10）")

    # browse
    browse_p = sub.add_parser("browse", help="查看卡片详情（阶段 2: Browse）")
    browse_p.add_argument("--id", required=True, help="卡片 ID")

    # list
    list_p = sub.add_parser("list", help="列出经验卡片")
    list_p.add_argument("--tags", default="", help="逗号分隔标签过滤")
    list_p.add_argument("--limit", type=int, default=50, help="最大返回数")
    list_p.add_argument("--json", action="store_true", help="以 JSON 格式输出")

    # stats
    sub.add_parser("stats", help="统计信息")

    # rebuild-index
    sub.add_parser("rebuild-index", help="重建索引文件")

    # hook
    hook_p = sub.add_parser("hook", help="SubagentStop Hook 入口")
    hook_p.add_argument("--goal", default="", help="任务目标")
    hook_p.add_argument("--outcome", default="", help="任务结果")
    hook_p.add_argument("--score", type=float, default=0.0, help="任务评分")
    hook_p.add_argument("--errors", default="", help="错误列表（逗号分隔）")
    hook_p.add_argument("--fixes", default="", help="修复列表（逗号分隔）")
    hook_p.add_argument("--lessons", default="", help="经验教训（逗号分隔）")
    hook_p.add_argument("--task-id", default="", help="来源任务 ID")

    args = parser.parse_args()

    if args.command == "create":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        card = create_card(
            symptoms=args.symptoms,
            root_cause=args.root_cause,
            fix_strategy=args.fix_strategy,
            error_signature=args.error_signature,
            patch_summary=args.patch_summary,
            verification=args.verification,
            tags=tags,
            context_hint=args.context_hint,
            source_task_id=args.source_task_id,
            source_score=args.source_score,
        )
        print(f"✅ 卡片已创建: {card.card_id}")
        print(f"   Symptoms: {card.index_layer.symptoms[:80]}")
        if card.index_layer.error_signature:
            print(f"   Signature: {card.index_layer.error_signature}")
        if tags:
            print(f"   Tags: {', '.join(tags)}")

    elif args.command == "search":
        results = search_cards(args.query, args.top)
        if not results:
            print("未找到匹配的经验卡片")
        else:
            print(f"🔍 搜索: \"{args.query}\" — 找到 {len(results)} 条结果\n")
            for i, r in enumerate(results, 1):
                ts = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
                print(f"{i}. [{r['score']:.4f}] {r['card_id']} ({ts})")
                print(f"   📋 {r['symptoms']}")
                if r.get("error_signature"):
                    print(f"   ⚠️  {r['error_signature']}")
                if r.get("tags"):
                    print(f"   🏷️  {', '.join(r['tags'])}")
                print(f"   💡 {r.get('root_cause_preview', '')}\n")

    elif args.command == "browse":
        card_data = browse_card(args.id)
        if card_data is None:
            print(f"❌ 卡片不存在: {args.id}")
            sys.exit(1)
        print(_format_card_for_display(card_data, full=True))

    elif args.command == "list":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
        results = list_cards(tags_filter=tags, limit=args.limit)
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        elif not results:
            print("没有找到卡片")
        else:
            filter_info = f" (tags: {', '.join(tags)})" if tags else ""
            print(f"📋 经验卡片列表{filter_info} — {len(results)} 张 (显示前 {args.limit})\n")
            for i, r in enumerate(results, 1):
                ts = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
                print(f"{i}. {r['card_id']} ({ts})  [views: {r['access_count']}]")
                print(f"   📋 {r['symptoms']}")
                if r.get("error_signature"):
                    print(f"   ⚠️  {r['error_signature']}")
                if r.get("tags"):
                    print(f"   🏷️  {', '.join(r['tags'])}")
                print(f"   Score: {r.get('source_score', 0):.1f}/10\n")

    elif args.command == "stats":
        stats = get_stats()
        print("📊 经验卡片系统统计")
        print(f"   总卡片数: {stats['total_cards']} / {stats['max_capacity']} ({stats['usage_percent']}%)")
        print(f"   已归档: {stats['total_archived']}")
        if stats["total_cards"] > 0:
            print(f"   平均评分: {stats['avg_source_score']}/10")
            if stats.get("newest_card_ts"):
                newest = time.strftime("%Y-%m-%d %H:%M", time.localtime(stats["newest_card_ts"]))
                oldest = time.strftime("%Y-%m-%d %H:%M", time.localtime(stats["oldest_card_ts"]))
                print(f"   时间范围: {oldest} → {newest}")
        if stats.get("top_tags"):
            print(f"\n🏷️  高频标签:")
            for tag, count in stats["top_tags"]:
                print(f"   {tag}: {count}")
        if stats.get("top_error_signatures"):
            print(f"\n⚠️  高频错误签名:")
            for sig, count in stats["top_error_signatures"]:
                print(f"   {sig}: {count}")
        if stats.get("hot_cards"):
            print(f"\n🔥 热门卡片:")
            for c in stats["hot_cards"]:
                print(f"   {c['card_id']} (views: {c['access_count']}): {c['symptoms']}")

    elif args.command == "rebuild-index":
        index = _rebuild_index()
        print(f"✅ 索引已重建: {index['total']} 张卡片")

    elif args.command == "hook":
        errors_list = [e.strip() for e in args.errors.split(",") if e.strip()] if args.errors else []
        fixes_list = [f.strip() for f in args.fixes.split(",") if f.strip()] if args.fixes else []
        lessons_list = [l.strip() for l in args.lessons.split(",") if l.strip()] if args.lessons else []
        card_id = on_subagent_stop(
            task_goal=args.goal,
            task_outcome=args.outcome,
            task_score=args.score,
            errors=errors_list,
            fixes=fixes_list,
            lessons=lessons_list,
            task_id=args.task_id,
        )
        if card_id:
            print(f"✅ 经验卡片已创建: {card_id}")
        else:
            print(f"⏭️  评分 {args.score}/10 < 7，跳过卡片创建")

    else:
        parser.print_help()


if __name__ == "__main__":
    import sys
    main()