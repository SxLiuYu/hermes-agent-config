#!/bin/bash
# PreToolUse: 前置元认知评估 — 每次工具调用前执行策略选择
set -euo pipefail
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)

# 只在关键工具前评估
case "$TOOL" in
    delegate_task|terminal|browser_navigate|web_search|session_search|memory)
        ARGS=$(echo "$INPUT" | python3 -c "import sys,json;a=json.load(sys.stdin).get('args',{});print(a.get('goal',a.get('query',a.get('command','')[:200])))" 2>/dev/null)
        python3 "$HOME/.hermes/tools/metacognition.py" assess --task "$ARGS" 2>/dev/null || true
        ;;
    *)
        ;;
esac
exit 0