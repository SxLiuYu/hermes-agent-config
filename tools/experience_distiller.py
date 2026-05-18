#!/usr/bin/env python3
"""
Experience Distiller — EvolveR 自蒸馏经验提炼
==============================================
从 Agent 的执行轨迹中自动提炼"认知 Skill"（思维方式），
而非工具 Skill。对标 EvolveR (ICML 2026) 的启发式经验蒸馏。

CLI 用法:
  python3 experience_distiller.py distill          — 从最近的轨迹中蒸馏经验
  python3 experience_distiller.py search --context "..." — 检索相关经验
  python3 experience_distiller.py inject --context "..."  — 注入当前任务相关经验
  python3 experience_distiller.py stats             — 经验库统计
  python3 experience_distiller.py prune             — 剪枝低分经验
  python3 experience_distiller.py merge             — 合并语义重复的经验
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── 路径常量 ───────────────────────────────────────────────────
HERMES_HOME = Path.home() / ".hermes"
DATA_DIR = HERMES_HOME / "experience_distiller"
EXPERIENCES_FILE = DATA_DIR / "experiences.json"
TRACE_INDEX_FILE = DATA_DIR / "trace_index.json"
JOURNAL_DIR = HERMES_HOME / "journal"
ERROR_CORRECTION_FILE = HERMES_HOME / "error_correction.json"
SESSION_MEMORY_FILE = HERMES_HOME / "session_memory.md"
TASK_JOURNAL_DIR = JOURNAL_DIR / "tasks"

# ─── 七条 EvolveR 经验提炼规则 ──────────────────────────────────
DISTILL_RULES = [
    {
        "id": "pattern_1",
        "principle": "当搜索返回空时，不要重复同样的关键词，而是改写查询角度",
        "trigger": "search_failed_then_rewrite",
        "detect": lambda traces: _detect_pattern_search_rewrite(traces),
    },
    {
        "id": "pattern_2",
        "principle": "比较类问题先分别收集双方信息，再做对比分析",
        "trigger": "comparison_sequential_gather",
        "detect": lambda traces: _detect_pattern_comparison(traces),
    },
    {
        "id": "pattern_3",
        "principle": "文件写入前先验证路径可写，必要时先创建目录",
        "trigger": "write_failed_then_mkdir",
        "detect": lambda traces: _detect_pattern_write_mkdir(traces),
    },
    {
        "id": "pattern_4",
        "principle": "调试前先用 session_search 查找类似历史案例",
        "trigger": "debug_after_history_lookup",
        "detect": lambda traces: _detect_pattern_debug_history(traces),
    },
    {
        "id": "pattern_5",
        "principle": "长篇任务拆分为子任务并行执行以提升成功率",
        "trigger": "split_then_parallel_success",
        "detect": lambda traces: _detect_pattern_split_parallel(traces),
    },
    {
        "id": "pattern_6",
        "principle": "不确定时用 clarify 工具与用户确认需求，避免基于错误假设执行",
        "trigger": "assumption_corrected_by_clarify",
        "detect": lambda traces: _detect_pattern_clarify(traces),
    },
    {
        "id": "pattern_7",
        "principle": "复杂任务先 plan 再执行，有规划的任务成功率更高",
        "trigger": "plan_before_execute_success",
        "detect": lambda traces: _detect_pattern_plan(traces),
    },
]

# ─── 工具函数 ───────────────────────────────────────────────────


def ensure_dir() -> None:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Dict:
    """安全加载 JSON 文件，文件不存在则返回空字典。"""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_json(path: Path, data: Any) -> None:
    """安全保存 JSON 文件。"""
    ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def load_experiences() -> Dict[str, Dict]:
    """加载经验库。"""
    data = load_json(EXPERIENCES_FILE)
    if isinstance(data, list):
        # 兼容旧格式
        return {exp.get("id", f"exp_{i}"): exp for i, exp in enumerate(data)}
    return data


def save_experiences(experiences: Dict[str, Dict]) -> None:
    """保存经验库。"""
    save_json(EXPERIENCES_FILE, experiences)


def load_trace_index() -> Dict[str, List[str]]:
    """加载轨迹→经验索引。"""
    data = load_json(TRACE_INDEX_FILE)
    if not isinstance(data, dict):
        return {}
    return data


def save_trace_index(index: Dict[str, List[str]]) -> None:
    """保存轨迹→经验索引。"""
    save_json(TRACE_INDEX_FILE, index)


# ─── 轨迹数据读取 ───────────────────────────────────────────────


def read_task_journal_traces() -> List[Dict]:
    """从 task_journal 中读取任务轨迹。"""
    traces = []
    if not TASK_JOURNAL_DIR.exists():
        return traces

    for journal_file in sorted(TASK_JOURNAL_DIR.glob("*.json"), reverse=True):
        try:
            data = load_json(journal_file)
            if not data:
                continue
            # 支持单任务和任务列表两种格式
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                trace = {
                    "trace_id": entry.get("task_id", journal_file.stem),
                    "source": "task_journal",
                    "file": str(journal_file),
                    "status": entry.get("status", "unknown"),
                    "task_description": entry.get("task", entry.get("description", "")),
                    "actions": entry.get("actions", entry.get("steps", [])),
                    "errors": entry.get("errors", []),
                    "outcome": entry.get("outcome", ""),
                    "plan_used": entry.get("plan") is not None or "plan" in str(entry.get("actions", "")).lower(),
                    "timestamp": entry.get("timestamp", entry.get("completed_at", "")),
                    "duration": entry.get("duration", 0),
                }
                traces.append(trace)
        except (json.JSONDecodeError, IOError):
            continue
    return traces


def read_error_correction_traces() -> List[Dict]:
    """从 error_correction.json 读取错误模式。"""
    traces = []
    data = load_json(ERROR_CORRECTION_FILE)
    if not data:
        return traces

    entries = data if isinstance(data, list) else [data]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        trace = {
            "trace_id": entry.get("id", f"err_{hash(str(entry))}"),
            "source": "error_correction",
            "file": str(ERROR_CORRECTION_FILE),
            "status": "failure" if entry.get("error") else "success",
            "task_description": entry.get("task", entry.get("context", "")),
            "actions": entry.get("actions", []),
            "errors": [entry.get("error", "")] if entry.get("error") else [],
            "correction": entry.get("correction", entry.get("fix", "")),
            "pattern": entry.get("pattern", ""),
            "timestamp": entry.get("timestamp", ""),
        }
        traces.append(trace)
    return traces


def read_session_memory_traces() -> List[Dict]:
    """从 session_memory.md 读取完成摘要。"""
    traces = []
    if not SESSION_MEMORY_FILE.exists():
        return traces

    try:
        content = SESSION_MEMORY_FILE.read_text(encoding="utf-8")
    except IOError:
        return traces

    # 解析 Markdown 中的任务块
    # 匹配形如 "## 任务" 或 "### task_xxx" 的段落
    blocks = re.split(r"\n(?=#{2,3}\s)", content)
    for block in blocks:
        lines = block.strip().split("\n")
        if not lines:
            continue
        header = lines[0]
        body = "\n".join(lines[1:])

        status = "unknown"
        if re.search(r"(成功|完成|success|done)", body, re.IGNORECASE):
            status = "success"
        elif re.search(r"(失败|错误|failure|error|fail)", body, re.IGNORECASE):
            status = "failure"

        trace = {
            "trace_id": f"session_{hash(header) & 0xFFFFFFFF:08x}",
            "source": "session_memory",
            "file": str(SESSION_MEMORY_FILE),
            "status": status,
            "task_description": header.lstrip("#").strip(),
            "actions": _extract_actions_from_text(body),
            "errors": _extract_errors_from_text(body),
            "outcome": body[:500],
            "timestamp": _extract_timestamp_from_text(body),
        }
        traces.append(trace)
    return traces


def _extract_actions_from_text(text: str) -> List[str]:
    """从文本中提取动作列表。"""
    actions = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("-") or re.match(r"^\d+\.", line):
            actions.append(line.lstrip("- 0123456789.").strip())
    return actions


def _extract_errors_from_text(text: str) -> List[str]:
    """从文本中提取错误信息。"""
    errors = []
    for match in re.finditer(r"(?:错误|Error|error|失败|异常)[：:]\s*(.+?)(?:\n|$)", text):
        errors.append(match.group(1).strip())
    return errors


def _extract_timestamp_from_text(text: str) -> str:
    """从文本中提取时间戳。"""
    match = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})", text)
    return match.group(1) if match else ""


def collect_all_traces() -> List[Dict]:
    """收集所有来源的轨迹。"""
    traces = []
    traces.extend(read_task_journal_traces())
    traces.extend(read_error_correction_traces())
    traces.extend(read_session_memory_traces())
    return traces


# ─── 模式检测函数 ───────────────────────────────────────────────


def _detect_pattern_search_rewrite(traces: List[Dict]) -> Optional[List[str]]:
    """检测模式1：搜索失败后改写查询角度成功。"""
    trace_ids = []
    for trace in traces:
        actions = trace.get("actions", [])
        if isinstance(actions, list) and len(actions) >= 2:
            action_texts = [
                a if isinstance(a, str) else a.get("action", a.get("tool", ""))
                for a in actions
            ]
            action_str = " ".join(action_texts).lower()
            has_search_fail = any(
                kw in action_str for kw in ["search", "搜索", "find", "查找"]
            ) and trace.get("status") == "failure"
            # 查找同一任务中后续成功的版本
            if has_search_fail:
                for other in traces:
                    if other is trace:
                        continue
                    other_actions = [
                        a if isinstance(a, str) else a.get("action", a.get("tool", ""))
                        for a in other.get("actions", [])
                    ]
                    other_str = " ".join(other_actions).lower()
                    if (
                        other.get("status") == "success"
                        and any(kw in other_str for kw in ["search", "rewrite", "改写", "query"])
                        and _task_similarity(trace.get("task_description", ""), other.get("task_description", "")) > 0.3
                    ):
                        trace_ids.append(trace.get("trace_id", ""))
                        break
    return trace_ids if trace_ids else None


def _detect_pattern_comparison(traces: List[Dict]) -> Optional[List[str]]:
    """检测模式2：比较类问题先分别收集双方信息。"""
    trace_ids = []
    compare_keywords = ["比较", "对比", "compare", "vs", "versus", "区别", "差异", "哪个"]
    for trace in traces:
        desc = trace.get("task_description", "").lower()
        if any(kw in desc for kw in compare_keywords):
            actions = trace.get("actions", [])
            if isinstance(actions, list):
                action_texts = [
                    a if isinstance(a, str) else a.get("action", a.get("tool", ""))
                    for a in actions
                ]
                action_str = " ".join(action_texts).lower()
                # 检测是否包含至少两次搜索/收集操作
                search_count = sum(
                    1 for kw in ["search", "搜索", "gather", "收集", "fetch", "获取"]
                    if kw in action_str
                )
                if search_count >= 2 and trace.get("status") == "success":
                    trace_ids.append(trace.get("trace_id", ""))
    return trace_ids if trace_ids else None


def _detect_pattern_write_mkdir(traces: List[Dict]) -> Optional[List[str]]:
    """检测模式3：文件写入失败后先创建目录再写成功。"""
    trace_ids = []
    for trace in traces:
        errors = trace.get("errors", [])
        error_text = " ".join(e if isinstance(e, str) else str(e) for e in errors).lower()
        if any(kw in error_text for kw in ["no such file", "directory", "mkdir", "不存在", "路径", "enoent"]):
            correction = trace.get("correction", "").lower()
            if any(kw in correction for kw in ["mkdir", "创建目录", "ensure", "makedirs"]):
                trace_ids.append(trace.get("trace_id", ""))
    return trace_ids if trace_ids else None


def _detect_pattern_debug_history(traces: List[Dict]) -> Optional[List[str]]:
    """检测模式4：调试前先查历史案例。"""
    trace_ids = []
    debug_keywords = ["debug", "调试", "bug", "fix", "修复", "错误", "排错"]
    for trace in traces:
        desc = trace.get("task_description", "").lower()
        if any(kw in desc for kw in debug_keywords):
            actions = trace.get("actions", [])
            if isinstance(actions, list):
                action_texts = [
                    a if isinstance(a, str) else a.get("action", a.get("tool", ""))
                    for a in actions
                ]
                action_str = " ".join(action_texts).lower()
                has_history_lookup = any(
                    kw in action_str
                    for kw in ["session_search", "history", "历史", "memory", "记忆", "recall"]
                )
                if has_history_lookup and trace.get("status") == "success":
                    trace_ids.append(trace.get("trace_id", ""))
    return trace_ids if trace_ids else None


def _detect_pattern_split_parallel(traces: List[Dict]) -> Optional[List[str]]:
    """检测模式5：拆分子任务并行执行后成功率提升。"""
    trace_ids = []
    for trace in traces:
        desc = trace.get("task_description", "").lower()
        if any(kw in desc for kw in ["拆分", "并行", "parallel", "subtask", "子任务", "分解"]):
            if trace.get("status") == "success":
                trace_ids.append(trace.get("trace_id", ""))
    return trace_ids if trace_ids else None


def _detect_pattern_clarify(traces: List[Dict]) -> Optional[List[str]]:
    """检测模式6：不确定时用 clarify 确认。"""
    trace_ids = []
    for trace in traces:
        actions = trace.get("actions", [])
        if isinstance(actions, list):
            action_texts = [
                a if isinstance(a, str) else a.get("action", a.get("tool", ""))
                for a in actions
            ]
            action_str = " ".join(action_texts).lower()
            has_clarify = any(
                kw in action_str for kw in ["clarify", "确认", "confirm", "ask_user", "提问"]
            )
            errors = trace.get("errors", [])
            error_text = " ".join(e if isinstance(e, str) else str(e) for e in errors).lower()
            had_assumption_error = any(
                kw in error_text for kw in ["assum", "假设", "误解", "misunderstand"]
            )
            if has_clarify or had_assumption_error:
                trace_ids.append(trace.get("trace_id", ""))
    return trace_ids if trace_ids else None


def _detect_pattern_plan(traces: List[Dict]) -> Optional[List[str]]:
    """检测模式7：复杂任务先 plan 再执行。"""
    trace_ids = []
    for trace in traces:
        plan_used = trace.get("plan_used", False)
        actions = trace.get("actions", [])
        if isinstance(actions, list):
            action_texts = [
                a if isinstance(a, str) else a.get("action", a.get("tool", ""))
                for a in actions
            ]
            action_str = " ".join(action_texts).lower()
            plan_used = plan_used or any(
                kw in action_str for kw in ["plan", "规划", "planning", "todo"]
            )
        # 复杂任务标志：长描述或多步骤
        desc = trace.get("task_description", "")
        is_complex = len(desc) > 100 or (
            isinstance(actions, list) and len(actions) >= 3
        )
        if plan_used and is_complex and trace.get("status") == "success":
            trace_ids.append(trace.get("trace_id", ""))
    return trace_ids if trace_ids else None


def _task_similarity(a: str, b: str) -> float:
    """计算两个任务描述的简单相似度（基于共同词）。"""
    if not a or not b:
        return 0.0
    words_a = set(re.findall(r"\w+", a.lower()))
    words_b = set(re.findall(r"\w+", b.lower()))
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    return len(intersection) / min(len(words_a), len(words_b))


# ─── 经验生成 ───────────────────────────────────────────────────


def generate_experience_id() -> str:
    """生成唯一经验ID。"""
    ts = int(time.time() * 1000)
    return f"exp_{ts:x}"


def extract_keywords(text: str) -> List[str]:
    """从文本中提取关键词（去停用词）。"""
    stopwords = {
        "的", "是", "在", "了", "和", "与", "或", "不", "要", "会", "可以",
        "the", "is", "in", "at", "to", "of", "and", "or", "a", "an",
        "这", "那", "我", "你", "他", "它", "们", "这个", "那个", "什么",
        "怎么", "如何", "一个", "如果", "因为", "所以", "但是", "然后",
        "进行", "使用", "需要", "通过", "已经", "没有", "可能", "应该",
    }
    words = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    keywords = []
    for w in words:
        if w not in stopwords and len(w) >= 2:
            keywords.append(w)
    # 去重保留顺序
    seen = set()
    result = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result[:20]


# ─── 评分系统 ───────────────────────────────────────────────────


def calculate_score(
    success_rate: float,
    usage_count: int,
    last_used: Optional[str],
    created_at: Optional[str] = None,
) -> float:
    """动态计算经验评分。

    score = success_rate * 0.6 + recency_factor * 0.2 + frequency_factor * 0.2
    """
    # 频率因子
    frequency_factor = min(1.0, usage_count / 20.0)

    # 时效因子
    recency_factor = 0.0
    if last_used:
        try:
            if isinstance(last_used, str):
                last_used_dt = datetime.fromisoformat(last_used.replace("Z", "+00:00").replace(" ", "T"))
            else:
                last_used_dt = last_used
            days_since = (datetime.now() - last_used_dt.replace(tzinfo=None)).days
            recency_factor = max(0.0, 1.0 - days_since / 30.0)
        except (ValueError, TypeError):
            recency_factor = 0.0

    # 综合评分
    score = success_rate * 0.6 + recency_factor * 0.2 + frequency_factor * 0.2
    return round(score, 4)


def update_experience_score(exp: Dict) -> Dict:
    """更新经验的评分字段。"""
    exp["score"] = calculate_score(
        success_rate=exp.get("success_rate", 0.5),
        usage_count=exp.get("usage_count", 0),
        last_used=exp.get("last_used"),
        created_at=exp.get("created_at"),
    )
    return exp


# ─── 蒸馏主逻辑 ──────────────────────────────────────────────────


def distill() -> Dict[str, Dict]:
    """
    从所有轨迹中蒸馏经验。
    返回更新后的经验库。
    """
    print("🔍 收集轨迹数据...")
    traces = collect_all_traces()
    if not traces:
        print("⚠️  没有找到任何轨迹数据。经验库保持不变。")
        return load_experiences()

    print(f"   收集到 {len(traces)} 条轨迹")
    success_traces = [t for t in traces if t.get("status") == "success"]
    failure_traces = [t for t in traces if t.get("status") == "failure"]
    print(f"   成功: {len(success_traces)} | 失败: {len(failure_traces)}")

    experiences = load_experiences()
    trace_index = load_trace_index()
    new_count = 0
    updated_count = 0

    print("\n🧠 应用 EvolveR 启发式规则...")
    for rule in DISTILL_RULES:
        trace_ids = rule["detect"](traces)
        if not trace_ids:
            print(f"   ✗ {rule['id']}: 未检测到匹配轨迹")
            continue

        # 检查是否已存在相同 principle 的经验
        existing_id = None
        for eid, exp in experiences.items():
            if exp.get("trigger") == rule["trigger"]:
                existing_id = eid
                break

        if existing_id:
            # 更新已有经验
            exp = experiences[existing_id]
            old_traces = set(exp.get("source_trace", []))
            new_traces = [tid for tid in trace_ids if tid not in old_traces and tid]
            if new_traces:
                exp["source_trace"] = list(old_traces) + new_traces
                for tid in new_traces:
                    trace_index.setdefault(tid, []).append(existing_id)
                # 更新统计
                total_count = len(success_traces) + len(failure_traces)
                exp_success = sum(1 for tid in exp["source_trace"] if any(
                    t.get("trace_id") == tid and t.get("status") == "success"
                    for t in traces
                ))
                exp["success_rate"] = round(exp_success / max(1, len(exp["source_trace"])), 4)
                exp["usage_count"] = len(exp["source_trace"])
                exp["success_count"] = exp_success
                exp["last_used"] = datetime.now().isoformat()
                exp = update_experience_score(exp)
                experiences[existing_id] = exp
                updated_count += 1
                print(f"   ↻ {rule['id']}: 更新 ({len(new_traces)} 条新轨迹)")
        else:
            # 创建新经验
            exp_id = generate_experience_id()
            exp_success = sum(1 for tid in trace_ids if any(
                t.get("trace_id") == tid and t.get("status") == "success"
                for t in traces
            ))
            now = datetime.now().isoformat()
            experience = {
                "id": exp_id,
                "principle": rule["principle"],
                "trigger": rule["trigger"],
                "source_trace": [tid for tid in trace_ids if tid],
                "success_rate": round(exp_success / max(1, len(trace_ids)), 4),
                "usage_count": len(trace_ids),
                "success_count": exp_success,
                "created_at": now,
                "last_used": now,
                "score": 0.0,
                "embedding_keywords": extract_keywords(rule["principle"]),
            }
            experience = update_experience_score(experience)
            experiences[exp_id] = experience
            for tid in trace_ids:
                if tid:
                    trace_index.setdefault(tid, []).append(exp_id)
            new_count += 1
            print(f"   ✓ {rule['id']}: 新建 (score={experience['score']:.3f})")

    save_experiences(experiences)
    save_trace_index(trace_index)

    total = len(experiences)
    print(f"\n✅ 蒸馏完成: 新建 {new_count} 条, 更新 {updated_count} 条, 经验库共 {total} 条")
    return experiences


# ─── 语义去重（基于关键词重叠） ──────────────────────────────────


def compute_keyword_overlap(kw1: List[str], kw2: List[str]) -> float:
    """计算两组关键词的重叠率。"""
    if not kw1 or not kw2:
        return 0.0
    set1 = set(kw1)
    set2 = set(kw2)
    intersection = set1 & set2
    return len(intersection) / min(len(set1), len(set2))


def find_duplicates(experiences: Dict[str, Dict], threshold: float = 0.6) -> List[Tuple[str, str, float]]:
    """查找语义重复的经验对。返回 (id1, id2, overlap)。"""
    pairs = []
    exp_list = list(experiences.items())
    for i in range(len(exp_list)):
        for j in range(i + 1, len(exp_list)):
            id1, exp1 = exp_list[i]
            id2, exp2 = exp_list[j]
            kw1 = exp1.get("embedding_keywords", [])
            kw2 = exp2.get("embedding_keywords", [])
            overlap = compute_keyword_overlap(kw1, kw2)
            if overlap > threshold:
                pairs.append((id1, id2, overlap))
    # 按重叠率降序排列
    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


# ─── 搜索 ────────────────────────────────────────────────────────


def search_experiences(
    context: str,
    experiences: Optional[Dict[str, Dict]] = None,
    top_k: int = 5,
) -> List[Dict]:
    """基于上下文检索相关经验。"""
    if experiences is None:
        experiences = load_experiences()

    if not experiences:
        return []

    context_keywords = set(extract_keywords(context))

    scored = []
    for exp_id, exp in experiences.items():
        exp_keywords = set(exp.get("embedding_keywords", []))
        if not exp_keywords:
            continue
        # 计算关键词重叠得分
        intersection = context_keywords & exp_keywords
        keyword_score = len(intersection) / max(1, len(exp_keywords))

        # 综合得分 = 关键词匹配 * 0.5 + 经验评分 * 0.5
        combined = keyword_score * 0.5 + exp.get("score", 0.5) * 0.5

        # 额外加成分：trigger 匹配
        trigger = exp.get("trigger", "")
        if any(kw in context.lower() for kw in trigger.split("_")):
            combined *= 1.2

        scored.append((exp, combined))

    scored.sort(key=lambda x: x[1], reverse=True)

    # 记录使用
    for exp, score in scored[:top_k]:
        if score > 0.1:
            exp["usage_count"] = exp.get("usage_count", 0) + 1
            exp["last_used"] = datetime.now().isoformat()
            exp = update_experience_score(exp)

    if scored[:top_k]:
        save_experiences(experiences)

    return [exp for exp, _ in scored[:top_k]]


# ─── 注入 ────────────────────────────────────────────────────────


def inject_experiences(context: str) -> str:
    """注入当前任务相关的经验，返回格式化的经验文本。"""
    related = search_experiences(context, top_k=3)

    if not related:
        return "（无相关经验）"

    lines = ["📚 **相关经验（EvolveR 经验库）**：\n"]
    for i, exp in enumerate(related, 1):
        principle = exp.get("principle", "")
        score = exp.get("score", 0)
        usage = exp.get("usage_count", 0)
        lines.append(f"{i}. {principle} _(评分: {score:.2f}, 使用: {usage}次)_")
        trigger = exp.get("trigger", "")
        lines.append(f"   - 触发条件: `{trigger}`")

    return "\n".join(lines)


# ─── 统计 ────────────────────────────────────────────────────────


def get_stats() -> Dict:
    """获取经验库统计信息。"""
    experiences = load_experiences()
    trace_index = load_trace_index()

    if not experiences:
        return {
            "total_experiences": 0,
            "total_traces_indexed": 0,
            "message": "经验库为空，请先运行 distill",
        }

    scores = [exp.get("score", 0) for exp in experiences.values()]
    triggers = Counter(exp.get("trigger", "unknown") for exp in experiences.values())
    total_usage = sum(exp.get("usage_count", 0) for exp in experiences.values())

    return {
        "total_experiences": len(experiences),
        "total_traces_indexed": len(trace_index),
        "total_usage_count": total_usage,
        "avg_score": round(sum(scores) / len(scores), 4) if scores else 0,
        "max_score": round(max(scores), 4) if scores else 0,
        "min_score": round(min(scores), 4) if scores else 0,
        "low_score_count": sum(1 for s in scores if s < 0.3),
        "trigger_distribution": dict(triggers.most_common()),
        "experiences": [
            {
                "id": exp.get("id"),
                "principle": exp.get("principle", "")[:80],
                "trigger": exp.get("trigger"),
                "score": exp.get("score", 0),
                "usage_count": exp.get("usage_count", 0),
            }
            for exp in sorted(
                experiences.values(), key=lambda x: x.get("score", 0), reverse=True
            )
        ],
    }


# ─── 剪枝 ────────────────────────────────────────────────────────


def prune(threshold: float = 0.3, dry_run: bool = False) -> Dict:
    """剪枝低分经验。"""
    experiences = load_experiences()
    trace_index = load_trace_index()

    to_prune = [
        eid for eid, exp in experiences.items()
        if exp.get("score", 0) < threshold
    ]

    if not to_prune:
        return {"pruned": 0, "message": "没有需要剪枝的经验", "dry_run": dry_run}

    if dry_run:
        return {
            "pruned": 0,
            "candidates": len(to_prune),
            "candidate_ids": to_prune,
            "details": [
                {
                    "id": eid,
                    "principle": experiences[eid].get("principle", "")[:80],
                    "score": experiences[eid].get("score", 0),
                }
                for eid in to_prune
            ],
            "dry_run": True,
            "message": f"发现 {len(to_prune)} 条待剪枝经验（使用 --execute 确认删除）",
        }

    removed_count = 0
    for eid in to_prune:
        exp = experiences.pop(eid, None)
        if exp:
            # 清理轨迹索引
            for tid in exp.get("source_trace", []):
                if tid in trace_index:
                    trace_index[tid] = [e for e in trace_index[tid] if e != eid]
                    if not trace_index[tid]:
                        del trace_index[tid]
            removed_count += 1

    save_experiences(experiences)
    save_trace_index(trace_index)

    return {
        "pruned": removed_count,
        "remaining": len(experiences),
        "dry_run": False,
        "message": f"已剪枝 {removed_count} 条经验，剩余 {len(experiences)} 条",
    }


# ─── 合并 ────────────────────────────────────────────────────────


def merge(threshold: float = 0.6, dry_run: bool = False) -> Dict:
    """合并语义重复的经验。"""
    experiences = load_experiences()
    trace_index = load_trace_index()

    pairs = find_duplicates(experiences, threshold)

    if not pairs:
        return {"merged": 0, "pairs_found": 0, "message": "没有发现需要合并的经验对", "dry_run": dry_run}

    if dry_run:
        return {
            "merged": 0,
            "pairs_found": len(pairs),
            "pairs": [
                {
                    "id_keep": p[0],
                    "id_remove": p[1],
                    "overlap": round(p[2], 4),
                    "principle_keep": experiences[p[0]].get("principle", "")[:80],
                    "principle_remove": experiences[p[1]].get("principle", "")[:80],
                }
                for p in pairs
            ],
            "dry_run": True,
            "message": f"发现 {len(pairs)} 对重复经验（使用 --execute 确认合并）",
        }

    merged = set()
    merged_count = 0
    for id_keep, id_remove, overlap in pairs:
        if id_keep in merged or id_remove in merged:
            continue
        exp_keep = experiences.get(id_keep)
        exp_remove = experiences.get(id_remove)
        if not exp_keep or not exp_remove:
            continue

        # 保留高分经验的表述，合并轨迹
        if exp_remove.get("score", 0) > exp_keep.get("score", 0):
            id_keep, id_remove = id_remove, id_keep
            exp_keep, exp_remove = exp_remove, exp_keep
            # 更新 experiences 中的引用
            experiences[id_keep] = exp_keep

        # 合并 source_trace
        keep_traces = set(exp_keep.get("source_trace", []))
        remove_traces = set(exp_remove.get("source_trace", []))
        exp_keep["source_trace"] = list(keep_traces | remove_traces)

        # 合并统计
        exp_keep["usage_count"] = exp_keep.get("usage_count", 0) + exp_remove.get("usage_count", 0)
        exp_keep["success_count"] = exp_keep.get("success_count", 0) + exp_remove.get("success_count", 0)
        total = max(1, exp_keep["usage_count"])
        exp_keep["success_rate"] = round(exp_keep["success_count"] / total, 4)

        # 合并关键词
        kw_keep = set(exp_keep.get("embedding_keywords", []))
        kw_remove = set(exp_remove.get("embedding_keywords", []))
        exp_keep["embedding_keywords"] = list(kw_keep | kw_remove)[:20]

        # 更新评分
        exp_keep = update_experience_score(exp_keep)
        experiences[id_keep] = exp_keep

        # 更新轨迹索引
        for tid in exp_remove.get("source_trace", []):
            if tid in trace_index:
                trace_index[tid] = [
                    id_keep if e == id_remove else e
                    for e in trace_index[tid]
                ]

        # 删除被合并的经验
        del experiences[id_remove]
        merged.add(id_keep)
        merged.add(id_remove)
        merged_count += 1

    save_experiences(experiences)
    save_trace_index(trace_index)

    return {
        "merged": merged_count,
        "remaining": len(experiences),
        "dry_run": False,
        "message": f"已合并 {merged_count} 对重复经验，剩余 {len(experiences)} 条",
    }


# ─── CLI ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="experience_distiller",
        description="EvolveR 自蒸馏经验提炼 — 从 Agent 轨迹中提炼认知 Skill",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s distill                    从轨迹中蒸馏经验
  %(prog)s search --context "调试错误"  检索相关经验
  %(prog)s inject --context "写文件"    注入当前任务相关经验
  %(prog)s stats                       查看经验库统计
  %(prog)s prune --dry-run             预览待剪枝经验
  %(prog)s prune --execute             执行剪枝
  %(prog)s merge --dry-run             预览重复经验对
  %(prog)s merge --execute             执行合并
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # distill
    subparsers.add_parser("distill", help="从最近的轨迹中蒸馏经验")

    # search
    search_parser = subparsers.add_parser("search", help="检索相关经验")
    search_parser.add_argument(
        "--context", "-c", required=True, type=str, help="搜索上下文（任务描述或关键词）"
    )
    search_parser.add_argument(
        "--top-k", "-k", type=int, default=5, help="返回结果数量 (默认: 5)"
    )

    # inject
    inject_parser = subparsers.add_parser("inject", help="注入当前任务相关的经验")
    inject_parser.add_argument(
        "--context", "-c", required=True, type=str, help="当前任务上下文"
    )

    # stats
    stats_parser = subparsers.add_parser("stats", help="经验库统计")
    stats_parser.add_argument(
        "--json", action="store_true", help="以 JSON 格式输出"
    )

    # prune
    prune_parser = subparsers.add_parser("prune", help="剪枝低分经验")
    prune_parser.add_argument(
        "--threshold", "-t", type=float, default=0.3, help="评分阈值 (默认: 0.3)"
    )
    prune_parser.add_argument(
        "--execute", action="store_true", help="实际执行剪枝（否则为 dry-run）"
    )

    # merge
    merge_parser = subparsers.add_parser("merge", help="合并语义重复的经验")
    merge_parser.add_argument(
        "--threshold", "-t", type=float, default=0.6, help="关键词重叠阈值 (默认: 0.6)"
    )
    merge_parser.add_argument(
        "--execute", action="store_true", help="实际执行合并（否则为 dry-run）"
    )

    return parser


def main() -> None:
    """主入口。"""
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    ensure_dir()

    if args.command == "distill":
        distill()

    elif args.command == "search":
        results = search_experiences(args.context, top_k=args.top_k)
        if not results:
            print(json.dumps({"results": [], "message": "未找到相关经验"}, ensure_ascii=False, indent=2))
        else:
            output = {
                "query": args.context,
                "count": len(results),
                "results": [
                    {
                        "id": exp.get("id"),
                        "principle": exp.get("principle"),
                        "trigger": exp.get("trigger"),
                        "score": exp.get("score"),
                        "usage_count": exp.get("usage_count"),
                        "success_rate": exp.get("success_rate"),
                        "keywords": exp.get("embedding_keywords"),
                    }
                    for exp in results
                ],
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))

    elif args.command == "inject":
        prompt = inject_experiences(args.context)
        print(json.dumps({"context": args.context, "injection": prompt}, ensure_ascii=False, indent=2))

    elif args.command == "stats":
        stats = get_stats()
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            _print_stats_pretty(stats)

    elif args.command == "prune":
        result = prune(threshold=args.threshold, dry_run=not args.execute)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "merge":
        result = merge(threshold=args.threshold, dry_run=not args.execute)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _print_stats_pretty(stats: Dict) -> None:
    """格式化打印统计信息。"""
    print("\n📊 经验库统计")
    print("=" * 50)
    print(f"  经验总数:     {stats.get('total_experiences', 0)}")
    print(f"  已索引轨迹:   {stats.get('total_traces_indexed', 0)}")
    print(f"  总使用次数:   {stats.get('total_usage_count', 0)}")
    print(f"  平均评分:     {stats.get('avg_score', 0):.4f}")
    print(f"  最高评分:     {stats.get('max_score', 0):.4f}")
    print(f"  最低评分:     {stats.get('min_score', 0):.4f}")
    print(f"  低分经验 (<0.3): {stats.get('low_score_count', 0)}")
    print(f"\n📈 触发器分布:")
    for trigger, count in stats.get("trigger_distribution", {}).items():
        print(f"  - {trigger}: {count}")
    print(f"\n🏆 Top 经验:")
    for exp in stats.get("experiences", [])[:5]:
        print(f"  [{exp['score']:.3f}] {exp['principle'][:70]}...")
    print()


if __name__ == "__main__":
    main()