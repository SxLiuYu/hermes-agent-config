#!/bin/bash
# SubagentStop: auto-extract experience card when subagent scores >= 7
# Runs after outcomes-grade.sh; extracts goal/method/lesson from session memory
set -euo pipefail

INPUT=$(cat)

# Extract score from stdin JSON (populated by outcomes_grader.py result)
SCORE=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('result',{}); print(r.get('score',0) or r.get('grade',0) or 0)" 2>/dev/null || echo "0")

# Only create card if score >= 7
if [ "$(echo "$SCORE >= 7" | bc -l 2>/dev/null || echo 0)" != "1" ]; then
    exit 0
fi

# Extract goal from stdin
GOAL=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('result',{}); print((r.get('goal','') or r.get('task','') or d.get('goal','') or 'subagent task')[:200])" 2>/dev/null || echo "subagent task")

OUTCOME=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('result',{}); o=r.get('outcome','')or r.get('status',''); print('success' if o in('success','completed','done') else ('partial' if o in('partial','warning') else ('failure' if o in('failure','error','failed') else 'unknown')))" 2>/dev/null || echo "unknown")

TASK_ID="${HOOK_EVENT_SUBAGENT_ID:-unknown}"

# Extract lessons from session memory
SESSION_MEMORY="$HOME/.hermes/session_memory.md"
LESSONS=""
if [ -f "$SESSION_MEMORY" ]; then
    LESSONS=$(tail -150 "$SESSION_MEMORY" | grep -oP '(?:lesson|教训|经验|修复策略|fix):\s*.+' | head -3 | tr '\n' ',' | sed 's/,$//' 2>/dev/null || echo "")
fi

CARDS_TOOL="$HOME/.hermes/tools/experience_cards.py"
[ -f "$CARDS_TOOL" ] || exit 0

python3 "$CARDS_TOOL" hook \
  --goal "$GOAL" \
  --outcome "$OUTCOME" \
  --score "$SCORE" \
  --task-id "$TASK_ID" \
  ${LESSONS:+--lessons "$LESSONS"} \
  2>/dev/null

echo "[experience-card] Score=$SCORE → card created for: $GOAL" >&2
exit 0