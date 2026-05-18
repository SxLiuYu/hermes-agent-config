#!/bin/bash
set -euo pipefail
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)
if [ -n "$TOOL" ]; then
    python3 ~/.hermes/tools/tool_chain_fusion.py record --tools "$TOOL" --chain-id "session" 2>/dev/null || true
fi
exit 0
