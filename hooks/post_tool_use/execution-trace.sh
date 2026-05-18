#!/bin/bash
# PostToolUse: record execution trace node after every tool call
# Calls execution_tracer.py record with auto-classified is_mutate
set -euo pipefail

INPUT=$(cat)
TOOL="${HOOK_EVENT_TOOL_NAME:-unknown}"
SESSION_ID="${HOOK_EVENT_SESSION_ID:-${HERMES_SESSION_ID:-$(date +%s)}}"

# Extract tool input/output/success/error from stdin JSON
TOOL_INPUT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('input','') or d.get('params','') or d.get('arguments','') or ''; print(json.dumps(v) if isinstance(v,(dict,list)) else str(v)[:500])" 2>/dev/null || echo "{}")
TOOL_OUTPUT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('output','') or d.get('result','') or ''; print(str(v)[:500])" 2>/dev/null || echo "")
ERR=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','') or '')" 2>/dev/null || echo "")
SUCCESS="true"
[ -n "$ERR" ] && SUCCESS="false"

TRACER="$HOME/.hermes/tools/execution_tracer.py"
[ -f "$TRACER" ] || exit 0

python3 "$TRACER" record \
  --session-id "$SESSION_ID" \
  --tool "$TOOL" \
  --input "$TOOL_INPUT" \
  --output "$TOOL_OUTPUT" \
  --success "$SUCCESS" \
  ${ERR:+--error "$ERR"} \
  2>/dev/null

exit 0