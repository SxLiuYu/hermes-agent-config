#!/bin/bash
# SessionStart: auto-grade last turn output and inject quality score
# Anthropic-styled Outcomes: separate grading agent scores against rubric
SCRIPT="$HOME/.hermes/scripts/outcomes_grader.py"
SESSION_MEMORY="$HOME/.hermes/session_memory.md"

[ -f "$SCRIPT" ] || exit 1

# Grade last session output if available
if [ -f "$SESSION_MEMORY" ]; then
    TEXT=$(tail -200 "$SESSION_MEMORY" 2>/dev/null)
    if [ -n "$TEXT" ]; then
        python3 "$SCRIPT" grade --text "$TEXT" --model "qwen3-32b" > /dev/null 2>&1 || true
    fi
fi

# Generate context injection from last score
INJECTION=$(python3 "$SCRIPT" inject 2>/dev/null)
if [ -n "$INJECTION" ]; then
    echo "$INJECTION"
    echo ""
    echo "*Quality score from last turn. Target: >=7.5/10*"
fi

exit 0