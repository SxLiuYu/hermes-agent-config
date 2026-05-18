#!/usr/bin/env python3
"""
Post-Compaction 身份注入 — 每次上下文压缩后自动重新注入核心身份

对标 Claude Code PostCompaction Hook:
  当对话历史被压缩后，agent 的核心身份、规则和偏好可能丢失。
  这个 hook 在工具调用时检测压缩信号，确保身份信息始终存在。

触发条件: PostToolUse 事件，检测到 write_file/patch 工具调用后
（作为压缩信号代理 — 因为 Hermes 只有在使用工具时才会触发 hook）
"""

import os
import time
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
IDENTITY_FILE = HERMES_HOME / "config" / "identity-core.md"
STATE_FILE = HERMES_HOME / ".identity_last_inject"

# 最小注入间隔（秒），避免频繁注入
MIN_INTERVAL = 300  # 5 分钟


def get_identity() -> str:
    """读取核心身份文档"""
    if IDENTITY_FILE.exists():
        return IDENTITY_FILE.read_text().strip()
    return ""


def should_inject() -> bool:
    """是否应该现在注入（距离上次注入超过 MIN_INTERVAL）"""
    if not STATE_FILE.exists():
        return True
    try:
        last = float(STATE_FILE.read_text().strip())
        return (time.time() - last) > MIN_INTERVAL
    except Exception:
        return True


def mark_injected():
    """记录注入时间"""
    STATE_FILE.write_text(str(time.time()))


if __name__ == "__main__":
    if not should_inject():
        exit(0)

    identity = get_identity()
    if not identity:
        exit(0)

    # 输出身份内容 — Hermes hook 机制会捕获 stdout
    print(f"\n[Identity re-injected after context activity]\n{identity}")

    mark_injected()