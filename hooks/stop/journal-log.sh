#!/bin/bash
# =============================================================================
# journal-log.sh — Auto-log tasks on session stop / subagent stop
# =============================================================================
# Triggers on Stop and SubagentStop events.
# Reads hook JSON from stdin, extracts task info, logs to task_journal.py.
#
# Context: P0-16 Agent Task Journal — AutoSkill/EvolveR-style self-evolution
# =============================================================================

set -euo pipefail
INPUT=$(cat)

# Parse event type and session info
EVENT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('event',''))" 2>/dev/null || true)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || true)

# --- For SubagentStop: extract grading info ---
if [ "$EVENT" = "SubagentStop" ]; then
    GOAL=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
result = data.get('result', {})
# Try multiple keys
goal = result.get('goal','') or result.get('task','') or data.get('goal','') or 'subagent task'
print(goal[:200])
" 2>/dev/null || echo "subagent task")

    SCORE=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
result = data.get('result', {})
score = result.get('score', 0) or result.get('grade', 0) or 0
print(score)
" 2>/dev/null || echo "0")

    OUTCOME=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
result = data.get('result', {})
outcome = result.get('outcome','') or result.get('status','')
# Map
if outcome in ('success','completed','done'): print('success')
elif outcome in ('partial','warning'): print('partial')
elif outcome in ('failure','error','failed'): print('failure')
elif outcome == 'interrupted': print('interrupted')
else: print('unknown')
" 2>/dev/null || echo "unknown")

    python3 "$HOME/.hermes/tools/task_journal.py" log "$GOAL" "$OUTCOME" "$SCORE" "subagent" 2>/dev/null
    echo "[journal-hook] Logged subagent: $GOAL (outcome=$OUTCOME, score=$SCORE)" >&2
    exit 0
fi

# --- For Stop: log session summary ---
if [ "$EVENT" = "Stop" ]; then
    # Extract last user message as goal context
    GOAL=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
messages = data.get('messages', [])
# Find last user message
for msg in reversed(messages):
    if msg.get('role') == 'user':
        content = msg.get('content','')
        print(content[:200])
        break
" 2>/dev/null || echo "session task")

    python3 "$HOME/.hermes/tools/task_journal.py" log "$GOAL" "completed" "7" "session" 2>/dev/null
    echo "[journal-hook] Logged session: $GOAL" >&2
    exit 0
fi

exit 0