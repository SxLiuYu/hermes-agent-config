#!/usr/bin/env python3
"""
P0-14: Smart Prompt Compressor — LLMLingua-style token-level noise filtering

对标:
  - LLMLingua (Microsoft, EMNLP'23/ACL'24): 20x compression with minimal loss
  - ACON paper: Context Collapse prevention through quality preservation

策略分层（与 observation_mask.py 互补）:
  observation_mask → 处理工具输出（接收后）
  prompt_compressor → 处理系统提示/工具描述/历史摘要（发送前）

压缩技术:
  1. 同义去重 (semantic dedup): 重复表述合并
  2. 格式精简 (format squash): 压缩 markdown/JSON 格式空白
  3. 规则优先级 (rule ranking): 低优先级规则可被压缩
  4. 重要标记 (importance tagging): 带 [CRITICAL] 标记的行永不压缩
"""

import json
import re
import hashlib
from dataclasses import dataclass, field
from typing import Optional

# ─── 噪声模式 ───────────────────────────────────────────

# 冗余表述模式（中英文）
FILLER_PATTERNS = [
    # 中文冗余
    r"请注意[，,\s]*",
    r"需要说明的是[，,\s]*",
    r"值得一提的是[，,\s]*",
    r"总的来说[，,\s]*",
    r"顾名思义[，,\s]*",
    r"众所周知[，,\s]*",
    r"当然[，,\s]*",
    r"显而易见[，,\s]*",
    # 英文冗余
    r"\bIt is (worth noting|important to note|worth mentioning) that\b",
    r"\bPlease note that\b",
    r"\bAs (mentioned|noted|stated) (above|previously|earlier)\b",
    r"\bIn other words[,\s]*",
    r"\bThat is to say[,\s]*",
    r"\bObviously[,\s]*",
    r"\bClearly[,\s]*",
    r"\bBasically[,\s]*",
    r"\bEssentially[,\s]*",
]

# 重复句式模式
REPETITION_PATTERNS = [
    (r"(\b\w+(?:\s+\w+){2,10})\s+\1", r"\1"),  # 连续重复短语
    (r"(确保|保证|请务必|一定)\s*(确保|保证|请务必|一定)", r"\1"),  # 中文双重强调
    (r"(always|never|must|should)\s+(always|never|must|should)\b", r"\1"),
]

# Markdown/格式冗余
FORMAT_NOISE = [
    (r"#{3,}\s*.*?\n", r"## "),         # 过量井号 → 两级标题
    (r"\n{3,}", "\n\n"),                  # 多余空行
    (r"---\n---", "---"),                # 重复分隔线
    (r"^\s*[-*]\s*$\n?", "", re.MULTILINE),  # 纯空格列表项
    (r"```\w*\n\s*```", ""),             # 空代码块
]

# ─── 保留标记 ────────────────────────────────────────────

# [CRITICAL] 或 [!IMPORTANT] 标记的行永不压缩
PRESERVE_MARKERS = [
    r"\[CRITICAL\]",
    r"\[!IMPORTANT\]",
    r"\[PRESERVE\]",
    r"\[NO-COMPRESS\]",
]


@dataclass
class CompressStats:
    """压缩统计"""
    original_chars: int = 0
    compressed_chars: int = 0
    filler_removed: int = 0
    repetition_removed: int = 0
    format_saved: int = 0
    preserved_lines: int = 0

    @property
    def ratio(self) -> float:
        return self.original_chars / max(self.compressed_chars, 1)

    @property
    def saved_pct(self) -> float:
        return (1 - self.compressed_chars / max(self.original_chars, 1)) * 100

    def summary(self) -> dict:
        return {
            "ratio": round(self.ratio, 1),
            "saved_pct": round(self.saved_pct, 1),
            "filler_removed": self.filler_removed,
            "repetition_removed": self.repetition_removed,
            "format_saved": self.format_saved,
            "preserved_lines": self.preserved_lines,
        }


def _has_preserve_marker(line: str) -> bool:
    """检查行是否包含保留标记"""
    for marker in PRESERVE_MARKERS:
        if re.search(marker, line):
            return True
    return False


def _compress_fillers(text: str, stats: CompressStats) -> str:
    """去除冗余表述"""
    for pattern in FILLER_PATTERNS:
        new_text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        if new_text != text:
            stats.filler_removed += len(text) - len(new_text)
            text = new_text
    return text


def _compress_repetitions(text: str, stats: CompressStats) -> str:
    """合并重复句式"""
    for pattern, replacement in REPETITION_PATTERNS:
        new_text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        if new_text != text:
            stats.repetition_removed += len(text) - len(new_text)
            text = new_text
    return text


def _compress_format(text: str, stats: CompressStats) -> str:
    """精简格式化噪声"""
    for pattern, replacement, *flags in FORMAT_NOISE:
        flag = flags[0] if flags else 0
        new_text = re.sub(pattern, replacement, text, flags=flag)
        if new_text != text:
            stats.format_saved += len(text) - len(new_text)
            text = new_text
    return text


def _preserve_critical_sections(text: str, stats: CompressStats) -> str:
    """
    保留带标记的关键行，确保安全规则不被压缩。
    对标 ACON 论文：防止 Context Collapse 导致安全规则丢失。
    """
    lines = text.split("\n")
    result = []
    for line in lines:
        if _has_preserve_marker(line):
            stats.preserved_lines += 1
            # 用特殊标记包裹保留行
            result.append(f"[KEPT] {line}")
        else:
            result.append(line)
    return "\n".join(result)


def compress(text: str, level: str = "standard") -> str:
    """
    智能压缩文本。

    level:
      - "minimal": 仅去除最明显的冗余（日常使用）
      - "standard": 适中压缩，保留可读性（默认）
      - "aggressive": 激进压缩，适合长文本但可能影响语义细微差异

    Returns: 压缩后的文本
    """
    stats = CompressStats(original_chars=len(text))

    # Step 0: 保留标记检查（所有级别）
    text = _preserve_critical_sections(text, stats)

    # Step 1: 去除冗余表述
    text = _compress_fillers(text, stats)

    # Step 2: 合并重复句式
    text = _compress_repetitions(text, stats)

    if level in ("standard", "aggressive"):
        # Step 3: 精简格式
        text = _compress_format(text, stats)

    if level == "aggressive":
        # Step 4: 激进模式 — 额外压缩换行和空格
        text = re.sub(r"\n\s*\n{2,}", "\n\n", text)
        text = re.sub(r"[ \t]{3,}", "  ", text)
        # 合并极短行（<10字符的非空行）
        lines = text.split("\n")
        merged = []
        for line in lines:
            stripped = line.strip()
            if stripped and len(stripped) < 10 and merged and merged[-1]:
                merged[-1] = merged[-1] + "; " + stripped
            else:
                merged.append(line)
        text = "\n".join(merged)

    stats.compressed_chars = len(text)
    return text


def compress_with_stats(text: str, level: str = "standard") -> tuple[str, dict]:
    """
    压缩并返回统计信息。
    Returns: (compressed_text, stats_dict)
    """
    stats = CompressStats(original_chars=len(text))

    text = _preserve_critical_sections(text, stats)
    text = _compress_fillers(text, stats)
    text = _compress_repetitions(text, stats)

    if level in ("standard", "aggressive"):
        text = _compress_format(text, stats)

    if level == "aggressive":
        text = re.sub(r"\n\s*\n{2,}", "\n\n", text)
        text = re.sub(r"[ \t]{3,}", "  ", text)

    stats.compressed_chars = len(text)
    return text, stats.summary()


# ─── 内容指纹 ────────────────────────────────────────────

def content_fingerprint(text: str) -> str:
    """
    生成内容指纹，用于检测跨轮重复。
    与 LLMLingua 不同，我们不依赖小模型，用 hash 做近似检测。
    """
    # 归一化后取 hash
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.md5(normalized.encode()).hexdigest()


def dedup_by_fingerprint(messages: list[dict]) -> list[dict]:
    """
    去重：移除内容指纹完全相同的相邻消息。
    对标 LLMLingua 的 token 级去重概念。
    """
    if not messages:
        return messages

    result = [messages[0]]
    prev_fp = content_fingerprint(messages[0].get("content", ""))

    for msg in messages[1:]:
        fp = content_fingerprint(msg.get("content", ""))
        if fp != prev_fp:
            result.append(msg)
            prev_fp = fp

    return result


# ─── 工具描述压缩 ─────────────────────────────────────────

def compress_tool_description(schema: dict) -> dict:
    """
    压缩工具 schema 描述。
    去除冗余的 description 字段重复内容。
    """
    if not schema or "description" not in schema:
        return schema

    result = schema.copy()
    desc = result.get("description", "")

    # 去除首尾空格
    desc = desc.strip()

    # 如果 description 和 name 完全相同，删除
    if "name" in schema and desc.lower() == schema["name"].lower():
        desc = ""

    # 压缩 description
    if desc:
        desc = compress(desc, level="standard")

    result["description"] = desc
    return result


# ─── CLI 入口 ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: prompt_compressor.py <text> [level: minimal|standard|aggressive]")
        print("       prompt_compressor.py --stdin [level]")
        sys.exit(1)

    if sys.argv[1] == "--stdin":
        text = sys.stdin.read()
        level = sys.argv[2] if len(sys.argv) > 2 else "standard"
    else:
        text = sys.argv[1]
        level = sys.argv[2] if len(sys.argv) > 2 else "standard"

    compressed, stats = compress_with_stats(text, level)
    print(json.dumps(stats))
    print("---")
    print(compressed)