#!/bin/bash
# SubagentStop: auto-grade subagent output when delegate_task completes
# 
# 对标 Anthropic Outcomes 的独立评分机制：
#   子 agent 完成任务后，用独立模型评分并记录到 outcomes_history
#   如果质量不达标，输出改进建议供 parent agent 参考
#
# HOOK_EVENT_SUBAGENT_ID, HOOK_EVENT_SUBAGENT_RESULT 等由 Hermes 注入

SCRIPT="$HOME/.hermes/scripts/outcomes_grader.py"
LOGFILE="$HOME/.hermes/logs/subagent_grades.log"

[ -f "$SCRIPT" ] || exit 0

# 获取子 agent 输出（从最近的 session memory 中提取）
SESSION_MEMORY="$HOME/.hermes/session_memory.md"
if [ ! -f "$SESSION_MEMORY" ]; then
    exit 0
fi

# 只取最近一个 turn 的 assistant 输出（子 agent 的结果通常在最近一次交互中）
TEXT=$(tail -150 "$SESSION_MEMORY" 2>/dev/null)
if [ -z "$TEXT" ]; then
    exit 0
fi

# 评分（静默，不污染 context）
RESULT=$(python3 "$SCRIPT" grade --text "$TEXT" --model "qwen3-32b" 2>&1)
EXIT_CODE=$?

# 记录到日志
mkdir -p "$(dirname "$LOGFILE")"
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] subagent_id=${HOOK_EVENT_SUBAGENT_ID:-unknown}" >> "$LOGFILE"
echo "$RESULT" >> "$LOGFILE"
echo "---" >> "$LOGFILE"

# 提取评分
AVG=$(echo "$RESULT" | grep -oP '\d+\.\d+(?=/10)' | head -1 || echo "0")

# 如果低于 6 分，输出警告给 parent agent
if [ -n "$AVG" ] && [ "$(echo "$AVG < 6.0" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
    echo ""
    echo "⚠️  **子 agent 输出质量偏低: ${AVG}/10**"
    echo ""
    # 提取改进建议
    echo "$RESULT" | grep -A 10 "改进建议" | tail -n +2 | head -5
    echo ""
fi

exit 0