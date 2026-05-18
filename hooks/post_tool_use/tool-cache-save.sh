#!/bin/bash
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)
PARAMS=$(echo "$INPUT" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin).get('args',{}),sort_keys=True))" 2>/dev/null || true)
RESULT=$(echo "$INPUT" | python3 -c "import sys,json;r=json.load(sys.stdin).get('result',{});print(json.dumps(r)[:2000])" 2>/dev/null || true)
if [ -n "$TOOL" ] && [ -n "$PARAMS" ]; then
    python3 ~/.hermes/tools/tool_cache.py set --tool "$TOOL" --params "$PARAMS" --result "$RESULT" 2>/dev/null || true
fi
exit 0