#!/usr/bin/env python3
"""
Chapter Compressor — 对话章节压缩 (对标 MemGPT)

核心思想：自动识别话题边界，一个话题结束后自动摘要压缩。防止长对话中上下文膨胀。

对标论文:
  - MemGPT (2024): OS-style 虚拟上下文管理，自动将旧上下文换出到外部存储
  - Letta (2025): MemGPT 的进化版，对话章节化管理

设计:
  1. 话题边界检测规则（基于消息内容）：
     - 用户说"好的"/"了解了"/"谢谢"/"OK" → 话题结束
     - 用户切换到新话题（关键词不重叠 <30%）→ 旧话题结束
     - AI 说"总结一下"/"完成"/"✅" → 话题可能结束
  2. 压缩策略：
     - 提取关键决策和输出（忽略中间错误/调试过程）
     - 每个章节摘要 ≤500 tokens
     - 格式：## 📚 第N章 [话题名称] (时间) → 摘要
  3. 状态存储：~/.hermes/chapters/state.json + ~/.hermes/chapters/summaries.jsonl

命令行用法:
  python3 chapter_compressor.py detect --messages '[{"role":"user","content":"..."},...]'
  python3 chapter_compressor.py compress --chapter-id 3
  python3 chapter_compressor.py inject
  python3 chapter_compressor.py stats
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
CHAPTERS_DIR = HERMES_HOME / "chapters"
STATE_FILE = CHAPTERS_DIR / "state.json"
SUMMARIES_FILE = CHAPTERS_DIR / "summaries.jsonl"
os.makedirs(CHAPTERS_DIR, exist_ok=True)

# ─── 常量 ─────────────────────────────────────────────────

MAX_SUMMARY_TOKENS = 500
TOPIC_OVERLAP_THRESHOLD = 0.30          # < 30% 关键词重叠 → 新话题
CHAPTER_IDLE_MINUTES = 5                # 连续无消息超过此阈值 → 话题可能结束

# 话题结束触发词
END_SIGNALS_USER = {
    "好的", "了解了", "明白", "谢谢", "OK", "ok", "好的好的",
    "没问题", "清楚了", "收到", "懂了", "got it", "thanks",
    "感谢", "多谢", "好", "行", "可以", "嗯嗯", "嗯",
}
END_SIGNALS_AI = {
    "总结一下", "总结", "完成", "✅", "搞定", "好了",
    "以上", "就是这样", "还有什么问题", "还有其他需要",
}

# 章节编号用词
CHAPTER_NUMBER_WORDS = [
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
]


# ─── 工具函数 ─────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _simple_tokenize(text: str) -> list[str]:
    """简单分词：提取中英文关键词（≥2 个字符）"""
    # 中文：连续中文字符
    cn_tokens = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    # 英文：连续的字母/数字（≥2 个字符）
    en_tokens = re.findall(r"[a-zA-Z0-9]{2,}", text.lower())
    return cn_tokens + en_tokens


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中文 ~1.5 字/token，英文 ~4 字/token"""
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    en_chars = len(text) - cn_chars
    return int(cn_chars / 1.5 + en_chars / 4)


def _keyword_overlap(tokens_a: list[str], tokens_b: list[str]) -> float:
    """计算两组关键词的 Jaccard 相似度"""
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _extract_topic_name(messages: list[dict]) -> str:
    """从消息中提取话题名称（取第一条用户消息的前 30 字）"""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            # 清理常见前缀
            content = re.sub(r"^(请|帮我|帮忙|能不能|能否)\s*", "", content)
            if len(content) > 30:
                content = content[:30] + "..."
            return content if content else "未命名话题"
    return "未命名话题"


def _extract_key_outputs(messages: list[dict]) -> list[str]:
    """
    从对话中提取关键决策和输出，过滤中间错误和调试过程。
    策略：
      - 保留 role=assistant 的最后几条消息（认为是最关键的输出）
      - 跳过包含错误/调试关键字的消息
      - 跳过过短的消息
    """
    error_keywords = [
        "error", "Error", "错误", "失败", "traceback", "Traceback",
        "exception", "Exception", "调试", "debug", "try again",
        "重试", "出错了", "报错", "❌", "⚠️",
    ]

    outputs = []
    # 取 assistant 消息，跳过错误类
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", "").strip()
            if len(content) < 15:
                continue
            if any(kw in content for kw in error_keywords):
                continue
            outputs.append(content)

    # 保留最后 3 条关键输出
    return outputs[-3:] if len(outputs) > 3 else outputs


def _build_summary(chapter_id: int, topic_name: str, key_outputs: list[str],
                   timestamp: str) -> str:
    """构建章节摘要（≤500 tokens）"""
    chapter_word = CHAPTER_NUMBER_WORDS[min(chapter_id - 1, len(CHAPTER_NUMBER_WORDS) - 1)]
    header = f"## 📚 第{chapter_word}章 [{topic_name}] ({timestamp})"

    # 合并关键输出并截断
    body_parts = []
    tokens_so_far = _estimate_tokens(header)
    remaining = MAX_SUMMARY_TOKENS - tokens_so_far - 20

    for output in key_outputs:
        snippet = output[:500]  # 每条最多 500 字符
        est = _estimate_tokens(snippet)
        if tokens_so_far + est > remaining:
            snippet = snippet[:int(remaining * 4)] + "..."
            body_parts.append(snippet)
            break
        body_parts.append(snippet)
        tokens_so_far += est

    body = "\n\n".join(body_parts)
    summary = f"{header}\n{body}" if body else header
    return summary


# ─── 状态管理 ─────────────────────────────────────────────

def load_state() -> dict:
    """加载章节状态"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "current_chapter_id": 0,
        "current_topic_tokens": [],
        "current_messages_count": 0,
        "last_message_time": None,
        "completed_chapters": [],
    }


def save_state(state: dict):
    """保存章节状态"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_summaries() -> list[dict]:
    """加载已完成的章节摘要"""
    if SUMMARIES_FILE.exists():
        summaries = []
        try:
            for line in SUMMARIES_FILE.read_text().strip().split("\n"):
                if line.strip():
                    summaries.append(json.loads(line))
        except Exception:
            pass
        return summaries
    return []


def append_summary(summary: dict):
    """追加章节摘要到 summaries.jsonl"""
    SUMMARIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUMMARIES_FILE, "a") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")


# ─── 话题检测 ─────────────────────────────────────────────

def detect_topic_boundary(messages: list[dict], state: dict) -> dict:
    """
    检测当前话题是否已结束，返回检测结果。

    返回:
      {
        "ended": bool,
        "reason": str,
        "confidence": float,   # 0.0 ~ 1.0
        "new_topic_tokens": list[str],
        "details": str
      }
    """
    if not messages:
        return {
            "ended": False,
            "reason": "no_messages",
            "confidence": 0.0,
            "new_topic_tokens": [],
            "details": "没有消息输入"
        }

    # 解析消息
    last_msg = messages[-1] if messages else {}
    last_role = last_msg.get("role", "")
    last_content = last_msg.get("content", "").strip()

    # ── 规则 1: 用户结束信号 ──
    if last_role == "user":
        clean = last_content.strip().rstrip("。！？,.!?")
        if clean in END_SIGNALS_USER or any(
            sig in clean for sig in ["没问题", "清楚了", "got it"]
        ):
            return {
                "ended": True,
                "reason": "user_end_signal",
                "confidence": 0.95,
                "new_topic_tokens": [],
                "details": f"用户发送结束信号: '{clean}'"
            }

    # ── 规则 2: AI 结束信号 ──
    if last_role == "assistant":
        for sig in END_SIGNALS_AI:
            if sig in last_content:
                return {
                    "ended": True,
                    "reason": "ai_end_signal",
                    "confidence": 0.75,
                    "new_topic_tokens": [],
                    "details": f"AI 输出包含结束信号: '{sig}'"
                }

    # ── 规则 3: 话题切换检测（关键词重叠 < 30%） ──
    current_tokens = state.get("current_topic_tokens", [])
    if current_tokens and len(messages) >= 2:
        # 比较最近一条用户消息与当前话题的关键词重叠
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if user_msgs:
            newest_user_tokens = _simple_tokenize(user_msgs[-1].get("content", ""))
            overlap = _keyword_overlap(current_tokens, newest_user_tokens)
            if overlap < TOPIC_OVERLAP_THRESHOLD and len(current_tokens) > 3:
                return {
                    "ended": True,
                    "reason": "topic_switch",
                    "confidence": 1.0 - overlap,
                    "new_topic_tokens": newest_user_tokens,
                    "details": f"话题切换: 关键词重叠率 {overlap:.1%} < {TOPIC_OVERLAP_THRESHOLD:.0%}"
                }

    # ── 规则 4: 空闲超时 ──
    last_time_str = state.get("last_message_time")
    if last_time_str:
        try:
            last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
            idle_minutes = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
            if idle_minutes > CHAPTER_IDLE_MINUTES:
                return {
                    "ended": True,
                    "reason": "idle_timeout",
                    "confidence": 0.60,
                    "new_topic_tokens": [],
                    "details": f"空闲 {idle_minutes:.0f} 分钟 > {CHAPTER_IDLE_MINUTES} 分钟阈值"
                }
        except Exception:
            pass

    # ── 默认：话题未结束 ──
    return {
        "ended": False,
        "reason": "ongoing",
        "confidence": 0.0,
        "new_topic_tokens": [],
        "details": "话题仍在进行中"
    }


# ─── 章节压缩 ─────────────────────────────────────────────

def compress_chapter(chapter_id: int, messages: list[dict]) -> dict:
    """
    压缩一个完整的话题章节为摘要。

    参数:
      chapter_id: 章节编号
      messages: 该章节的所有消息

    返回:
      {
        "chapter_id": int,
        "topic_name": str,
        "summary": str,
        "timestamp": str,
        "message_count": int,
        "estimated_tokens": int
      }
    """
    topic_name = _extract_topic_name(messages)
    key_outputs = _extract_key_outputs(messages)
    timestamp = _now_iso()
    summary = _build_summary(chapter_id, topic_name, key_outputs, timestamp)

    result = {
        "chapter_id": chapter_id,
        "topic_name": topic_name,
        "summary": summary,
        "timestamp": timestamp,
        "message_count": len(messages),
        "estimated_summary_tokens": _estimate_tokens(summary),
    }

    # 持久化
    append_summary(result)

    return result


# ─── 注入 ─────────────────────────────────────────────────

def inject_summaries(max_chapters: int = 10) -> dict:
    """
    返回压缩后的章节摘要注入块，可直接插入 system prompt 中。

    返回:
      {
        "inject_text": str,
        "chapter_count": int,
        "total_summary_tokens": int
      }
    """
    summaries = load_summaries()
    # 取最近 N 章
    recent = summaries[-max_chapters:] if len(summaries) > max_chapters else summaries

    if not recent:
        return {
            "inject_text": "",
            "chapter_count": 0,
            "total_summary_tokens": 0,
        }

    blocks = ["## 📖 历史章节摘要\n"]
    total_tokens = 0
    for s in recent:
        blocks.append(s.get("summary", ""))
        total_tokens += s.get("estimated_summary_tokens", _estimate_tokens(s.get("summary", "")))

    inject_text = "\n\n".join(blocks)
    return {
        "inject_text": inject_text,
        "chapter_count": len(recent),
        "total_summary_tokens": total_tokens,
    }


# ─── 统计 ─────────────────────────────────────────────────

def get_stats() -> dict:
    """获取章节统计"""
    state = load_state()
    summaries = load_summaries()

    total_compressed_messages = sum(s.get("message_count", 0) for s in summaries)
    total_summary_tokens = sum(
        s.get("estimated_summary_tokens", _estimate_tokens(s.get("summary", "")))
        for s in summaries
    )

    return {
        "current_chapter_id": state.get("current_chapter_id", 0),
        "completed_chapters": len(summaries),
        "total_compressed_messages": total_compressed_messages,
        "total_summary_tokens": total_summary_tokens,
        "avg_summary_tokens": round(total_summary_tokens / len(summaries), 1) if summaries else 0,
        "last_updated": _now_iso(),
    }


# ─── JSON 输入解析 ────────────────────────────────────────

def _parse_messages(raw: str) -> list[dict]:
    """解析消息 JSON 字符串或从 stdin 读取"""
    # 尝试作为 JSON 解析
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "messages" in data:
            return data["messages"]
    except json.JSONDecodeError:
        pass

    # 尝试从文件读取
    if os.path.isfile(raw):
        try:
            data = json.loads(Path(raw).read_text())
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "messages" in data:
                return data["messages"]
        except Exception:
            pass

    # 尝试从 stdin 读取（如果是管道输入）
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read())
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "messages" in data:
                return data["messages"]
        except Exception:
            pass

    return []


# ─── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Chapter Compressor — 对话章节压缩 (对标 MemGPT)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 检测当前话题是否结束
  python3 chapter_compressor.py detect --messages '[{"role":"user","content":"好的谢谢"}]'

  # 压缩最近完成的话题章节（需要先有 messages 输入）
  python3 chapter_compressor.py compress --chapter-id 1 --messages '[{"role":"user","content":"..."}]'

  # 返回注入块
  python3 chapter_compressor.py inject

  # 章节统计
  python3 chapter_compressor.py stats
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # detect
    p_detect = subparsers.add_parser("detect", help="检测话题边界")
    p_detect.add_argument("--messages", type=str, default="",
                          help="消息 JSON 数组 (或文件路径, 或 stdin)")

    # compress
    p_compress = subparsers.add_parser("compress", help="压缩章节")
    p_compress.add_argument("--chapter-id", type=int, default=1, help="章节编号")
    p_compress.add_argument("--messages", type=str, default="",
                            help="消息 JSON 数组 (或文件路径, 或 stdin)")

    # inject
    p_inject = subparsers.add_parser("inject", help="返回摘要注入块")
    p_inject.add_argument("--max-chapters", type=int, default=10, help="最多返回几章")

    # stats
    subparsers.add_parser("stats", help="章节统计")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "detect":
            messages = _parse_messages(args.messages)
            state = load_state()
            result = detect_topic_boundary(messages, state)

            # 更新状态
            if messages:
                user_msgs = [m for m in messages if m.get("role") == "user"]
                if user_msgs:
                    state["current_topic_tokens"] = _simple_tokenize(
                        user_msgs[-1].get("content", "")
                    )
                state["last_message_time"] = _now_iso()
                state["current_messages_count"] += len(messages)
            save_state(state)

            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "compress":
            messages = _parse_messages(args.messages)
            if not messages:
                print(json.dumps({"error": "没有提供消息"}, ensure_ascii=False))
                sys.exit(1)

            result = compress_chapter(args.chapter_id, messages)

            # 更新状态
            state = load_state()
            state["completed_chapters"].append(args.chapter_id)
            state["current_chapter_id"] = args.chapter_id
            state["current_topic_tokens"] = []
            state["current_messages_count"] = 0
            save_state(state)

            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "inject":
            result = inject_summaries(args.max_chapters)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "stats":
            result = get_stats()
            print(json.dumps(result, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()