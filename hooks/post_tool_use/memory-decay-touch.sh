#!/bin/bash
# =============================================================================
# memory-decay-touch.sh — PostToolUse: 每次 memory / session_search 使用时
# 自动更新 last_accessed，驱动记忆衰减
#
# 对标 Mem0 Memory Decay: 每次记忆被检索时自动 touch
# =============================================================================
set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)

# 只处理 memory 和 session_search 工具
case "$TOOL" in
    memory|session_search|session_recall|memory_search)
        ;;
    *)
        exit 0
        ;;
esac

# 提取查询参数作为 touch-query 的上下文
ARGS=$(echo "$INPUT" | python3 -c "import sys,json;a=json.load(sys.stdin).get('args',{});print(a.get('query',a.get('text',json.dumps(a)[:200])))" 2>/dev/null)

if [ -n "$ARGS" ]; then
    python3 "$HOME/.hermes/tools/memory_decay.py" touch-query "$ARGS" 2>/dev/null || true
fi

exit 0