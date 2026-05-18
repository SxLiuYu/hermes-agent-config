#!/bin/bash
# PreToolUse: 对关键工具调用前进行分类
set -euo pipefail
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)
case "$TOOL" in
    delegate_task|terminal|web_search)
        ARGS=$(echo "$INPUT" | python3 -c "import sys,json;a=json.load(sys.stdin).get('args',{});print(a.get('goal',a.get('query',a.get('command','')[:200])))" 2>/dev/null)
        python3 /Users/sxliuyu/.hermes/tools/multi_exit.py classify --query "$ARGS" 2>/dev/null || true
        ;;
esac
exit 0