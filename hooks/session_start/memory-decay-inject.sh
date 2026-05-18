#!/bin/bash
# =============================================================================
# memory-decay-inject.sh — SessionStart: recency 加权记忆注入
#
# 对标 Mem0 Memory Decay + Google 上下文工程:
#   每次新会话开始时，将 MEMORY.md 按 recency 重新排序后注入
#   热记忆优先，冷记忆靠后（但不删除）
# =============================================================================
set -euo pipefail

DECAY_TOOL="$HOME/.hermes/tools/memory_decay.py"

if [ ! -f "$DECAY_TOOL" ]; then
    exit 1  # 工具不存在，跳过
fi

echo "---"
echo "# 🧠 Memory Decay: Recency-Weighted Context"
python3 "$DECAY_TOOL" context 2>/dev/null

exit 0