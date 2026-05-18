#!/usr/bin/env python3
"""
Experience Evolution — 统一进化闭环
结合 EvolveR + JiuwenSwarm 的进化闭环

核心思想：统一管理所有经验来源，实现"越用越强"的正循环。

CLI 用法：
  python3 experience_evolution.py evolve — 运行一轮进化（合并+去重+评分+剪枝+升级）
  python3 experience_evolution.py merge-all — 跨模块合并语义重复的经验
  python3 experience_evolution.py score-all — 重新评分所有经验
  python3 experience_evolution.py promote --exp-id "..." — 将经验升级为永久 Skill
  python3 experience_evolution.py stats — 全局进化统计
  python3 experience_evolution.py inject — 注入当前最活跃的经验

数据流：
  所有经验来源 → 统一读取 → 语义去重 → 统一评分 → 自动升降级 → Skill / Archive

存储路径：
  ~/.hermes/experience_evolution/state.json       — 进化状态
  ~/.hermes/experience_evolution/merge_log.jsonl  — 合并日志
  ~/.hermes/experience_evolution/archive/         — 降级存档
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
#  配置 & 路径
# ═══════════════════════════════════════════════════════════════════════════════

HERMES_HOME = Path.home() / ".hermes"
EVO_DIR = HERMES_HOME / "experience_evolution"
STATE_FILE = EVO_DIR / "state.json"
MERGE_LOG = EVO_DIR / "merge_log.jsonl"
ARCHIVE_DIR = EVO_DIR / "archive"
SKILLS_DIR = HERMES_HOME / "skills"

# 经验来源路径
EXPERIENCE_DISTILLER_FILE = HERMES_HOME / "experience_distiller" / "experiences.json"
ERROR_CORRECTION_FILE = HERMES_HOME / "error_correction" / "patterns.json"
SKILL_DISTILLER_REGISTRY = HERMES_HOME / "skills" / "_distilled" / "registry.json"
FEW_SHOTS_FILE = HERMES_HOME / "few_shots" / "examples.jsonl"
CHAPTER_COMPRESSOR_FILE = HERMES_HOME / "chapter_compressor" / "summaries.jsonl"
EXPERIENCE_CARDS_INDEX = HERMES_HOME / "experience_cards" / "cards_index.json"

# 阈值
SIMILARITY_MERGE_THRESHOLD = 0.7   # Jaccard 相似度阈值，超过则合并
PROMOTE_SUCCESS_COUNT = 10         # 连续成功次数阈值，触发升级
PROMOTE_SCORE_THRESHOLD = 0.8      # 评分阈值，触发升级
DEMOTE_FAILURE_COUNT = 5           # 连续失败次数阈值，触发降级
DEMOTE_SCORE_THRESHOLD = 0.2       # 评分阈值，触发降级
DECAY_DAYS = 30                    # 未使用天数，触发衰减
DECAY_FACTOR = 0.5                 # 衰减因子
TOP_KEYWORDS = 20                  # 关键词提取数量

# 评分权重
SCORE_FRESHNESS_WEIGHT = 0.15      # 新鲜度权重
SCORE_USAGE_WEIGHT = 0.25          # 使用频率权重
SCORE_SUCCESS_WEIGHT = 0.35        # 成功率权重
SCORE_QUALITY_WEIGHT = 0.25        # 来源质量权重

# 来源质量基准分
SOURCE_QUALITY = {
    "experience_cards": 0.90,       # 经验卡片：结构化、有验证
    "experience_distiller": 0.85,   # 认知经验蒸馏
    "error_correction": 0.75,       # 错误模式
    "skill_distiller": 0.80,        # 提炼的技能
    "few_shots": 0.70,              # 成功案例
    "chapter_compressor": 0.60,     # 章节摘要
}

# 英文停用词
STOP_WORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same",
    "so", "than", "too", "very", "and", "but", "or", "if", "while",
    "that", "this", "it", "its", "i", "me", "my", "we", "our", "you",
    "your", "he", "she", "they", "them", "their", "what", "which", "who",
    "whom", "just", "up", "down", "also", "about", "any", "these", "those",
    "using", "use", "used", "need", "like", "get", "got", "make", "made",
    "want", "try", "trying", "tried", "one", "two", "see", "know",
    "file", "files", "code", "function", "error", "errors", "task",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def ensure_dirs() -> None:
    """确保所有存储目录存在。"""
    EVO_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> dict | list:
    """安全加载 JSON 文件，缺失或损坏时返回 {} 或 []。"""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_jsonl(path: Path) -> list[dict]:
    """安全加载 JSONL 文件。"""
    entries: list[dict] = []
    if not path.exists():
        return entries
    try:
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if line:
                entries.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    return entries


def save_json(data: dict | list, path: Path) -> None:
    """原子写入 JSON 文件。"""
    ensure_dirs()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(entry: dict, path: Path) -> None:
    """追加一行 JSON 到 JSONL 文件。"""
    ensure_dirs()
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def exp_hash(text: str) -> str:
    """对经验文本生成确定性短哈希，用于去重。"""
    norm = re.sub(r"\s+", " ", str(text).strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def extract_keywords(text: str, top_n: int = TOP_KEYWORDS) -> list[str]:
    """提取关键词：去停用词后的高频词列表"""
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", str(text).lower())
    filtered = [t for t in tokens if t not in STOP_WORDS]
    counter = Counter(filtered)
    return [kw for kw, _ in counter.most_common(top_n)]


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Jaccard 相似度：|A∩B| / |A∪B|"""
    if not set_a or not set_b:
        return 0.0
    inter = set_a & set_b
    union = set_a | set_b
    return len(inter) / len(union) if union else 0.0


def days_since(iso_ts: Optional[str]) -> float:
    """计算距离某个 ISO 时间戳的天数。"""
    if not iso_ts:
        return 999.0
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 999.0


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════════════════
#  经验加载 — 统一读取所有来源
# ═══════════════════════════════════════════════════════════════════════════════

def load_experiences_from_distiller() -> list[dict]:
    """从 experience_distiller/experiences.json 加载认知经验。"""
    data = load_json(EXPERIENCE_DISTILLER_FILE)
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = list(data.values())
    else:
        return []

    results = []
    for e in entries:
        if isinstance(e, dict):
            results.append({
                "exp_id": e.get("id", exp_hash(json.dumps(e))),
                "source": "experience_distiller",
                "content": e.get("lesson", e.get("content", json.dumps(e, ensure_ascii=False))),
                "keywords": e.get("keywords", extract_keywords(str(e.get("lesson", e)))),
                "score": e.get("score", 0.5),
                "success_count": e.get("success_count", 0),
                "failure_count": e.get("failure_count", 0),
                "usage_count": e.get("usage_count", 0),
                "last_used": e.get("last_used", e.get("timestamp", "")),
                "created_at": e.get("timestamp", today_iso()),
                "tags": e.get("tags", []),
                "raw": e,
            })
    return results


def load_experiences_from_error_correction() -> list[dict]:
    """从 error_correction/patterns.json 提取错误模式。"""
    patterns = load_json(ERROR_CORRECTION_FILE)
    if not isinstance(patterns, dict):
        return []

    results = []
    for tid, entry in patterns.items():
        if not isinstance(entry, dict):
            continue

        errors = entry.get("errors", [])
        for err in errors:
            corrections = err.get("corrections", [])
            error_text = err.get("error_text", "")
            success_count = sum(c.get("success_count", 0) for c in corrections)
            failure_count = err.get("failure_count", 0)
            total_uses = success_count + failure_count
            success_rate = success_count / total_uses if total_uses > 0 else 0.5

            # 为每个 correction 创建独立经验
            for corr in corrections:
                approach = corr.get("approach", "")
                content = f"任务 '{entry.get('task', '')}' 中，错误 '{error_text}' 通过 '{approach}' 解决。"
                results.append({
                    "exp_id": f"ec_{tid}_{exp_hash(error_text + approach)[:8]}",
                    "source": "error_correction",
                    "content": content,
                    "keywords": entry.get("keywords", []) + extract_keywords(error_text + " " + approach),
                    "score": success_rate,
                    "success_count": corr.get("success_count", 0),
                    "failure_count": failure_count,
                    "usage_count": total_uses,
                    "last_used": corr.get("last_success", entry.get("last_updated", "")),
                    "created_at": entry.get("first_seen", today_iso()),
                    "tags": ["error-correction"],
                    "raw": {"task_hash": tid, "error": error_text, "correction": approach},
                })

            # 仅有错误无成功方案的也记录
            if not corrections and error_text:
                content = f"任务 '{entry.get('task', '')}' 中遇到错误 '{error_text}'，尝试了 '{err.get('attempted_approach', '')}' 但尚未解决。"
                results.append({
                    "exp_id": f"ec_{tid}_{exp_hash(error_text)[:8]}_warn",
                    "source": "error_correction",
                    "content": content,
                    "keywords": entry.get("keywords", []) + extract_keywords(error_text),
                    "score": 0.1,  # 仅警告，低分
                    "success_count": 0,
                    "failure_count": failure_count,
                    "usage_count": failure_count,
                    "last_used": err.get("last_failure", entry.get("last_updated", "")),
                    "created_at": entry.get("first_seen", today_iso()),
                    "tags": ["error-correction", "unresolved"],
                    "raw": {"task_hash": tid, "error": error_text},
                })

    return results


def load_experiences_from_skill_distiller() -> list[dict]:
    """从 skill_distiller/_distilled/registry.json 加载蒸馏技能。"""
    reg = load_json(SKILL_DISTILLER_REGISTRY)
    if not isinstance(reg, dict):
        return []

    results = []
    skills_map = reg.get("skills", {})
    for task_type, info in skills_map.items():
        if not isinstance(info, dict):
            continue
        versions = info.get("versions", {})
        for ver, ver_info in versions.items():
            if not isinstance(ver_info, dict):
                continue
            name = ver_info.get("name", task_type)
            status = ver_info.get("status", "active")
            source_count = ver_info.get("source_count", 0)
            distilled_at = ver_info.get("distilled_at", "")

            # 尝试读取 SKILL.md
            skill_dir = HERMES_HOME / "skills" / "_distilled" / name
            content = ""
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                content = skill_md.read_text(encoding="utf-8")[:3000]

            score = 0.7 if status == "active" else 0.3
            results.append({
                "exp_id": f"sk_{name}",
                "source": "skill_distiller",
                "content": content or f"蒸馏技能: {name} (v{ver}, {source_count} sources)",
                "keywords": extract_keywords(content or name),
                "score": score,
                "success_count": source_count,
                "failure_count": 0,
                "usage_count": source_count,
                "last_used": distilled_at,
                "created_at": distilled_at,
                "tags": ["skill", task_type, status],
                "raw": ver_info,
            })

    return results


def load_experiences_from_few_shots() -> list[dict]:
    """从 few_shots/examples.jsonl 加载成功案例。"""
    entries = load_jsonl(FEW_SHOTS_FILE)
    results = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        content = e.get("example", e.get("content", json.dumps(e, ensure_ascii=False)))
        results.append({
            "exp_id": f"fs_{exp_hash(content)[:12]}_{i}",
            "source": "few_shots",
            "content": str(content)[:2000],
            "keywords": e.get("keywords", extract_keywords(str(content))),
            "score": e.get("score", 0.65),
            "success_count": 1,
            "failure_count": 0,
            "usage_count": e.get("usage_count", 0),
            "last_used": e.get("last_used", ""),
            "created_at": e.get("timestamp", today_iso()),
            "tags": ["few-shot", e.get("task_type", "general")],
            "raw": e,
        })
    return results


def load_experiences_from_chapter_compressor() -> list[dict]:
    """从 chapter_compressor/summaries.jsonl 加载章节摘要。"""
    entries = load_jsonl(CHAPTER_COMPRESSOR_FILE)
    results = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        summary = e.get("summary", e.get("content", json.dumps(e, ensure_ascii=False)))
        results.append({
            "exp_id": f"cc_{exp_hash(summary)[:12]}_{i}",
            "source": "chapter_compressor",
            "content": str(summary)[:2000],
            "keywords": e.get("keywords", extract_keywords(str(summary))),
            "score": e.get("score", 0.55),
            "success_count": 0,
            "failure_count": 0,
            "usage_count": e.get("usage_count", 0),
            "last_used": e.get("timestamp", ""),
            "created_at": e.get("timestamp", today_iso()),
            "tags": ["summary", e.get("chapter", "general")],
            "raw": e,
        })
    return results


def load_experiences_from_cards() -> list[dict]:
    """从 experience_cards/cards_index.json 加载经验卡片。"""
    data = load_json(EXPERIENCE_CARDS_INDEX)
    if not isinstance(data, dict):
        return []

    results = []
    cards = data.get("cards", data)
    if isinstance(cards, dict):
        cards = list(cards.values())
    if not isinstance(cards, list):
        return []

    for c in cards:
        if not isinstance(c, dict):
            continue
        index_layer = c.get("index_layer", {})
        resolution_layer = c.get("resolution_layer", {})
        symptoms = index_layer.get("symptoms", "")
        fix_strategy = resolution_layer.get("fix_strategy", "")
        content = f"{symptoms} → {fix_strategy}" if symptoms else json.dumps(c, ensure_ascii=False)

        results.append({
            "exp_id": c.get("card_id", exp_hash(content)[:12]),
            "source": "experience_cards",
            "content": content[:2000],
            "keywords": index_layer.get("tags", []) + extract_keywords(content),
            "score": c.get("source_score", 0.7),
            "success_count": c.get("success_count", 0),
            "failure_count": 0,
            "usage_count": c.get("access_count", 0),
            "last_used": datetime.fromtimestamp(c.get("updated_at", 0), tz=timezone.utc).isoformat() if c.get("updated_at") else "",
            "created_at": datetime.fromtimestamp(c.get("created_at", time.time()), tz=timezone.utc).isoformat(),
            "tags": index_layer.get("tags", []),
            "raw": c,
        })
    return results


def load_all_experiences() -> list[dict]:
    """从所有来源加载经验，统一返回标准化列表。"""
    all_exp: list[dict] = []

    for loader in [
        load_experiences_from_distiller,
        load_experiences_from_error_correction,
        load_experiences_from_skill_distiller,
        load_experiences_from_few_shots,
        load_experiences_from_chapter_compressor,
        load_experiences_from_cards,
    ]:
        try:
            exps = loader()
            all_exp.extend(exps)
        except Exception as exc:
            print(f"⚠️  加载 {loader.__name__} 失败: {exc}", file=sys.stderr)

    return all_exp


# ═══════════════════════════════════════════════════════════════════════════════
#  跨模块语义去重
# ═══════════════════════════════════════════════════════════════════════════════

def merge_duplicates(experiences: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    跨模块语义去重：基于关键词 Jaccard 相似度合并重复经验。

    Returns:
        (deduped_experiences, merge_logs)
    """
    n = len(experiences)
    if n <= 1:
        return experiences, []

    # 预计算关键词集合
    kw_sets: list[set[str]] = []
    for exp in experiences:
        kws = exp.get("keywords", [])
        kw_sets.append(set(kws) if kws else set())

    # 合并队列
    merged_flags = [False] * n
    deduped: list[dict] = []
    merge_logs: list[dict] = []

    for i in range(n):
        if merged_flags[i]:
            continue

        best = experiences[i]
        best_score = best.get("score", 0)
        merged_ids = []

        for j in range(i + 1, n):
            if merged_flags[j]:
                continue
            sim = jaccard_similarity(kw_sets[i], kw_sets[j])
            if sim >= SIMILARITY_MERGE_THRESHOLD:
                other = experiences[j]
                other_score = other.get("score", 0)
                merged_ids.append(other.get("exp_id", f"exp_{j}"))

                # 保留评分更高的
                if other_score > best_score:
                    # 合并 usage / success 等计数
                    best_usage = best.get("usage_count", 0) + other.get("usage_count", 0)
                    best_success = best.get("success_count", 0) + other.get("success_count", 0)
                    best_failure = best.get("failure_count", 0) + other.get("failure_count", 0)
                    # 合并关键词和标签
                    merged_kws = list(set(best.get("keywords", []) + other.get("keywords", [])))[:TOP_KEYWORDS * 2]
                    merged_tags = list(set(best.get("tags", []) + other.get("tags", [])))

                    best = {
                        **other,
                        "exp_id": best.get("exp_id", other["exp_id"]),  # 保留原 ID
                        "usage_count": best_usage,
                        "success_count": best_success,
                        "failure_count": best_failure,
                        "keywords": merged_kws,
                        "tags": merged_tags,
                        "merged_from": best.get("merged_from", []) + [other.get("exp_id")] + (best.get("merged_from", []) if best != experiences[i] else []),
                    }
                    best_score = other_score
                else:
                    # 将 other 的计数汇入 best
                    best["usage_count"] = best.get("usage_count", 0) + other.get("usage_count", 0)
                    best["success_count"] = best.get("success_count", 0) + other.get("success_count", 0)
                    best["failure_count"] = best.get("failure_count", 0) + other.get("failure_count", 0)
                    best["keywords"] = list(set(best.get("keywords", []) + other.get("keywords", [])))[:TOP_KEYWORDS * 2]
                    best["tags"] = list(set(best.get("tags", []) + other.get("tags", [])))
                    best["merged_from"] = best.get("merged_from", []) + [other.get("exp_id")]

                merged_flags[j] = True

        if merged_ids:
            best.setdefault("merged_from", [])
            merge_log = {
                "action": "merge",
                "kept_id": best.get("exp_id"),
                "kept_score": best_score,
                "merged_ids": merged_ids,
                "similarity_threshold": SIMILARITY_MERGE_THRESHOLD,
            }
            merge_logs.append(merge_log)
            append_jsonl(merge_log, MERGE_LOG)

        deduped.append(best)
        merged_flags[i] = True

    return deduped, merge_logs


# ═══════════════════════════════════════════════════════════════════════════════
#  统一评分
# ═══════════════════════════════════════════════════════════════════════════════

def score_experiences(experiences: list[dict]) -> list[dict]:
    """
    统一评分所有经验，应用同一公式：
    score = freshness_weight * freshness + usage_weight * usage_norm
            + success_weight * success_rate + quality_weight * source_quality
    """
    now = datetime.now(timezone.utc)

    # 计算全局统计用于归一化
    max_usage = max((e.get("usage_count", 0) for e in experiences), default=1)

    for exp in experiences:
        # 新鲜度：近期使用（30天内的得高分）
        last_used = exp.get("last_used", "")
        freshness = 1.0
        try:
            if last_used:
                ts = datetime.fromisoformat(last_used.replace("Z", "+00:00"))
                age_days = (now - ts).total_seconds() / 86400.0
                freshness = max(0.0, 1.0 - (age_days / DECAY_DAYS))
        except (ValueError, TypeError):
            freshness = 0.3

        # 使用频率归一化
        usage_norm = min(1.0, exp.get("usage_count", 0) / max(max_usage, 1))

        # 成功率
        total = exp.get("success_count", 0) + exp.get("failure_count", 0)
        success_rate = exp.get("success_count", 0) / total if total > 0 else 0.5

        # 来源质量
        source_quality = SOURCE_QUALITY.get(exp.get("source", ""), 0.5)

        # 综合评分
        score = (
            SCORE_FRESHNESS_WEIGHT * freshness
            + SCORE_USAGE_WEIGHT * usage_norm
            + SCORE_SUCCESS_WEIGHT * success_rate
            + SCORE_QUALITY_WEIGHT * source_quality
        )

        # 30 天未使用衰减
        if days_since(exp.get("last_used")) > DECAY_DAYS:
            score *= DECAY_FACTOR

        exp["score"] = round(score, 4)
        exp["_score_detail"] = {
            "freshness": round(freshness, 3),
            "usage_norm": round(usage_norm, 3),
            "success_rate": round(success_rate, 3),
            "source_quality": round(source_quality, 3),
            "raw_score": round(score, 4),
        }

    # 排序
    experiences.sort(key=lambda e: e.get("score", 0), reverse=True)
    return experiences


# ═══════════════════════════════════════════════════════════════════════════════
#  自动升级 / 降级
# ═══════════════════════════════════════════════════════════════════════════════

def auto_promote_demote(experiences: list[dict], stats: dict) -> list[dict]:
    """
    自动升级/降级：
    - 连续 10 次成功 + score > 0.8 → promote 为永久 Skill
    - 连续 5 次失败 + score < 0.2 → demote 到 archive
    """
    promoted: list[dict] = []
    demoted: list[dict] = []

    for exp in experiences:
        exp_id = exp.get("exp_id", "unknown")
        score = exp.get("score", 0)
        success_count = exp.get("success_count", 0)
        failure_count = exp.get("failure_count", 0)

        # 升级条件
        if success_count >= PROMOTE_SUCCESS_COUNT and score >= PROMOTE_SCORE_THRESHOLD:
            # 生成 Skill
            skill_name = f"evolved-{re.sub(r'[^a-z0-9-]', '-', exp_id.lower())[:40]}"
            skill_content = _build_skill_markdown(exp, skill_name)
            skill_dir = SKILLS_DIR / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
            (skill_dir / "meta.json").write_text(json.dumps({
                "name": skill_name,
                "source": exp.get("source"),
                "score": score,
                "success_count": success_count,
                "promoted_at": datetime.now(timezone.utc).isoformat(),
                "exp_id": exp_id,
            }, indent=2, ensure_ascii=False), encoding="utf-8")

            exp["promoted"] = True
            exp["skill_name"] = skill_name
            promoted.append(exp)

        # 降级条件
        elif failure_count >= DEMOTE_FAILURE_COUNT and score < DEMOTE_SCORE_THRESHOLD:
            archive_path = ARCHIVE_DIR / f"{exp_id}.json"
            archive_path.write_text(json.dumps(exp, ensure_ascii=False, indent=2), encoding="utf-8")
            exp["archived"] = True
            exp["archive_path"] = str(archive_path)
            demoted.append(exp)

    # 移除已归档的经验
    active = [e for e in experiences if not e.get("archived")]

    stats["promoted_count"] = len(promoted)
    stats["demoted_count"] = len(demoted)
    stats["promoted_ids"] = [e["exp_id"] for e in promoted]
    stats["demoted_ids"] = [e["exp_id"] for e in demoted]

    return active


def _build_skill_markdown(exp: dict, skill_name: str) -> str:
    """从经验生成 SKILL.md 内容。"""
    content = exp.get("content", "")
    tags = exp.get("tags", [])
    source = exp.get("source", "unknown")
    score = exp.get("score", 0)

    return f"""---
name: {skill_name}
description: 自动进化升级 — 来源: {source} | 评分: {score:.2%}
tags: {tags}
version: 1
source: {source}
evolved_at: {datetime.now(timezone.utc).isoformat()}
exp_id: {exp.get("exp_id", "")}
---

# Skill: {skill_name}

## 触发条件
- 评分 ≥ {PROMOTE_SCORE_THRESHOLD}
- 连续成功 ≥ {PROMOTE_SUCCESS_COUNT} 次

## 经验内容
{content[:3000]}

## 来源
- 原始来源: {source}
- 标签: {', '.join(tags)}

## 使用反馈
- 成功次数: {exp.get('success_count', 0)}
- 失败次数: {exp.get('failure_count', 0)}
- 总使用: {exp.get('usage_count', 0)}
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  全局进化统计
# ═══════════════════════════════════════════════════════════════════════════════

def generate_stats(experiences: list[dict]) -> dict:
    """生成进化周期统计报告。"""
    n = len(experiences)
    active = sum(1 for e in experiences if not e.get("archived"))
    merged = sum(1 for e in experiences if e.get("merged_from"))
    promoted = sum(1 for e in experiences if e.get("promoted"))

    # 评分分布直方图
    score_bins = {
        "0.0-0.3": 0,
        "0.3-0.6": 0,
        "0.6-0.8": 0,
        "0.8-1.0": 0,
    }
    for e in experiences:
        s = e.get("score", 0)
        if s < 0.3:
            score_bins["0.0-0.3"] += 1
        elif s < 0.6:
            score_bins["0.3-0.6"] += 1
        elif s < 0.8:
            score_bins["0.6-0.8"] += 1
        else:
            score_bins["0.8-1.0"] += 1

    # 来源分布
    source_dist = Counter(e.get("source", "unknown") for e in experiences)

    # 标签统计
    all_tags: Counter[str] = Counter()
    for e in experiences:
        for t in e.get("tags", []):
            all_tags[t] += 1

    return {
        "total_experiences": n,
        "active_experiences": active,
        "merged_experiences": merged,
        "promoted_to_skills": promoted,
        "archived_experiences": sum(1 for e in experiences if e.get("archived")),
        "source_distribution": dict(source_dist),
        "score_distribution": score_bins,
        "top_tags": all_tags.most_common(20),
        "avg_score": round(sum(e.get("score", 0) for e in experiences) / max(n, 1), 4),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def format_stats_report(stats: dict) -> str:
    """格式化统计报告为可读文本。"""
    lines = [
        "╔══════════════════════════════════════════╗",
        "║     🔄 经验进化全局统计报告              ║",
        "╚══════════════════════════════════════════╝",
        "",
        "📊 核心指标：",
        f"   总经验数:     {stats['total_experiences']}",
        f"   活跃经验:     {stats['active_experiences']}",
        f"   已合并:       {stats['merged_experiences']}",
        f"   已升级为Skill: {stats['promoted_to_skills']}",
        f"   已归档:       {stats['archived_experiences']}",
        f"   平均评分:     {stats['avg_score']:.4f}",
        "",
        "📂 来源分布：",
    ]
    for src, cnt in sorted(stats["source_distribution"].items(), key=lambda x: x[1], reverse=True):
        bar = "█" * min(cnt, 40)
        lines.append(f"   {src:<25} {cnt:>4} {bar}")

    lines.extend([
        "",
        "📈 评分分布直方图：",
    ])
    for bin_name, cnt in stats["score_distribution"].items():
        bar = "█" * min(cnt, 50)
        pct = f"({cnt / max(stats['total_experiences'], 1) * 100:.0f}%)"
        lines.append(f"   {bin_name:<10} {cnt:>4} {pct:<6} {bar}")

    if stats.get("top_tags"):
        lines.extend([
            "",
            "🏷️  高频标签 Top 10：",
        ])
        for tag, cnt in stats["top_tags"][:10]:
            lines.append(f"   {tag:<20} {cnt}")

    lines.extend([
        "",
        f"生成时间: {stats['generated_at']}",
    ])
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI 命令
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_evolve(args: argparse.Namespace) -> None:
    """运行一轮完整进化：合并 + 去重 + 评分 + 剪枝 + 升级。"""
    print("🔬 开始进化周期...", file=sys.stderr)

    # 1. 加载所有经验
    experiences = load_all_experiences()
    print(f"   📥 加载 {len(experiences)} 条经验", file=sys.stderr)

    # 2. 语义去重合并
    experiences, merge_logs = merge_duplicates(experiences)
    print(f"   🔀 合并后剩余 {len(experiences)} 条 (去重 {len(merge_logs)} 对)", file=sys.stderr)

    # 3. 统一评分
    experiences = score_experiences(experiences)
    print(f"   📊 评分完成", file=sys.stderr)

    # 4. 自动升级/降级
    stats = {
        "total_before": len(experiences),
        "merge_count": len(merge_logs),
        "promoted_count": 0,
        "demoted_count": 0,
    }
    experiences = auto_promote_demote(experiences, stats)
    print(f"   ⬆️  升级 {stats['promoted_count']} 个 → Skill | ⬇️  降级 {stats['demoted_count']} 个 → Archive", file=sys.stderr)

    # 5. 保存状态
    full_stats = generate_stats(experiences)
    full_stats.update(stats)
    full_stats["evolution_round"] = full_stats.get("evolution_round", 0) + 1

    save_json({
        "round": full_stats["evolution_round"],
        "stats": full_stats,
        "experiences": experiences,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, STATE_FILE)

    # 输出结果
    print(json.dumps(full_stats, ensure_ascii=False, indent=2))


def cmd_merge_all(args: argparse.Namespace) -> None:
    """跨模块合并语义重复的经验。"""
    experiences = load_all_experiences()
    print(f"加载 {len(experiences)} 条经验", file=sys.stderr)

    experiences, merge_logs = merge_duplicates(experiences)
    print(f"合并后: {len(experiences)} 条, 合并对: {len(merge_logs)}", file=sys.stderr)

    result = {
        "before": len(experiences) + len(merge_logs) * 1,  # 近似
        "after": len(experiences),
        "merge_pairs": len(merge_logs),
        "merge_details": merge_logs,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_score_all(args: argparse.Namespace) -> None:
    """重新评分所有经验。"""
    experiences = load_all_experiences()
    print(f"加载 {len(experiences)} 条经验", file=sys.stderr)

    experiences = score_experiences(experiences)

    # 输出排序后的经验评分
    output = []
    for e in experiences[:50]:  # 默认 Top 50
        output.append({
            "exp_id": e.get("exp_id"),
            "source": e.get("source"),
            "score": e.get("score"),
            "content_preview": e.get("content", "")[:100],
        })

    print(json.dumps({
        "total_scored": len(experiences),
        "top_experiences": output,
    }, ensure_ascii=False, indent=2))


def cmd_promote(args: argparse.Namespace) -> None:
    """将指定经验升级为永久 Skill。"""
    exp_id = args.exp_id
    if not exp_id:
        print("ERROR: --exp-id is required", file=sys.stderr)
        sys.exit(1)

    experiences = load_all_experiences()
    target = None
    for e in experiences:
        if e.get("exp_id") == exp_id:
            target = e
            break

    if not target:
        print(json.dumps({"error": f"经验 {exp_id} 未找到", "total_loaded": len(experiences)}, ensure_ascii=False))
        sys.exit(1)

    # 强制升级
    skill_name = f"promoted-{re.sub(r'[^a-z0-9-]', '-', exp_id.lower())[:40]}"
    skill_content = _build_skill_markdown(target, skill_name)
    skill_dir = SKILLS_DIR / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
    (skill_dir / "meta.json").write_text(json.dumps({
        "name": skill_name,
        "source": target.get("source"),
        "score": target.get("score", 0),
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "exp_id": exp_id,
        "manual": True,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    result = {
        "status": "promoted",
        "exp_id": exp_id,
        "skill_name": skill_name,
        "skill_path": str(skill_dir),
        "score": target.get("score", 0),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_stats(args: argparse.Namespace) -> None:
    """全局进化统计。"""
    # 尝试加载已保存的状态
    state = load_json(STATE_FILE)
    if isinstance(state, dict) and state.get("experiences"):
        experiences = state["experiences"]  # type: ignore[reportArgumentType]
        stats = generate_stats(experiences)
        stats["from_cache"] = True
        stats["last_evolution_round"] = state.get("round", 0)
    else:
        experiences = load_all_experiences()
        stats = generate_stats(experiences)
        stats["from_cache"] = False

    if args.format == "text":
        print(format_stats_report(stats))
    else:
        print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_inject(args: argparse.Namespace) -> None:
    """注入当前最活跃的经验（生成提示文本）。"""
    # 加载状态或实时计算
    state = load_json(STATE_FILE)
    if state and state.get("experiences"):
        experiences = state["experiences"]
    else:
        experiences = load_all_experiences()
        experiences = score_experiences(experiences)

    # 取 top N 且 score > 阈值
    top_n = args.top or 5
    min_score = args.min_score or 0.6

    candidates = [
        e for e in experiences
        if e.get("score", 0) >= min_score and not e.get("archived")
    ]

    if not candidates:
        print(json.dumps({"injections": [], "message": "没有符合条件的活跃经验"}, ensure_ascii=False))
        return

    top = candidates[:top_n]

    lines = ["## 🔄 经验进化提示 (Top 活跃经验)", ""]
    seen_content = set()

    for i, e in enumerate(top, 1):
        content = e.get("content", "")
        content_hash = hashlib.md5(content.encode()).hexdigest()
        if content_hash in seen_content:
            continue
        seen_content.add(content_hash)

        preview = content[:200] + ("..." if len(content) > 200 else "")
        score = e.get("score", 0)
        source = e.get("source", "unknown")
        tags = ", ".join(e.get("tags", [])[:5])

        lines.append(f"### {i}. [{source}] (评分: {score:.2f})")
        if tags:
            lines.append(f"标签: {tags}")
        lines.append(f"{preview}")
        lines.append("")

    injection_text = "\n".join(lines)

    if args.format == "text":
        print(injection_text)
    else:
        print(json.dumps({
            "injections": [
                {
                    "exp_id": e.get("exp_id"),
                    "source": e.get("source"),
                    "score": e.get("score"),
                    "content_preview": e.get("content", "")[:200],
                    "tags": e.get("tags", []),
                }
                for e in top
            ],
            "injection_text": injection_text,
        }, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Experience Evolution — 统一进化闭环。合并 EvolveR + JiuwenSwarm，实现经验越用越强。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 experience_evolution.py evolve       # 运行一轮进化
  python3 experience_evolution.py merge-all    # 跨模块合并语义重复
  python3 experience_evolution.py score-all    # 重新评分
  python3 experience_evolution.py promote --exp-id "..." # 升级为 Skill
  python3 experience_evolution.py stats --format text    # 统计报告
  python3 experience_evolution.py inject --top 5         # 注入活跃经验
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # evolve — 完整进化周期
    sub.add_parser("evolve", help="运行一轮完整进化（合并+去重+评分+剪枝+升级）")

    # merge-all — 跨模块语义合并
    sub.add_parser("merge-all", help="跨模块合并语义重复的经验")

    # score-all — 统一评分
    sub.add_parser("score-all", help="重新评分所有经验")

    # promote — 手动升级
    p = sub.add_parser("promote", help="将经验升级为永久 Skill")
    p.add_argument("--exp-id", required=True, help="经验 ID")

    # stats — 统计报告
    p = sub.add_parser("stats", help="全局进化统计")
    p.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")

    # inject — 注入活跃经验
    p = sub.add_parser("inject", help="注入当前最活跃的经验")
    p.add_argument("--top", type=int, default=5, help="返回 top N 条经验 (默认 5)")
    p.add_argument("--min-score", type=float, default=0.6, help="最低评分阈值 (默认 0.6)")
    p.add_argument("--format", choices=["json", "text"], default="json", help="输出格式")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "evolve":
        cmd_evolve(args)
    elif args.command == "merge-all":
        cmd_merge_all(args)
    elif args.command == "score-all":
        cmd_score_all(args)
    elif args.command == "promote":
        cmd_promote(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "inject":
        cmd_inject(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()