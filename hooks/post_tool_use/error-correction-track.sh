#!/bin/bash
# PostToolUse: 追踪错误模式
set -euo pipefail
INPUT=$(cat)
ERROR=$(echo "$INPUT" | python3 -c "import sys,json;r=json.load(sys.stdin).get('result',{});e=r.get('error','') if isinstance(r,dict) else '';print(e[:200])" 2>/dev/null || true)
if [ -n "$ERROR" ]; then
    echo "$INPUT" | python3 /Users/sxliuyu/.hermes/tools/error_correction.py record --task "last tool call" --error "$ERROR" --attempted "auto" 2>/dev/null || true
fi
exit 0