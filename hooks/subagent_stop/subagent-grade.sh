#!/bin/bash
# SubagentStop: auto-grade subagent output when delegate_task completes
SCRIPT="$HOME/.hermes/scripts/outcomes_grader.py"
LOGFILE="$HOME/.hermes/logs/subagent_grades.log"

[ -f "$SCRIPT" ] || exit 0

SESSION_MEMORY="$HOME/.hermes/session_memory.md"
[ -f "$SESSION_MEMORY" ] || exit 0

TEXT=$(tail -150 "$SESSION_MEMORY" 2>/dev/null)
[ -z "$TEXT" ] && exit 0

RESULT=$(python3 "$SCRIPT" grade --text "$TEXT" --model "qwen3-32b" 2>&1)
EXIT_CODE=$?

mkdir -p "$(dirname "$LOGFILE")"
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] subagent_id=${HOOK_EVENT_SUBAGENT_ID:-unknown}" >> "$LOGFILE"
echo "$RESULT" >> "$LOGFILE"
echo "---" >> "$LOGFILE"

AVG=$(echo "$RESULT" | grep -oP '\d+\.\d+(?=/10)' | head -1 || echo "0")

if [ -n "$AVG" ] && [ "$(echo "$AVG < 6.0" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
    echo ""
    echo "⚠️  **Subagent output quality low: ${AVG}/10**"
    echo ""
    echo "$RESULT" | grep -A 10 "改进建议\|improvements" | tail -n +2 | head -5
    echo ""
fi

exit 0