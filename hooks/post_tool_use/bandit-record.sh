#!/bin/bash
set -euo pipefail
INPUT=$(cat)
TOOL=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('tool',''))" 2>/dev/null || true)
ERR=$(echo "$INPUT" | python3 -c "import sys,json;r=json.load(sys.stdin).get('result',{});e=r.get('error','') if isinstance(r,dict) else '';print(e[:10])" 2>/dev/null || true)
SUCCESS_FLAG="--success" && [ -n "$ERR" ] && SUCCESS_FLAG=""
# Guess task type from context
ARGS=$(echo "$INPUT" | python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin).get('args',{})))" 2>/dev/null || echo "{}")
TASKTYPE="debug"
echo "$ARGS" | grep -qi "feature\|实现\|新增" && TASKTYPE="feature"
echo "$ARGS" | grep -qi "refactor\|重构\|清理" && TASKTYPE="refactor"
python3 ~/.hermes/tools/bandit_router.py record --task-type "$TASKTYPE" --tool "$TOOL" $SUCCESS_FLAG 2>/dev/null || true
exit 0
