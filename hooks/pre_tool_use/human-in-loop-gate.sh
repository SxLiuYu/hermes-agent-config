#!/bin/bash
set -euo pipefail
INPUT=$(cat)
ACTION=$(echo "$INPUT" | python3 -c "import sys,json;a=json.load(sys.stdin).get('args',{});print(a.get('command',a.get('action','')))" 2>/dev/null || true)
if [ -n "$ACTION" ]; then
    python3 ~/.hermes/tools/human_in_loop.py check --action "$ACTION" 2>/dev/null || true
fi
exit 0
