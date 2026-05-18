#!/bin/bash
# kg-extract: Auto-extract entity relations from memory writes
set -euo pipefail
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)
case "$TOOL" in memory_add|memory_replace) ;; *) exit 0 ;; esac
CONTENT=$(echo "$INPUT" | python3 -c "import sys,json;d=json.load(sys.stdin);a=d.get('args',{});print((a.get('content','')or a.get('value','')or'')[:500])" 2>/dev/null || true)
[ -z "$CONTENT" ] && exit 0
python3 "$HOME/.hermes/tools/knowledge_graph.py" auto-extract "$CONTENT" 2>/dev/null
exit 0